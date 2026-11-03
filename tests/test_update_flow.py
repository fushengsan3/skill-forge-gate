"""Update the registered skill only; retain the previous version on failure."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon import installer, precheck
from daemon import fetcher, queue_bridge


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.forge = self.root / 'skill-forge'
        self.forge.mkdir()
        self.sources = self.forge / 'sources.json'
        self.info = {'url': 'https://github.com/example/collection.git',
                     'branch': 'release', 'subpath': 'skills/target',
                     'installed_sha': 'a' * 40, 'installed_at': '2025-01-01',
                     'custom': 'keep'}
        self.sources.write_text(json.dumps({'target': self.info, 'sibling': {'keep': True}}))
        (self.root / 'target').mkdir()
        (self.root / 'target' / 'SKILL.md').write_text('old target')
        self.repo = self.root / 'upstream'
        (self.repo / 'skills' / 'target').mkdir(parents=True)
        (self.repo / 'SKILL.md').write_text('collection root')
        (self.repo / 'skills' / 'target' / 'SKILL.md').write_text('new target')
        self.clones = []

        def clone(url, dest, branch='main', **kwargs):
            self.clones.append((branch, kwargs))
            shutil.copytree(self.repo, dest)
            return True, ''

        for p in [patch.object(installer, 'SKILL_ROOT', self.root),
                  patch.object(installer, 'FORGE_ROOT', self.forge),
                  patch.object(installer, 'SOURCES_FILE', self.sources),
                  patch.object(installer, 'log'),
                  patch.object(installer, '_git_clone', side_effect=clone),
                  patch.object(installer, '_get_latest_sha', return_value='b' * 40)]:
            p.start()
            self.addCleanup(p.stop)
        self.verifier = patch.object(precheck, 'verify_repo', return_value={
            'ok': True, 'summary': 'passed', 'layers': []}).start()
        self.addCleanup(patch.stopall)

    def update(self):
        data = json.loads(self.sources.read_text())
        data['target'] = self.info
        self.sources.write_text(json.dumps(data))
        return installer.update_skill('target', self.info)

    def test_updates_exact_subpath_and_branch_and_keeps_backup(self):
        result = self.update()
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.root / 'target' / 'SKILL.md').read_text(), 'new target')
        self.assertEqual(self.clones[0], ('release', {'strict_branch': True}))
        self.assertEqual((Path(result['backup']) / 'SKILL.md').read_text(), 'old target')
        data = json.loads(self.sources.read_text())
        self.assertEqual(data['target']['installed_sha'], 'b' * 40)
        for key in ('branch', 'subpath', 'custom', 'installed_at'):
            self.assertEqual(data['target'][key], self.info[key])
        self.assertEqual(data['sibling'], {'keep': True})
        self.assertFalse((self.root / 'collection').exists())

    def test_missing_subpath_does_not_install_repository_root(self):
        self.info['subpath'] = 'missing'
        self.assertFalse(self.update()['ok'])
        self.assertEqual((self.root / 'target' / 'SKILL.md').read_text(), 'old target')

    def test_rejected_update_keeps_existing_files_and_registry(self):
        before = self.sources.read_bytes()
        self.verifier.return_value = {'ok': False, 'summary': 'rejected', 'layers': []}
        self.assertFalse(self.update()['ok'])
        self.assertEqual((self.root / 'target' / 'SKILL.md').read_text(), 'old target')
        self.assertEqual(self.sources.read_bytes(), before)

    def test_registry_failure_rolls_back_files(self):
        original = Path.replace
        def fail_registry(path, target):
            if Path(target) == self.sources:
                raise OSError('disk write failed')
            return original(path, target)
        with patch.object(Path, 'replace', fail_registry):
            self.assertFalse(self.update()['ok'])
        self.assertEqual((self.root / 'target' / 'SKILL.md').read_text(), 'old target')
        self.assertEqual(json.loads(self.sources.read_text())['target']['installed_sha'], 'a' * 40)

    def test_subpath_escape_is_rejected(self):
        self.info['subpath'] = '../outside'
        self.assertFalse(self.update()['ok'])
        self.verifier.assert_not_called()

    def test_successful_update_syncs_to_codex(self):
        target = self.root / 'codex-skills'
        (self.forge / 'templates').mkdir()
        (self.forge / 'templates' / 'codex-sync.json').write_text(json.dumps({
            'enabled': True, 'target_dir': str(target)}))
        # Use a sibling directory, as the real ~/.agents and ~/.claude roots are.
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / 'skills'
            (self.forge / 'templates' / 'codex-sync.json').write_text(json.dumps({
                'enabled': True, 'target_dir': str(target)}))
            result = self.update()
            self.assertTrue(result['ok'], result)
            self.assertEqual((target / 'target' / 'SKILL.md').read_text(), 'new target')
            self.assertTrue(result['codex_sync']['ok'])

    def test_bridge_invalidates_old_update_cache(self):
        handler = object.__new__(queue_bridge.QueueHandler)
        replies = []
        handler._json_reply = replies.append
        with patch.object(queue_bridge, 'SKILL_ROOT', self.forge), \
             patch.object(queue_bridge, 'SKILLS_DIR', self.root), \
             patch.object(installer, 'update_skill', return_value={'ok': True}) as update, \
             patch.dict(queue_bridge._check_updates_cache, {'at': 42, 'result': {'count': 1}}):
            handler._handle_update({'name': 'target', 'url': 'https://github.com/untrusted/repo'})
            update.assert_called_once_with('target', self.info)
            self.assertTrue(replies[0]['ok'])
            self.assertIsNone(queue_bridge._check_updates_cache['result'])

    def test_short_sha_is_not_a_false_update(self):
        self.info['installed_sha'] = 'b' * 8
        self.sources.write_text(json.dumps({'target': self.info}))
        handler = object.__new__(queue_bridge.QueueHandler)
        with patch.object(queue_bridge, 'SKILL_ROOT', self.forge), \
             patch.object(fetcher, 'fetch_latest_shas', return_value={'target': 'b' * 40}):
            self.assertEqual(handler._compute_updates()['count'], 0)

    def test_update_check_keeps_codex_and_claude_versions_separate(self):
        (self.forge/'templates').mkdir(exist_ok=True)
        (self.forge/'templates/codex-sources.json').write_text(json.dumps({
            'target': dict(self.info, installed_sha='c'*40)}))
        handler = object.__new__(queue_bridge.QueueHandler)
        latest = {'target':'b'*40, 'codex:target':'d'*40}
        with patch.object(queue_bridge, 'SKILL_ROOT', self.forge), \
             patch.object(fetcher, 'fetch_latest_shas', return_value=latest):
            result = handler._compute_updates()
        self.assertEqual(result['count'], 2)
        self.assertEqual({(u['name'], u.get('tool', 'claude')) for u in result['updates']},
                         {('target', 'claude'), ('target', 'codex')})

    def test_shared_repository_is_checked_once_per_branch(self):
        sources = {'one': self.info, 'two': dict(self.info, subpath='skills/two')}
        with patch.object(fetcher, 'api_get', return_value={'ok': True, 'data': {'sha': 'b' * 40}}) as api:
            self.assertEqual(fetcher.fetch_latest_shas(sources), {'one': 'b' * 40, 'two': 'b' * 40})
            self.assertEqual(api.call_count, 1)

    def collection(self):
        self.info['subpath'] = ''
        (self.repo / 'SKILL.md').unlink()
        (self.repo / 'skills' / 'target').rename(self.repo / 'skills' / 'alpha')
        (self.repo / 'scripts').mkdir()
        (self.repo / 'scripts' / 'core.py').write_text('print("core")')
        (self.repo / 'tests' / 'example').mkdir(parents=True)
        (self.repo / 'tests' / 'example' / 'SKILL.md').write_text('test fixture')
        (self.root / 'alpha').mkdir()
        (self.root / 'alpha' / 'SKILL.md').write_text('old alpha')
        data = json.loads(self.sources.read_text())
        data['alpha'] = dict(self.info, subpath='skills/alpha')
        self.sources.write_text(json.dumps(data))

    def test_collection_updates_children_and_shared_runtime_without_test_fixtures(self):
        self.collection()
        result = self.update()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['updated_skills'], ['alpha'])
        self.assertEqual((self.root / 'alpha' / 'SKILL.md').read_text(), 'new target')
        self.assertTrue((self.root / 'target' / 'scripts' / 'core.py').is_file())
        self.assertFalse((self.root / 'target' / 'SKILL.md').exists())
        self.assertFalse((self.root / 'target' / 'tests').exists())
        self.assertFalse((self.root / 'example').exists())
        data = json.loads(self.sources.read_text())
        self.assertEqual(data['target']['type'], 'collection')
        self.assertEqual(data['alpha']['installed_sha'], 'b' * 40)
        self.assertEqual(data['alpha']['subpath'], 'skills/alpha')
        self.assertEqual((Path(result['backup']) / 'SKILL.md').read_text(), 'old target')

    def test_moved_primary_skill_updates_only_its_registered_directory(self):
        self.info['subpath'] = ''
        (self.root / 'target' / 'SKILL.md').write_text('---\nname: gpt-image\ndescription: original\n---\nold')
        (self.repo / 'SKILL.md').unlink()
        (self.repo / 'skills' / 'target').rename(self.repo / 'skills' / 'gpt-image')
        (self.root / 'gpt-image').mkdir()
        (self.root / 'gpt-image' / 'SKILL.md').write_text('independent Claude skill')
        with tempfile.TemporaryDirectory() as other:
            codex = Path(other) / 'skills'
            (codex / 'gpt-image').mkdir(parents=True)
            (codex / 'gpt-image' / 'SKILL.md').write_text('independent Codex skill')
            (self.forge / 'templates').mkdir(exist_ok=True)
            (self.forge / 'templates' / 'codex-sync.json').write_text(json.dumps({
                'enabled': True, 'target_dir': str(codex)}))
            result = self.update()
            self.assertTrue(result['ok'], result)
            self.assertEqual((self.root / 'target' / 'SKILL.md').read_text(), 'new target')
            self.assertEqual((self.root / 'gpt-image' / 'SKILL.md').read_text(), 'independent Claude skill')
            self.assertEqual((codex / 'gpt-image' / 'SKILL.md').read_text(), 'independent Codex skill')
            self.assertEqual((codex / 'target' / 'SKILL.md').read_text(), 'new target')
            self.assertEqual(json.loads(self.sources.read_text())['target']['subpath'], 'skills/gpt-image')
            self.assertEqual((Path(result['backup']) / 'SKILL.md').read_text().splitlines()[-1], 'old')

    def test_moved_primary_without_matching_entry_does_not_replace_existing_skill(self):
        self.info['subpath'] = ''
        (self.root / 'target' / 'SKILL.md').write_text('---\nname: missing\ndescription: original\n---\nold')
        (self.repo / 'SKILL.md').unlink()
        result = self.update()
        self.assertFalse(result['ok'])
        self.assertIn('old', (self.root / 'target' / 'SKILL.md').read_text())

    def test_collection_failure_restores_root_and_children(self):
        self.collection()
        original = Path.replace
        def fail_registry(path, target):
            if Path(target) == self.sources:
                raise OSError('disk write failed')
            return original(path, target)
        with patch.object(Path, 'replace', fail_registry):
            self.assertFalse(self.update()['ok'])
        self.assertEqual((self.root / 'target' / 'SKILL.md').read_text(), 'old target')
        self.assertEqual((self.root / 'alpha' / 'SKILL.md').read_text(), 'old alpha')

    def test_collection_rejects_child_name_owned_by_another_repository(self):
        self.collection()
        data = json.loads(self.sources.read_text())
        data['alpha']['url'] = 'https://github.com/someone/else.git'
        self.sources.write_text(json.dumps(data))
        self.assertFalse(self.update()['ok'])
        self.assertEqual((self.root / 'alpha' / 'SKILL.md').read_text(), 'old alpha')

    def test_collection_checks_shared_scripts(self):
        self.collection()
        from verify import l3_content_scan
        with patch.object(l3_content_scan, 'scan_skill', return_value={'verdict': 'REJECT', 'reason': 'blocked'}):
            self.assertFalse(self.update()['ok'])
        self.assertEqual((self.root / 'alpha' / 'SKILL.md').read_text(), 'old alpha')


if __name__ == '__main__':
    unittest.main()
