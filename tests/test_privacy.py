import json
import os
import unittest
from unittest.mock import patch

from daemon import credentials, fetcher, privacy, semantic_search, translate_ai
from verify import l4_conflict_detect, l5_sandbox


class PrivacyTests(unittest.TestCase):
    def test_github_requests_never_use_stored_tokens(self):
        with patch.object(credentials, 'get_secret', side_effect=AssertionError('must not read token')):
            headers = fetcher.github_headers()
        self.assertNotIn('Authorization', headers)

    def test_git_environment_ignores_local_credentials_and_config(self):
        env = privacy.git_env({'PATH':'example','GITHUB_TOKEN':'secret','GIT_CONFIG_COUNT':'1',
                               'GIT_CONFIG_KEY_0':'http.extraHeader','GIT_ASKPASS':'helper'})
        self.assertNotIn('GITHUB_TOKEN', env)
        self.assertNotIn('GIT_CONFIG_COUNT', env)
        self.assertNotIn('GIT_CONFIG_KEY_0', env)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')

    def test_sensitive_text_is_not_used_as_search_model_input(self):
        captured = []
        def complete(text, model, system_prompt, **kwargs):
            captured.append(text); return '[0]'
        with patch.object(semantic_search.translate_config, 'load', return_value={'model':'deepseek-flash'}), \
             patch.object(semantic_search.translate_ai, 'complete', side_effect=complete):
            result = semantic_search.search('查 PDF 论文，路径 C:\\Users\\alice\\secret.txt，邮箱 a@example.com',
                [{'name':'pdf-reader','description':'Read files at C:\\Users\\alice\\report.pdf'}])
        self.assertEqual(result['mode'], 'ai')
        self.assertNotIn('alice', captured[0])
        self.assertNotIn('example.com', captured[0])
        self.assertNotIn('report.pdf', captured[0])
        self.assertIn('pdf', captured[0].lower())

    def test_github_search_receives_only_generic_tags(self):
        with patch.object(fetcher, 'api_get', return_value={'ok':True, 'data':{'items':[]}}) as request:
            fetcher.search_skills('PDF C:\\Users\\alice\\private.pdf alice@example.com')
        self.assertNotIn('alice', request.call_args.args[0])
        self.assertIn('pdf', request.call_args.args[0])

    def test_provider_rejects_arbitrary_input_before_reading_key(self):
        with patch.object(credentials, 'get_secret', side_effect=AssertionError('must not read token')):
            with self.assertRaises(translate_ai.AIBackendError):
                translate_ai.complete(json.dumps({'request':'C:/Users/alice'}), 'deepseek-flash', purpose='capabilities')
            with self.assertRaises(translate_ai.AIBackendError):
                translate_ai.complete(json.dumps({'request_tags':['pdf'], 'candidates':[], 'private':'secret'}), 'deepseek-flash', purpose='capabilities')

    def test_only_approved_official_endpoint_receives_shared_key(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"choices":[{"message":{"content":"[0]"}}]}'
        class Opener:
            def __init__(self): self.request = None
            def open(self, request, timeout): self.request = request; return Response()
        opener=Opener()
        payload=json.dumps({'request_tags':['pdf'], 'candidates':[{'id':0,'tags':['pdf']}]})
        with patch.object(credentials, 'get_secret', return_value='test-secret'), \
             patch('urllib.request.build_opener', return_value=opener):
            result=translate_ai.complete(payload, 'deepseek-flash', purpose='capabilities')
        self.assertEqual(result, '[0]')
        self.assertEqual(opener.request.full_url, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(opener.request.get_header('Authorization'), 'Bearer test-secret')
        with self.assertRaises(translate_ai.AIBackendError):
            translate_ai.complete(payload, 'deepseek-flash', purpose='capabilities', provider='unapproved')
        with patch.dict(translate_ai.PROVIDERS, {'other':{'endpoint':'https://example.com','model_pattern':'.*','credential':credentials.AI_TOKEN}}):
            with self.assertRaises(translate_ai.AIBackendError):
                translate_ai.complete(payload, 'deepseek-flash', purpose='capabilities', provider='other')

    def test_public_description_sanitizer_removes_private_indicators(self):
        text = 'Read PDF at C:\\Users\\alice\\secret.pdf; email alice@example.com; key sk-secret123456789012345'
        safe = privacy.clean_public_description(text)
        self.assertNotIn('alice', safe)
        self.assertNotIn('sk-secret', safe)

    def test_module_translation_uses_same_key_path_and_only_sanitized_descriptions(self):
        from daemon import module_install, translate_config
        seen = []
        def complete(text, model, system_prompt='', **kwargs):
            seen.append((json.loads(text), model, kwargs.get('purpose')))
            return '[{"id":0,"text":"本地阅读 PDF"}]'
        modules=[{'description':'Read PDF from C:\\Users\\alice\\private.pdf'}]
        with patch.object(translate_config, 'load', return_value={'model':'deepseek-flash'}), \
             patch.object(translate_ai, 'complete', side_effect=complete):
            result, warning = module_install.translate_public_modules(modules)
        self.assertEqual(result[0]['description_zh'], '本地阅读 PDF')
        self.assertEqual(seen[0][1:], ('deepseek-flash', 'public_descriptions'))
        self.assertNotIn('alice', json.dumps(seen[0][0]))

    def test_l4_and_l5_do_not_send_local_files_or_paths(self):
        report = {'conflicts': {'red': [{'requires_claude_analysis':True,'paths':['C:/Users/alice']}]}}
        with patch.object(l4_conflict_detect.llm_auth, 'credentials', side_effect=AssertionError('must not read key')):
            out = l4_conflict_detect.claude_deep_analysis(report)
        self.assertIn('隐私', out['claude_analysis']['error'])
        with patch.object(l5_sandbox.subprocess, 'run', side_effect=AssertionError('must not start')):
            result = l5_sandbox.run_docker_sandbox('local-private-folder', [])
        self.assertEqual(result['verdict'], 'SKIPPED')
        self.assertIn('隐私', result['error'])


if __name__ == '__main__': unittest.main()
