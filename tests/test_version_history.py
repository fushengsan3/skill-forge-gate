import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daemon import installer, module_install, version_history


class VersionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skills = self.root / 'skills'
        self.skills.mkdir()
        self.forge = self.root / 'forge'
        self.forge.mkdir()
        self.sources = self.forge / 'sources.json'
        self.sources.write_text(json.dumps({'sample': {'url':'https://github.com/example/project', 'branch':'main', 'installed_sha':'a'*40}}))
        (self.skills / 'sample').mkdir()
        (self.skills / 'sample' / 'SKILL.md').write_text('current')
        self.patches = [patch.object(installer, 'SKILL_ROOT', self.skills), patch.object(installer, 'FORGE_ROOT', self.forge), patch.object(installer, 'SOURCES_FILE', self.sources)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def test_local_backup_restores_record_and_keeps_current_as_new_backup(self):
        backup = self.forge / '.backup' / 'sample-20260101-120000-123456'
        backup.mkdir(parents=True)
        (backup / 'SKILL.md').write_text('older')
        version_history.write_backup_metadata(backup, {'url':'https://github.com/example/project', 'branch':'main', 'installed_sha':'b'*40})
        with patch.object(version_history.precheck, 'verify_repo', return_value={'ok':True,'layers':[]}):
            result = version_history.rollback('sample', 'claude', backup.name)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.skills / 'sample' / 'SKILL.md').read_text(), 'older')
        self.assertEqual(json.loads(self.sources.read_text())['sample']['installed_sha'], 'b'*40)
        self.assertEqual((Path(result['backup']) / 'SKILL.md').read_text(), 'current')

    def test_listing_uses_registered_public_source_and_local_backups(self):
        backup = self.forge / '.backup' / 'sample-20260101-120000'
        backup.mkdir(parents=True)
        with patch.object(version_history.fetcher, 'api_get', return_value={'ok':True, 'data':[{'sha':'b'*40, 'commit':{'message':'old', 'committer':{'date':'2026-01-01'}}}]}) as request:
            result = version_history.history('sample', 'claude')
        self.assertTrue(result['ok'])
        self.assertEqual(result['commits'][0]['sha'], 'b'*40)
        self.assertEqual(result['backups'][0]['name'], backup.name)
        self.assertIn('api.github.com/repos/example/project/commits', request.call_args.args[0])
        self.assertNotIn(str(self.root), request.call_args.args[0])

    def test_rejects_backup_path_traversal(self):
        self.assertFalse(version_history.rollback('sample', 'claude', '../other')['ok'])

    def test_collection_rollback_restores_matching_children_together(self):
        data = json.loads(self.sources.read_text())
        data['sample']['type'] = 'collection'
        data['child'] = dict(data['sample'], type='remote', subpath='skills/child')
        self.sources.write_text(json.dumps(data))
        (self.skills / 'child').mkdir()
        (self.skills / 'child' / 'SKILL.md').write_text('current child')
        stamp = '20260101-120000-123456'
        backup = self.forge / '.backup' / ('sample-' + stamp)
        child = self.forge / '.backup' / ('child-' + stamp)
        (backup / 'skills' / 'child').mkdir(parents=True)
        (backup / 'skills' / 'child' / 'SKILL.md').write_text('old child')
        child.mkdir()
        (child / 'SKILL.md').write_text('old child')
        for path, record in ((backup, data['sample']), (child, data['child'])):
            version_history.write_backup_metadata(path, dict(record, installed_sha='b'*40))
        with patch.object(version_history.precheck, 'verify_collection', return_value={'ok':True, 'layers':[]}), \
             patch.object(installer, '_finish_codex_sync', side_effect=lambda name, result: result):
            result = version_history.rollback('sample', 'claude', backup.name)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.skills / 'child' / 'SKILL.md').read_text(), 'old child')
        self.assertEqual(json.loads(self.sources.read_text())['child']['installed_sha'], 'b'*40)
        self.assertEqual(result['updated_skills'], ['child'])

    def test_codex_backup_does_not_touch_claude_copy(self):
        codex = self.root / 'codex'
        codex.mkdir()
        dest = codex / 'sample'
        dest.mkdir()
        (dest / 'SKILL.md').write_text('codex current')
        registry = self.forge / 'templates' / 'codex-sources.json'
        registry.parent.mkdir()
        registry.write_text(self.sources.read_text())
        backup = codex.parent / '.skill-forge-backups' / 'sample-20260101-120000'
        backup.mkdir(parents=True)
        (backup / 'SKILL.md').write_text('codex old')
        with patch.object(module_install, 'CODEX_ROOT', codex), \
             patch.object(version_history.precheck, 'verify_repo', return_value={'ok':True,'layers':[]}):
            result = version_history.rollback('sample', 'codex', backup.name)
        self.assertTrue(result['ok'], result)
        self.assertEqual((dest / 'SKILL.md').read_text(), 'codex old')
        self.assertEqual((self.skills / 'sample' / 'SKILL.md').read_text(), 'current')


if __name__ == '__main__': unittest.main()
