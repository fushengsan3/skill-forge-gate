"""Upgrade routes and unknown requests cannot mutate the installation queue."""
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.request import ProxyHandler, Request, build_opener

from daemon import app_updater, queue_bridge


class AppUpdateBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'templates').mkdir()
        self.queue=self.root/'templates/install-queue.json'
        self.queue.write_text('{"pending":[{"name":"keep"}],"history":[]}')
        for context in (patch.object(queue_bridge,'SKILL_ROOT',self.root),patch.object(queue_bridge,'QUEUE_FILE',self.queue),
                        patch.object(queue_bridge.QueueHandler,'_authorized',return_value=True)):
            context.start();self.addCleanup(context.stop)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),queue_bridge.QueueHandler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def request(self,path,data=None):
        body=data if isinstance(data,bytes) else json.dumps(data).encode() if data is not None else None
        request=Request(f'http://127.0.0.1:{self.server.server_port}/{path}',data=body,headers={'Content-Type':'application/json'})
        try:
            with build_opener(ProxyHandler({})).open(request,timeout=3) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)

    def test_unknown_routes_and_invalid_json_are_rejected_without_queue_write(self):
        before=self.queue.read_bytes()
        self.assertEqual(self.request('unknown-command',{})[0],404)
        self.assertEqual(self.request('unknown-command')[0],404)
        self.assertEqual(self.request('',b'{invalid')[0],400)
        self.assertEqual(self.queue.read_bytes(),before)

    def test_upgrade_status_does_not_contact_github(self):
        with patch.object(app_updater,'_fetch_release') as network:
            code,payload=self.request('app-update')
        self.assertEqual(code,200);self.assertIn('current_version',payload);network.assert_not_called()

    def test_mutation_is_busy_but_upgrade_progress_remains_readable(self):
        app_updater.OPERATION_LOCK.acquire()
        try:
            self.assertEqual(self.request('',{'pending':[]})[0],409)
            self.assertEqual(self.request('app-update')[0],200)
        finally:app_updater.OPERATION_LOCK.release()
        self.assertTrue(json.loads(self.queue.read_text())['pending'])

    def test_online_search_also_starts_update_check(self):
        with patch.object(app_updater,'check_async') as check, \
             patch('daemon.semantic_search.search_online',return_value={'ok':True,'skills':[]}):
            self.assertEqual(self.request('ai-search',{'query':'pdf'})[0],200)
        check.assert_called_once_with(self.root)

    def test_successful_upgrade_closes_listener_before_quiet_restart(self):
        listener=MagicMock();listener.restart_requested=True
        with patch.object(queue_bridge,'ThreadingHTTPServer',return_value=listener), \
             patch.object(subprocess,'Popen') as launch:
            queue_bridge.start_bridge()
        listener.server_close.assert_called_once()
        launch.assert_called_once()
        args,kwargs=launch.call_args
        self.assertEqual(args[0][-1],str(self.root/'daemon/queue_bridge.py'))
        self.assertEqual(kwargs['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))


if __name__=='__main__':unittest.main()
