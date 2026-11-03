"""Local skill sharing must preserve existing Codex copies and source files."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daemon import codex_sync


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skills = self.root / 'claude' / 'skills'
        self.forge = self.skills / 'skill-forge'
        self.target = self.root / 'agents' / 'skills'
        (self.forge / 'templates').mkdir(parents=True)
        self.source = self.skills / 'demo'
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text('latest')
        self.sources = self.forge / 'sources.json'
        self.sources.write_text(json.dumps({'demo': {'type': 'remote'}}))

    def enable(self):
        (self.forge / 'templates' / 'codex-sync.json').write_text(json.dumps({
            'enabled': True, 'target_dir': str(self.target)}))

    def sync(self, **kwargs):
        return codex_sync.sync_skills(self.skills, self.forge, **kwargs)

    def test_disabled_does_not_touch_codex(self):
        self.assertFalse(self.sync()['enabled'])
        self.assertFalse(self.target.exists())

    def test_link_follows_replacement_without_copying_again(self):
        self.enable()
        self.assertTrue(self.sync()['ok'])
        dest = self.target / 'demo'
        self.assertEqual(dest.resolve(), self.source.resolve())
        self.source.rename(self.skills / 'old-demo')
        self.source.mkdir()
        (self.source / 'SKILL.md').write_text('updated')
        self.assertEqual((dest / 'SKILL.md').read_text(), 'updated')
        self.assertEqual(self.sync()['unchanged'], ['demo'])

    def test_adoption_backs_up_existing_copy(self):
        self.enable()
        old = self.target / 'demo'
        old.mkdir(parents=True)
        (old / 'SKILL.md').write_text('customized')
        (old / 'notes.txt').write_text('keep me')
        self.assertFalse(self.sync()['ok'])
        self.assertEqual((old / 'SKILL.md').read_text(), 'customized')
        result = self.sync(adopt=True)
        self.assertTrue(result['ok'], result)
        backup = Path(result['backups']['demo'])
        self.assertEqual((backup / 'notes.txt').read_text(), 'keep me')
        self.assertEqual((old / 'SKILL.md').read_text(), 'latest')

    def test_failed_link_creation_preserves_existing_copy(self):
        self.enable()
        old = self.target / 'demo'
        old.mkdir(parents=True)
        (old / 'SKILL.md').write_text('customized')
        with patch.object(codex_sync, '_create_link', side_effect=OSError('denied')):
            self.assertFalse(self.sync(adopt=True)['ok'])
        self.assertEqual((old / 'SKILL.md').read_text(), 'customized')

    def test_uninstall_removes_only_matching_link(self):
        self.enable()
        self.sync()
        self.sources.write_text('{}')
        shutil.rmtree(self.source)
        self.assertTrue(self.sync(names=['demo'])['ok'])
        self.assertFalse(codex_sync._exists(self.target / 'demo'))
        manual = self.target / 'demo'
        manual.mkdir()
        (manual / 'SKILL.md').write_text('manual')
        self.sync(names=['demo'])
        self.assertEqual((manual / 'SKILL.md').read_text(), 'manual')

    def test_collection_root_does_not_expose_duplicate_skills(self):
        self.enable()
        (self.source / 'SKILL.md').unlink()
        self.sources.write_text(json.dumps({'demo': {'type': 'collection'}}))
        old = self.target / 'demo'
        old.mkdir(parents=True)
        (old / 'SKILL.md').write_text('obsolete fixture')
        result = self.sync(adopt=True)
        self.assertTrue(result['ok'])
        self.assertFalse(old.exists())
        self.assertEqual((Path(result['backups']['demo']) / 'SKILL.md').read_text(), 'obsolete fixture')

    def test_rejects_path_escape_and_overlapping_roots(self):
        self.enable()
        self.assertFalse(self.sync(names=['../escape'])['ok'])
        (self.forge / 'templates' / 'codex-sync.json').write_text(json.dumps({
            'enabled': True, 'target_dir': str(self.skills)}))
        self.assertFalse(self.sync()['ok'])

    def test_install_publishes_new_skill_after_validation(self):
        from daemon import installer, precheck
        self.enable()
        upstream = self.root / 'upstream'
        upstream.mkdir()
        (upstream / 'SKILL.md').write_text('new install')
        def clone(url, dest, *args, **kwargs):
            shutil.copytree(upstream, dest)
            return True, ''
        with patch.object(installer, 'SKILL_ROOT', self.skills), \
             patch.object(installer, 'FORGE_ROOT', self.forge), \
             patch.object(installer, 'SOURCES_FILE', self.sources), \
             patch.object(installer, '_git_clone', side_effect=clone), \
             patch.object(installer, '_get_latest_sha', return_value='a' * 40), \
             patch.object(precheck, 'verify_repo', return_value={'ok': True, 'summary': 'pass', 'layers': []}):
            result = installer.install_skill('demo', 'https://github.com/example/demo')
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.target / 'demo' / 'SKILL.md').read_text(), 'new install')


if __name__ == '__main__':
    unittest.main()
