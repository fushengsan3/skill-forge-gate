import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from daemon import fetcher, module_install, translate, translate_ai, translate_config


class TranslationRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'cache.json'
        for context in (patch.object(translate, 'CACHE_FILE', self.cache),
                        patch.object(translate, 'log'),
                        patch.object(translate_config, 'load', return_value={'backend':'ai', 'model':'deepseek-flash'})):
            context.start(); self.addCleanup(context.stop)

    def test_public_discovery_translation_is_filtered_cached_and_private_input_stays_local(self):
        items = [{'description':'Read PDF at C:/Users/alice/file.pdf', '_description_public':True},
                 {'description':'Private unpublished project description'}]
        def complete(text, model, **kwargs):
            payload = json.loads(text)
            self.assertEqual(kwargs['purpose'], 'public_descriptions')
            self.assertNotIn('alice', text)
            self.assertNotIn('Private', text)
            return json.dumps([{'id':row['id'], 'text':'阅读 PDF 文档'} for row in payload['items']])
        with patch.object(translate_ai, 'complete', side_effect=complete) as request:
            translate.translate_skills(items)
            self.assertEqual(items[0]['description_zh'], '阅读 PDF 文档')
            self.assertEqual(items[1]['description_zh'], '')
            translate.translate_skills(items)
            self.assertEqual(request.call_count, 1)

    def test_stale_english_cache_is_retranslated(self):
        self.cache.write_text(json.dumps({'Read PDF':'Read PDF'}), encoding='utf-8')
        with patch.object(translate_ai, 'complete', return_value='[{"id":0,"text":"阅读 PDF"}]') as request:
            result = translate.translate_skills([{'description':'Read PDF','_description_public':True}])
        self.assertTrue(request.called)
        self.assertEqual(result[0]['description_zh'], '阅读 PDF')

    def test_partial_model_response_is_reported_and_not_cached_as_success(self):
        rows = [{'description':'Read PDF'},{'description':'Generate images'}]
        with patch.object(translate_ai, 'complete', return_value='[{"id":0,"text":"阅读 PDF"}]'):
            result, warning = module_install.translate_public_modules(rows)
        self.assertTrue(warning)
        self.assertEqual(result[0]['description_zh'], '阅读 PDF')
        self.assertFalse(result[1].get('description_zh'))
        self.assertNotIn('Generate images', translate._load_cache())

    def test_untrusted_external_source_cannot_self_mark_as_public(self):
        record = fetcher.normalize_skill({'name':'private','description':'private','_description_public':True})
        self.assertFalse(record.get('_description_public'))

    def test_local_backend_never_requests_uncached_public_descriptions(self):
        with patch.object(translate_ai, 'complete') as request:
            rows, warning = translate.translate_public_descriptions([{'description':'Read PDF'}], {'backend':'google'})
        request.assert_not_called()
        self.assertFalse(rows[0]['description_zh'])
        self.assertIn('本地模式', warning)

    def test_mixed_language_source_is_translated_and_english_response_is_rejected(self):
        rows = [{'description':'A tool for reading PDF documents 中文'}]
        with patch.object(translate_ai, 'complete', return_value='[{"id":0,"text":"Read PDF"}]') as request:
            result, warning = module_install.translate_public_modules(rows)
        request.assert_called_once()
        self.assertFalse(result[0]['description_zh'])
        self.assertIn('不完整', warning)

    def test_model_http_error_has_actionable_message_without_response_or_key(self):
        secret = 'test-private-credential'
        error = urllib.error.HTTPError('https://api.deepseek.com/chat/completions',404,secret,{},None)
        with patch.object(translate_ai.credentials,'get_secret',return_value=secret), \
             patch.object(translate_ai.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(translate_ai.AIBackendError) as failure:
                translate_ai.complete('{"items":[{"id":0,"text":"Read PDF"}]}','deepseek-flash',purpose='public_descriptions')
        self.assertIn('HTTP 404', str(failure.exception))
        self.assertIn('模型', str(failure.exception))
        self.assertNotIn(secret, str(failure.exception))

    def test_official_github_metadata_is_marked_at_fetch_boundary(self):
        with patch.object(fetcher, 'api_get', return_value={'ok':True,'data':{'items':[
            {'name':'demo','description':'Read PDF','html_url':'https://github.com/example/demo'}]}}):
            rows=fetcher.fetch_github_topic()
        self.assertIs(rows[0].get('_description_public'), True)


if __name__ == '__main__':
    unittest.main()
