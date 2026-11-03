"""Background Git and Docker processes must not allocate a Windows console."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daemon import installer, precheck, privacy
from verify import l5_sandbox


@unittest.skipUnless(hasattr(subprocess, 'CREATE_NO_WINDOW'), 'Windows-specific behavior')
class BackgroundProcessesTests(unittest.TestCase):
    def assert_hidden(self, call):
        self.assertTrue(call.kwargs.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW)

    def test_git_clone_and_sha_are_hidden(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(installer.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(stdout='abc123\n', stderr='', returncode=0)
            self.assertTrue(installer._git_clone('https://github.com/example/repo.git', Path(temp) / 'repo')[0])
            self.assertEqual(installer._get_latest_sha(Path(temp)), 'abc123')
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assert_hidden(call)

    def test_docker_precheck_is_hidden(self):
        with patch.object(precheck.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0)
            self.assertTrue(precheck._docker_available()[0])
            self.assert_hidden(run.call_args)

    def test_docker_build_run_and_timeout_cleanup_are_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / 'SKILL.md').write_text('demo', encoding='utf-8')
            def run_side_effect(args, **kwargs):
                if args[1] == 'run':
                    raise subprocess.TimeoutExpired(args, 1)
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            with patch.object(privacy, 'allow_remote_audit', return_value=True), \
                 patch.object(l5_sandbox.subprocess, 'run', side_effect=run_side_effect) as run, \
                 patch.object(l5_sandbox, 'api_credentials', return_value=('test-key', 'test-style', 'test')):
                result = l5_sandbox.run_docker_sandbox(temp, [])
            self.assertEqual(result['verdict'], 'ERROR')
            self.assertEqual([call.args[0][1] for call in run.call_args_list], ['build', 'run', 'kill'])
            for call in run.call_args_list:
                self.assert_hidden(call)


if __name__ == '__main__':
    unittest.main()
