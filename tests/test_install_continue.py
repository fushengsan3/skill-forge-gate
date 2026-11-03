import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daemon import installer


class ContinueInstallTests(unittest.TestCase):
    def test_duplicate_queue_processing_is_rejected(self):
        with installer._queue_lock:
            result = installer.process_install_queue()
        self.assertIn('正在执行', result['error'])

    def test_rejection_report_contains_readable_finding_details(self):
        from daemon import precheck
        layer = precheck._run('L3 内容', precheck.STATIC, lambda: {
            'verdict': 'REJECT', 'reason': '发现高危规则',
            'red': [{'file': 'scripts/run.py', 'line': 12, 'rule': '危险操作'}]})
        self.assertIn('scripts/run.py:12', layer['details'][0])
        self.assertIn('危险操作', layer['details'][0])

    def test_failures_are_retained_and_later_items_continue(self):
        from daemon import module_install
        with tempfile.TemporaryDirectory() as temp:
            queue_file = Path(temp) / 'queue.json'
            queue_file.write_text(json.dumps({'pending': [
                {'skill_name': 'bad', 'repo_url': 'https://github.com/a/bad', 'selected_modules':[{'path':'.','target':'claude'}]},
                {'skill_name': 'good', 'repo_url': 'https://github.com/a/good', 'selected_modules':[{'path':'.','target':'claude'}]},
            ], 'history': []}), encoding='utf-8')
            def install(name, url, branch, sha, selected):
                return {'ok': name == 'good', 'error': 'L3 拒绝' if name == 'bad' else '',
                        'failed_modules': selected if name == 'bad' else [],
                        'modules': [{'ok':name == 'good', 'name':name}]}
            with patch.object(installer, 'QUEUE_FILE', queue_file), \
                 patch.object(module_install, 'install_selected', side_effect=install), \
                 patch.object(installer, 'log'):
                result = installer.process_install_queue()
            saved = json.loads(queue_file.read_text(encoding='utf-8'))
            self.assertEqual((result['success'], result['failed']), (1, 1))
            self.assertEqual([x['skill_name'] for x in saved['pending']], ['bad'])
            self.assertEqual([x['name'] for x in saved['history']], ['good'])
            self.assertEqual(result['results'][0]['error'], 'L3 拒绝')

    def test_old_queue_item_waits_for_first_install_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            queue_file = Path(temp) / 'queue.json'
            queue_file.write_text(json.dumps({'pending':[{'name':'old','url':'https://github.com/a/b'}]}))
            with patch.object(installer, 'QUEUE_FILE', queue_file), patch.object(installer, 'log'), \
                 patch.object(installer, 'install_skill') as legacy:
                result = installer.process_install_queue()
            legacy.assert_not_called()
            self.assertTrue(result['pending'][0]['needs_preview'])

    def test_only_failed_modules_are_retained_for_retry(self):
        from daemon import module_install
        with tempfile.TemporaryDirectory() as temp:
            queue_file = Path(temp) / 'queue.json'
            first = {'path': 'skills/a', 'target': 'claude'}
            second = {'path': 'skills/b', 'target': 'codex'}
            queue_file.write_text(json.dumps({'pending': [{'name': 'repo', 'url': 'https://github.com/a/b',
                'branch': 'main', 'preview_sha': 'a'*40, 'selected_modules': [first, second]}]}), encoding='utf-8')
            with patch.object(installer, 'QUEUE_FILE', queue_file), patch.object(installer, 'log'), \
                 patch.object(module_install, 'install_selected', return_value={
                     'ok':False, 'error':'部分模块未安装', 'failed_modules':[second],
                     'modules':[{'ok':True, **first}, {'ok':False, **second}]}):
                result = installer.process_install_queue()
            self.assertEqual(result['pending'][0]['selected_modules'], [second])
            self.assertEqual(json.loads(queue_file.read_text(encoding='utf-8'))['pending'][0]['selected_modules'], [second])


if __name__ == '__main__': unittest.main()
