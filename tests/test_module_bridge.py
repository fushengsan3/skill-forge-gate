"""End-to-end preview and selected installation over an isolated local bridge."""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from daemon import installer, module_install, precheck, queue_bridge


class ModuleBridgeTests(unittest.TestCase):
    def test_preview_queue_and_install_separate_tool_variants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root/'upstream'; repo.mkdir()
            for path, description in [('skills/shared', 'Claude skill'), ('configs/codex/skills/shared', 'Codex skill')]:
                entry = repo/path; entry.mkdir(parents=True)
                (entry/'SKILL.md').write_text('---\nname: shared\ndescription: '+description+'\n---\nbody', encoding='utf-8')
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            git('init', '-b', 'main'); git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
            forge = root/'forge'; (forge/'templates').mkdir(parents=True)
            sources = forge/'sources.json'; sources.write_text('{}', encoding='utf-8')
            queue = forge/'templates/install-queue.json'
            claude = root/'claude'; codex = root/'codex'; claude.mkdir(); codex.mkdir()
            def clone(url, dest, *args, **kwargs):
                shutil.copytree(repo, dest)
                return True, ''
            with patch.object(installer, 'SKILL_ROOT', claude), patch.object(installer, 'FORGE_ROOT', forge), \
                 patch.object(installer, 'SOURCES_FILE', sources), patch.object(installer, 'QUEUE_FILE', queue), \
                 patch.object(installer, '_git_clone', side_effect=clone), patch.object(installer, 'log'), \
                 patch.object(module_install, 'CODEX_ROOT', codex), patch.object(queue_bridge, 'SKILL_ROOT', forge), \
                 patch.object(queue_bridge, 'QUEUE_FILE', queue), patch.object(queue_bridge.QueueHandler, '_authorized', return_value=True), \
                 patch.object(precheck, 'verify_repo', return_value={'ok':True, 'summary':'passed', 'layers':[]}):
                server = ThreadingHTTPServer(('127.0.0.1', 0), queue_bridge.QueueHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                opener = build_opener(ProxyHandler({}))
                def post(path, data):
                    req = Request(f'http://127.0.0.1:{server.server_port}/{path}',
                                  data=json.dumps(data).encode(), headers={'Content-Type':'application/json'}, method='POST')
                    with opener.open(req, timeout=15) as response: return json.load(response)
                try:
                    preview = post('preview-install', {'name':'project', 'url':'https://github.com/example/project'})
                    self.assertTrue(preview['ok'], preview)
                    self.assertEqual({m['platform'] for m in preview['modules']}, {'generic', 'codex'})
                    pending = [{'skill_name':'project','repo_url':'https://github.com/example/project',
                                'branch':preview['branch'],'preview_sha':preview['sha'],
                                'selected_modules':[{'path':'skills/shared','target':'claude'},
                                                    {'path':'configs/codex/skills/shared','target':'codex'}]}]
                    self.assertTrue(post('', {'version':1,'pending':pending})['ok'])
                    result = post('install', {})
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(result['success'], 1)
                    self.assertEqual(result['pending'], [])
                    self.assertIn('Claude skill', (claude/'shared/SKILL.md').read_text(encoding='utf-8'))
                    self.assertIn('Codex skill', (codex/'shared/SKILL.md').read_text(encoding='utf-8'))
                finally:
                    server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__': unittest.main()
