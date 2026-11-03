import json
import unittest
from unittest.mock import patch

from daemon import semantic_search, fetcher


class SemanticSearchTests(unittest.TestCase):
    def test_ai_ranks_only_existing_candidates(self):
        items = [{'name':'slides','description':'make presentations'},
                 {'name':'images','description':'generate pictures'}]
        with patch.object(semantic_search.translate_config, 'load', return_value={'model':'test'}), \
             patch.object(semantic_search.translate_ai, 'complete', return_value='[1, 999, 0, 1]'):
            result = semantic_search.search('我要制作演示文稿', items)
        self.assertEqual(result['mode'], 'ai')
        self.assertEqual(result['indices'], [1, 0])

    def test_unavailable_ai_reports_local_fallback(self):
        with patch.object(semantic_search.translate_config, 'load', return_value={'backend':'google', 'model':''}), \
             patch.object(semantic_search.cc_models, 'list_models', return_value={'current':''}):
            result = semantic_search.search('pdf', [{'name':'pdf reader','description':'documents'},
                                                     {'name':'music','description':'audio'}])
        self.assertEqual(result['mode'], 'local')
        self.assertIn(0, result['indices'])
        self.assertTrue(result['warning'])

    def test_invalid_model_response_is_not_marked_ai(self):
        with patch.object(semantic_search.translate_config, 'load', return_value={'model':'test'}), \
             patch.object(semantic_search.translate_ai, 'complete', return_value='not json'):
            result = semantic_search.search('pdf', [{'name':'pdf','description':'files'}])
        self.assertEqual(result['mode'], 'local')

    def test_online_search_queries_sources_with_only_generic_labels(self):
        github = {'ok':True,'skills':[{'name':'pdf-online','description':'PDF reader',
                  'url':'https://github.com/example/pdf-online','source':'search'}]}
        external = {'skills':[{'name':'pdf-external','description':'PDF support',
                    'url':'https://github.com/example/pdf-external','source':'external'}], 'errors':[]}
        with patch.object(fetcher, 'search_online', return_value=github) as remote, \
             patch.object(fetcher, 'fetch_external_sources', return_value=external), \
             patch.object(semantic_search.translate_config, 'load', return_value={'backend':'google'}):
            result = semantic_search.search_online('PDF 阅读 C:\\Users\\alice\\secret.pdf')
        self.assertTrue(result['ok'], result)
        self.assertEqual({s['name'] for s in result['skills']}, {'pdf-online','pdf-external'})
        self.assertEqual(remote.call_args.args[0], 'pdf')
        self.assertNotIn('alice', str(remote.call_args))
        self.assertEqual(result['scope'], 'online')

    def test_online_search_does_not_silently_use_old_local_results_on_failure(self):
        with patch.object(fetcher, 'search_online', return_value={'ok':False,'error':'HTTP 403','skills':[]}), \
             patch.object(fetcher, 'fetch_external_sources', return_value={'skills':[],'errors':[]}), \
             patch.object(fetcher, 'search_claudskills', return_value={'ok':False,'skills':[],'error':'目录不可用'}, create=True):
            result = semantic_search.search_online('PDF')
        self.assertFalse(result['ok'])
        self.assertIn('403', result['error'])

    def test_partial_source_failure_preserves_online_results_and_warning(self):
        external = {'skills':[{'name':'pdf-from-source','description':'PDF reader',
                    'url':'https://github.com/example/pdf-from-source','source':'external'}], 'errors':[]}
        with patch.object(fetcher, 'search_online', return_value={'ok':False,'error':'HTTP 403','skills':[]}), \
             patch.object(fetcher, 'fetch_external_sources', return_value=external), \
             patch.object(semantic_search.translate_config, 'load', return_value={'backend':'google'}):
            result = semantic_search.search_online('PDF')
        self.assertTrue(result['ok'])
        self.assertEqual(result['skills'][0]['name'], 'pdf-from-source')
        self.assertIn('403', result['warning'])

    def test_online_search_without_general_labels_does_not_call_network(self):
        with patch.object(fetcher, 'search_online', side_effect=AssertionError('network not allowed')):
            result = semantic_search.search_online('C:\\Users\\alice\\secret.txt')
        self.assertFalse(result['ok'])

    def test_no_candidates_does_not_call_paid_model(self):
        with patch.object(fetcher, 'search_online', return_value={'ok':True,'skills':[]}), \
             patch.object(fetcher, 'fetch_external_sources', return_value={'skills':[],'errors':[]}), \
             patch.object(fetcher, 'search_claudskills', return_value={'ok':True,'skills':[]}, create=True), \
             patch.object(semantic_search.translate_ai, 'complete', side_effect=AssertionError('unnecessary model call')):
            result = semantic_search.search_online('PDF')
        self.assertTrue(result['ok'])
        self.assertEqual(result['skills'], [])

    def test_public_catalog_fallback_when_github_is_rate_limited(self):
        fallback = {'ok':True,'skills':[{'name':'pdf-new','description':'PDF tools',
                    'url':'https://github.com/example/pdf-new','source':'claudskills'}]}
        with patch.object(fetcher, 'search_online', return_value={'ok':False,'error':'GitHub HTTP 403','skills':[]}), \
             patch.object(fetcher, 'fetch_external_sources', return_value={'skills':[],'errors':[]}), \
             patch.object(fetcher, 'search_claudskills', return_value=fallback, create=True) as directory, \
             patch.object(semantic_search.translate_config, 'load', return_value={'backend':'google'}):
            result = semantic_search.search_online('PDF C:\\Users\\alice\\secret')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['skills'][0]['name'], 'pdf-new')
        self.assertEqual(directory.call_args.args[0], ['pdf'])
        self.assertIn('403', result['warning'])

    def test_public_catalog_reads_source_url_and_filters_relevant_items(self):
        data={'skills':[{'name':'PDF tool','description':'Read PDF', 'source_url':'https://github.com/a/pdf/blob/main/SKILL.md'},
                        {'name':'Music tool','description':'audio', 'source_url':'https://github.com/a/music'}]}
        with patch.object(fetcher, 'api_get', return_value={'ok':True,'data':data}):
            report = fetcher.search_claudskills(['pdf'])
        self.assertTrue(report['ok'])
        self.assertEqual([s['url'] for s in report['skills']], ['https://github.com/a/pdf'])

    def test_github_online_query_contains_only_capability_tags(self):
        with patch.object(fetcher, 'api_get', return_value={'ok':True, 'data':{'items':[]}}) as api:
            report = fetcher.search_online('pdf')
        self.assertTrue(report['ok'])
        self.assertIn('pdf', api.call_args.args[0])
        self.assertNotIn('alice', api.call_args.args[0])


if __name__ == '__main__': unittest.main()
