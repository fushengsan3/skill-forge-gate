import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daemon import installer, precheck


class ModuleInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        for rel, desc in [('skills/shared', '通用工具'), ('configs/codex/skills/shared', 'Codex 工具'),
                          ('skills/other', '其他能力')]:
            path = self.repo / rel
            path.mkdir(parents=True)
            (path / 'SKILL.md').write_text(f'---\nname: {path.name}\ndescription: {desc}\n---\nbody', encoding='utf-8')
        (self.repo / 'tests/example').mkdir(parents=True)
        (self.repo / 'tests/example/SKILL.md').write_text('fixture', encoding='utf-8')
        (self.repo / 'configs/gemini/skills/shared').mkdir(parents=True)
        (self.repo / 'configs/gemini/skills/shared/SKILL.md').write_text('---\nname: shared\ndescription: Gemini\n---\nbody', encoding='utf-8')
        self.claude = self.root / 'claude'
        self.codex = self.root / 'codex'
        self.claude.mkdir(); self.codex.mkdir()
        self.forge = self.root / 'forge'
        (self.forge / 'templates').mkdir(parents=True)
        self.sources = self.forge / 'sources.json'
        self.sources.write_text('{}', encoding='utf-8')
        def clone(url, dest, *args, **kwargs):
            shutil.copytree(self.repo, dest)
            return True, ''
        for p in [patch.object(installer, 'SKILL_ROOT', self.claude),
                  patch.object(installer, 'FORGE_ROOT', self.forge),
                  patch.object(installer, 'SOURCES_FILE', self.sources),
                  patch.object(installer, '_git_clone', side_effect=clone),
                  patch.object(installer, '_get_latest_sha', return_value='a' * 40),
                  patch.object(precheck, 'verify_repo', return_value={'ok': True, 'summary': 'pass', 'layers': []})]:
            p.start(); self.addCleanup(p.stop)

    def test_preview_includes_distinct_variants_and_excludes_fixtures(self):
        from daemon import module_install
        data = module_install.inspect_repo(self.repo, 'project')
        self.assertEqual({x['path'] for x in data}, {'skills/shared', 'configs/codex/skills/shared', 'skills/other', 'configs/gemini/skills/shared'})
        codex = next(x for x in data if x['path'].startswith('configs/codex'))
        self.assertEqual(codex['platform'], 'codex')
        self.assertEqual(codex['description'], 'Codex 工具')
        gemini = next(x for x in data if x['path'].startswith('configs/gemini'))
        self.assertEqual(gemini['platform'], 'other')

    def test_rejects_other_tool_variant_and_path_escape(self):
        from daemon import module_install
        for selection in ([{'path':'configs/gemini/skills/shared', 'target':'claude'}],
                          [{'path':'../outside', 'target':'codex'}]):
            result = module_install.install_selected('project', 'https://github.com/a/b', 'main', 'a'*40, selection)
            self.assertFalse(result['ok'])
        self.assertFalse((self.claude/'shared').exists())

    def test_preview_reads_multiline_module_description(self):
        from daemon import module_install
        entry = self.repo/'skills/other/SKILL.md'
        entry.write_text('---\nname: other\ndescription: >\n  查找论文和资料\n  整理研究笔记\n---\nbody', encoding='utf-8')
        other = next(x for x in module_install.inspect_repo(self.repo, 'project') if x['path']=='skills/other')
        self.assertIn('查找论文和资料', other['description'])

    def test_tool_name_in_skill_name_does_not_imply_tool_specific_version(self):
        from daemon import module_install
        path = self.repo/'skills/codex-helper'
        path.mkdir()
        (path/'SKILL.md').write_text('---\nname: codex-helper\ndescription: generic\n---\nbody', encoding='utf-8')
        item = next(x for x in module_install.inspect_repo(self.repo, 'project') if x['path']=='skills/codex-helper')
        self.assertEqual(item['platform'], 'generic')

    def test_selected_versions_install_independently_and_preserve_unselected(self):
        from daemon import module_install
        selected = [{'path':'skills/shared', 'target':'claude'},
                    {'path':'configs/codex/skills/shared', 'target':'codex'}]
        with patch.object(module_install, 'CODEX_ROOT', self.codex):
            result = module_install.install_selected('project', 'https://github.com/a/b', 'main', 'a'*40, selected)
        self.assertTrue(result['ok'], result)
        self.assertIn('通用工具', (self.claude/'shared/SKILL.md').read_text(encoding='utf-8'))
        self.assertIn('Codex 工具', (self.codex/'shared/SKILL.md').read_text(encoding='utf-8'))
        self.assertFalse((self.claude/'other').exists())
        self.assertIn('shared', json.loads(self.sources.read_text(encoding='utf-8')))
        codex_sources = self.forge/'templates/codex-sources.json'
        self.assertIn('shared', json.loads(codex_sources.read_text(encoding='utf-8')))
        from daemon.installed import installed_snapshot
        snapshots = [x for x in installed_snapshot(self.forge) if x['name']=='shared']
        self.assertEqual({x['tool'] for x in snapshots}, {'claude', 'codex'})

    def test_existing_independent_skill_is_not_overwritten_and_other_selected_continue(self):
        from daemon import module_install
        (self.claude/'shared').mkdir()
        (self.claude/'shared/SKILL.md').write_text('existing', encoding='utf-8')
        selected = [{'path':'skills/shared', 'target':'claude'}, {'path':'skills/other', 'target':'claude'}]
        with patch.object(module_install, 'CODEX_ROOT', self.codex):
            result = module_install.install_selected('project', 'https://github.com/a/b', 'main', 'a'*40, selected)
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['failed_modules']), 1)
        self.assertEqual((self.claude/'shared/SKILL.md').read_text(), 'existing')
        self.assertTrue((self.claude/'other/SKILL.md').is_file())

    def test_commit_change_requires_preview_again(self):
        from daemon import module_install
        result = module_install.install_selected('project', 'https://github.com/a/b', 'main', 'b'*40,
                                                 [{'path':'skills/other', 'target':'claude'}])
        self.assertFalse(result['ok'])
        self.assertFalse((self.claude/'other').exists())

    def test_selecting_root_does_not_install_unselected_nested_modules(self):
        from daemon import module_install
        (self.repo/'SKILL.md').write_text('---\nname: project\ndescription: root\n---\nroot', encoding='utf-8')
        (self.repo/'assets').mkdir()
        (self.repo/'assets'/'support.txt').write_text('needed', encoding='utf-8')
        with patch.object(module_install, 'CODEX_ROOT', self.codex):
            result = module_install.install_selected('project', 'https://github.com/a/b', 'main', 'a'*40,
                                                     [{'path':'.', 'target':'claude'}])
        self.assertTrue(result['ok'], result)
        self.assertTrue((self.claude/'project/assets/support.txt').is_file())
        self.assertFalse((self.claude/'project/skills/shared/SKILL.md').exists())
        self.assertFalse((self.claude/'project/configs/codex/skills/shared/SKILL.md').exists())
        record = json.loads(self.sources.read_text(encoding='utf-8'))['project']
        self.assertTrue(record.get('selection_mode'))
        result = installer.update_skill('project', record)
        self.assertTrue(result['ok'], result)
        self.assertFalse((self.claude/'project/skills/shared/SKILL.md').exists())

    def test_independent_codex_update_and_uninstall_leave_claude_untouched(self):
        from daemon import module_install
        selected = [{'path':'configs/codex/skills/shared', 'target':'codex'}]
        (self.claude/'shared').mkdir()
        (self.claude/'shared/SKILL.md').write_text('Claude personal copy', encoding='utf-8')
        with patch.object(module_install, 'CODEX_ROOT', self.codex):
            self.assertTrue(module_install.install_selected('project', 'https://github.com/a/b', 'main', 'a'*40, selected)['ok'])
            (self.repo/'configs/codex/skills/shared/SKILL.md').write_text('---\nname: shared\ndescription: updated\n---\nnew', encoding='utf-8')
            result = module_install.update_codex('shared')
            self.assertTrue(result['ok'], result)
            self.assertIn('updated', (self.codex/'shared/SKILL.md').read_text(encoding='utf-8'))
            self.assertTrue(module_install.uninstall_codex('shared')['ok'])
        self.assertFalse((self.codex/'shared').exists())
        self.assertEqual((self.claude/'shared/SKILL.md').read_text(encoding='utf-8'), 'Claude personal copy')

    def test_explicit_codex_version_replaces_only_owned_sync_link(self):
        from daemon import codex_sync, module_install
        (self.claude/'shared').mkdir()
        (self.claude/'shared/SKILL.md').write_text('Claude copy', encoding='utf-8')
        self.sources.write_text(json.dumps({'shared': {'type':'remote'}}), encoding='utf-8')
        (self.forge/'templates/codex-sync.json').write_text(json.dumps({'enabled':True, 'target_dir':str(self.codex)}))
        codex_sync._create_link(self.codex/'shared', self.claude/'shared')
        with patch.object(module_install, 'CODEX_ROOT', self.codex):
            result = module_install.install_selected('project', 'https://github.com/a/b', 'main', 'a'*40,
                [{'path':'configs/codex/skills/shared', 'target':'codex'}])
        self.assertTrue(result['ok'], result)
        self.assertIn('Codex 工具', (self.codex/'shared/SKILL.md').read_text(encoding='utf-8'))
        self.assertEqual((self.claude/'shared/SKILL.md').read_text(), 'Claude copy')
        report = codex_sync.sync_skills(self.claude, self.forge, ['shared'])
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['independent'], ['shared'])
        self.assertIn('Codex 工具', (self.codex/'shared/SKILL.md').read_text(encoding='utf-8'))


if __name__ == '__main__': unittest.main()
