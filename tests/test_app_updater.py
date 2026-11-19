"""Manager upgrades preserve user state and never cross distribution editions."""
import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from daemon import app_updater

ROOT = Path(__file__).resolve().parents[1]


class AppUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'forge'
        for folder in ('daemon', 'panel', 'scripts', 'templates', 'discover'):
            (self.root / folder).mkdir(parents=True)
        (self.root / 'daemon/app_version.py').write_text('VERSION = "2026.11.04"\nCOMMIT = ""\n')
        (self.root / 'daemon/edition.py').write_text('ID = "full"\n')
        (self.root / 'SKILL.md').write_text('old application')
        (self.root / 'sources.json').write_text('{"personal":{"url":"private"}}')
        (self.root / 'templates/.bridge-key').write_text('local-only-key')
        (self.root / 'templates/translate-config.json').write_text('{"model":"keep"}')
        (self.root / 'discover/latest.html').write_text('const DISCOVER_DATA = {"categories":{},"total":7};\nconst BRIDGE_KEY = "local-only-key";')
        self.release = {'tag_name':'v2026.11.19', 'body':'New panel', 'draft':False, 'prerelease':False,
                        'html_url':'https://github.com/fushengsan3/skill-forge-gate/releases/tag/v2026.11.19',
                        'assets':[{'name':'skill-forge-gate-full.zip','size':1000,
                        'browser_download_url':'https://github.com/fushengsan3/skill-forge-gate/releases/download/v2026.11.19/skill-forge-gate-full.zip'}]}

    def package(self, edition='full', extra=None):
        output = Path(self.temp.name) / 'package.zip'
        files = {'SKILL.md':'new application', 'daemon/app_version.py':'VERSION = "2026.11.19"\nCOMMIT = "'+'a'*40+'"\n',
                 'daemon/edition.py':'ID = "'+edition+'"\n','daemon/queue_bridge.py':'# bridge\n',
                 'scripts/data-paths.txt':(ROOT/'scripts/data-paths.txt').read_text(encoding='utf-8'),
                 'panel/fallback.html':'const DISCOVER_DATA = /* __DATA_PLACEHOLDER__ */;\nconst BRIDGE_KEY = /* __BRIDGE_KEY_PLACEHOLDER__ */;',
                 'sources.json':'{}','templates/translate-config.json':'{}'}
        from daemon.bridge_auth import KEY_PLACEHOLDER
        files['panel/fallback.html']=files['panel/fallback.html'].replace('/* __BRIDGE_KEY_PLACEHOLDER__ */',KEY_PLACEHOLDER)
        with zipfile.ZipFile(output,'w') as archive:
            for rel, body in files.items():archive.writestr('skill-forge/'+rel,body)
            if extra:archive.writestr(extra,'bad')
        return output

    def check(self):
        with patch.object(app_updater, '_fetch_release', return_value=self.release):
            return app_updater.check(self.root, force=True)

    def test_check_and_defer_do_not_install_or_change_current_version(self):
        before=(self.root/'SKILL.md').read_bytes()
        status=self.check()
        self.assertTrue(status['available'])
        self.assertEqual(status['current_version'],'2026.11.04')
        self.assertEqual(app_updater.defer(self.root,'v2026.11.19')['deferred'],'v2026.11.19')
        self.assertTrue(self.check()['available'])
        self.assertEqual(app_updater.status(self.root)['deferred'],'v2026.11.19')
        self.assertEqual((self.root/'SKILL.md').read_bytes(),before)

    def test_missing_matching_asset_and_older_release_are_not_installable(self):
        self.release['assets'][0]['name']='skill-forge-gate-coreless.zip'
        self.assertFalse(self.check()['available'])
        self.assertTrue(self.check()['error'])
        self.release['tag_name']='v2026.11.01'
        self.assertFalse(self.check()['available'])

    def test_network_error_preserves_last_successful_check(self):
        self.check()
        with patch.object(app_updater, '_fetch_release', side_effect=OSError('offline')):
            status=app_updater.check(self.root,force=True)
        self.assertTrue(status['available'])
        self.assertTrue(status['error'])
        self.assertTrue(status['checked_at'])

    def test_upgrade_preserves_data_and_rebuilds_panel(self):
        self.check()
        result=app_updater.apply_package(self.root,self.package(),'v2026.11.19')
        self.assertEqual(result['current_version'],'2026.11.19')
        self.assertEqual((self.root/'SKILL.md').read_text(),'new application')
        self.assertEqual(json.loads((self.root/'sources.json').read_text())['personal']['url'],'private')
        self.assertEqual(json.loads((self.root/'templates/translate-config.json').read_text()),{'model':'keep'})
        self.assertEqual((self.root/'templates/.bridge-key').read_text(),'local-only-key')
        panel=(self.root/'discover/latest.html').read_text()
        self.assertIn('local-only-key',panel)
        self.assertIn('"total": 7',panel)
        self.assertTrue(list((self.root/'.backup').glob('deploy-*')))

    def test_previous_code_reclassified_as_user_data_is_not_deleted(self):
        (self.root/'.deploy-manifest.json').write_text(json.dumps({'files':['templates/translate-config.json']}))
        app_updater.apply_package(self.root,self.package(),'v2026.11.19')
        self.assertEqual(json.loads((self.root/'templates/translate-config.json').read_text()),{'model':'keep'})

    def test_explicit_upgrade_worker_downloads_and_installs_offered_release(self):
        self.check(); package=self.package();done=threading.Event()
        def ready():
            app_updater.OPERATION_LOCK.release();done.set()
        with patch.object(app_updater,'_fetch_release',return_value=self.release), \
             patch.object(app_updater,'_download',side_effect=lambda release,destination:shutil.copy2(package,destination)):
            result=app_updater.start_install(self.root,'v2026.11.19',on_ready=ready)
            self.assertTrue(result['ok']);self.assertTrue(done.wait(5))
        self.assertEqual(app_updater.status(self.root)['current_version'],'2026.11.19')
        self.assertTrue(app_updater.status(self.root)['restart_required'])
        app_updater._save(self.root, install_error='restart needed')
        app_updater.mark_started(self.root)
        self.assertFalse(app_updater.status(self.root)['restart_required'])
        self.assertEqual(app_updater.status(self.root)['install_error'],'')

    def test_install_requires_matching_offer_and_no_active_mutation(self):
        self.check()
        self.assertFalse(app_updater.start_install(self.root,'v2099.01.01')['ok'])
        app_updater.OPERATION_LOCK.acquire()
        try:self.assertFalse(app_updater.start_install(self.root,'v2026.11.19')['ok'])
        finally:app_updater.OPERATION_LOCK.release()

    def test_coreless_upgrade_keeps_coreless_distribution(self):
        (self.root/'daemon/edition.py').write_text('ID = "coreless"\n')
        result=app_updater.apply_package(self.root,self.package('coreless'),'v2026.11.19')
        self.assertEqual(result['edition'],'coreless')
        self.assertFalse((self.root/'verify').exists())

    def test_wrong_edition_and_zip_escape_rejected_before_replacement(self):
        for package in (self.package('coreless'),):
            with self.assertRaises(ValueError):app_updater.apply_package(self.root,package,'v2026.11.19')
        with self.assertRaises(ValueError):
            app_updater.apply_package(self.root,self.package(extra='skill-forge/../../outside.txt'),'v2026.11.19')
        self.assertEqual((self.root/'SKILL.md').read_text(),'old application')
        self.assertFalse((Path(self.temp.name)/'outside.txt').exists())

    def test_failed_replacement_restores_previous_files_and_data(self):
        package=self.package()
        original=shutil.copy2
        def failing_copy(src,dst,*args,**kwargs):
            if Path(dst)==self.root/'SKILL.md' and Path(src).parent.name=='skill-forge':
                raise OSError('simulated disk failure')
            return original(src,dst,*args,**kwargs)
        with patch('shutil.copy2',side_effect=failing_copy):
            with self.assertRaises(OSError):app_updater.apply_package(self.root,package,'v2026.11.19')
        self.assertEqual((self.root/'SKILL.md').read_text(),'old application')
        self.assertEqual((self.root/'templates/.bridge-key').read_text(),'local-only-key')


if __name__=='__main__':unittest.main()
