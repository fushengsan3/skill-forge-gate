import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from daemon import fetcher, queue_bridge


class ExternalSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'templates').mkdir()
        self.file = self.root / 'templates' / 'external_sources.json'
        self.root_patch = patch.object(fetcher, 'SKILL_ROOT', self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def sources(self, sources):
        self.file.write_text(json.dumps({'sources':sources}), encoding='utf-8')

    def test_json_and_github_repository_are_imported(self):
        self.sources([{'url':'https://example.com/skills.json','label':'catalog'},
                      {'url':'https://github.com/example/project','label':'repo'}])
        def reply(url):
            if url == 'https://example.com/skills.json':
                return {'ok':True, 'data':[{'name':'alpha', 'url':'https://github.com/a/alpha'}]}
            if url == 'https://api.github.com/repos/example/project':
                return {'ok':True, 'data':{'name':'project','full_name':'example/project',
                                           'html_url':'https://github.com/example/project'}}
            return {'ok':False, 'reason':'unexpected URL'}
        with patch.object(fetcher, 'api_get', side_effect=reply):
            report = fetcher.fetch_external_sources(with_report=True)
        self.assertEqual([s['name'] for s in report['skills']], ['alpha','project'])
        self.assertEqual(report['errors'], [])

    def test_failures_are_reported_and_private_query_is_never_sent(self):
        self.sources([{'url':'https://example.com/data?token=secret', 'label':'private'},
                      {'url':'https://example.com/bad', 'label':'unavailable'}])
        with patch.object(fetcher, 'api_get', return_value={'ok':False, 'reason':'HTTP Error 503'}) as api:
            report = fetcher.fetch_external_sources(with_report=True)
        self.assertEqual(report['skills'], [])
        self.assertEqual(len(report['errors']), 2)
        self.assertEqual(api.call_count, 1)
        self.assertNotIn('secret', json.dumps(report, ensure_ascii=False))

    def test_unsupported_json_is_reported(self):
        self.sources([{'url':'https://example.com/data','label':'bad json shape'}])
        with patch.object(fetcher, 'api_get', return_value={'ok':True,'data':{'hello':'world'}}):
            report = fetcher.fetch_external_sources(with_report=True)
        self.assertEqual(len(report['errors']), 1)

    def test_invalid_entries_are_not_counted_as_skills(self):
        self.sources([{'url':'https://example.com/data','label':'catalog'}])
        with patch.object(fetcher, 'api_get', return_value={'ok':True,'data':[{'unrelated':'value'}]}):
            report = fetcher.fetch_external_sources(with_report=True)
        self.assertEqual(report['skills'], [])
        self.assertTrue(report['errors'])

    def test_catalog_source_url_is_converted_to_github_repository(self):
        self.sources([{'url':'https://example.com/data','label':'catalog'}])
        data = {'skills':[{'name':'alpha', 'source_url':'https://github.com/a/alpha/blob/main/SKILL.md'}]}
        with patch.object(fetcher, 'api_get', return_value={'ok':True,'data':data}):
            report = fetcher.fetch_external_sources(with_report=True)
        self.assertEqual(report['skills'][0]['url'], 'https://github.com/a/alpha')
        self.assertEqual(report['errors'], [])

    def test_bridge_save_then_fetch_and_reject_credential_url(self):
        class Handler:
            def _json_reply(self, payload): self.payload = payload
        handler = Handler()
        with patch.object(queue_bridge, 'EXT_SOURCES_FILE', self.file):
            queue_bridge.QueueHandler._handle_save_ext_sources(handler, {'sources':[{'url':'https://example.com/skills','label':'test'}]})
            self.assertTrue(handler.payload['ok'])
            with patch.object(fetcher, 'api_get', return_value={'ok':True,'data':[{'name':'alpha','url':'https://github.com/a/alpha'}]}):
                queue_bridge.QueueHandler._handle_fetch_ext_sources(handler)
            self.assertTrue(handler.payload['ok'])
            self.assertEqual(handler.payload['count'], 1)
            queue_bridge.QueueHandler._handle_save_ext_sources(handler, {'sources':[{'url':'https://example.com/?token=secret'}]})
            self.assertFalse(handler.payload['ok'])
            self.assertNotIn('secret', json.dumps(handler.payload))
            self.assertEqual(json.loads(self.file.read_text())['sources'][0]['label'], 'test')


if __name__ == '__main__': unittest.main()
