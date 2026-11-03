#!/usr/bin/env python3
"""Exercise the real WinForms layout without displaying or entering credentials."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from daemon import credentials, secret_prompt


PROBE = r"""
$form.CreateControl()
$form.PerformLayout()
$form.Scale([System.Drawing.SizeF]::new($testScale, $testScale))
$form.PerformLayout()
function Bounds-InForm($control) {
    $left = 0; $top = 0; $current = $control
    while ($current -ne $form) {
        $left += $current.Left; $top += $current.Top
        $current = $current.Parent
    }
    return @{left=$left; top=$top; right=$left+$control.Width; bottom=$top+$control.Height}
}
function Label-Metrics($control) {
    $preferred = $control.GetPreferredSize([System.Drawing.Size]::new($control.Width, 0))
    return @{height=$control.Height; needed=$preferred.Height; bounds=(Bounds-InForm $control)}
}
$metadata = @{
    ok=$ok.Text; cancel=$cancel.Text; masked=$box.UseSystemPasswordChar
    promptText=$label.Text; hintText=$hintLabel.Text
    accept=($form.AcceptButton -eq $ok); escape=($form.CancelButton -eq $cancel)
    prompt=(Label-Metrics $label); input=(Bounds-InForm $box)
    hint=(Label-Metrics $hintLabel); okBounds=(Bounds-InForm $ok); cancelBounds=(Bounds-InForm $cancel)
    client=@{width=$form.ClientSize.Width; height=$form.ClientSize.Height}
}
[Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($metadata | ConvertTo-Json -Depth 5 -Compress))))
$form.Dispose()
exit 0
"""


def inspect_dialog(name, scale=1.0, legacy_codepage=False, texts=None):
    raw = secret_prompt.SCRIPT.read_bytes()
    # Windows PowerShell 5.1 uses the machine ANSI page for BOM-less scripts.
    # Force a common Chinese ANSI page so UTF-8 system settings cannot mask the regression.
    encoding = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') or not legacy_codepage else 'cp936'
    source = raw.decode(encoding, errors='replace')
    marker = '$result = $form.ShowDialog()'
    if source.count(marker) != 1:
        raise AssertionError('Expected one modal-dialog boundary to replace with a headless probe')
    source = source.replace(marker, PROBE)
    args = base64.b64encode(json.dumps(texts or secret_prompt.PROMPTS[name]).encode('utf-8')).decode()
    command = (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        f"$testScale={scale}; $argsData=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{args}'))); "
        '& {\n' + source + '\n} -Title $argsData[0] -Prompt $argsData[1] -Hint $argsData[2]'
    )
    encoded = base64.b64encode(command.encode('utf-16-le')).decode()
    result = subprocess.run(
        ['powershell', '-STA', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
        capture_output=True, text=True, errors='replace', timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if result.returncode:
        raise AssertionError('Headless WinForms probe failed: ' + result.stderr[:1200])
    return json.loads(base64.b64decode(result.stdout.strip()))


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'Windows PowerShell/WinForms required')
class SecretPromptLayoutTests(unittest.TestCase):
    def test_chinese_buttons_survive_legacy_windows_codepage(self):
        result = inspect_dialog(credentials.GITHUB_TOKEN, legacy_codepage=True)
        self.assertEqual(result['ok'], '确定')
        self.assertEqual(result['cancel'], '取消')

    def test_legacy_running_bridge_copy_fits_as_plain_text(self):
        result = inspect_dialog(credentials.GITHUB_TOKEN, texts=(
            'GitHub 个人访问令牌 (PAT)',
            '粘贴你的 GitHub Token。\n供 skill-forge **读取** GitHub API 使用。\n'
            '密钥保存在 Windows 凭据管理器。\n认证可减少匿名访问限流。\n'
            '来源检查可能因限流跳过。\nSkill Forge **不会**推送代码。\n'
            '仅按实际用途配置权限。\n留空时保留原配置。',
            '创建入口：GitHub → Settings → Developer settings → Personal access tokens。\n'
            '**只读**用途无需写权限。\n取消会保留已有配置。',
        ))
        self.assertNotIn('**', result['promptText'])
        self.assertNotIn('**', result['hintText'])
        self.assertIn('读取', result['promptText'])
        for label in ('prompt', 'hint'):
            self.assertGreaterEqual(result[label]['height'], result[label]['needed'])
        self.assertLessEqual(result['hint']['bounds']['bottom'], result['okBounds']['top'])
        self.assertLessEqual(result['okBounds']['bottom'], result['client']['height'])

    def test_both_prompts_fit_without_overlapping_at_common_scales(self):
        for name in (credentials.GITHUB_TOKEN, credentials.AI_TOKEN):
            for scale in (1.0, 1.5, 2.0):
                with self.subTest(name=name, scale=scale):
                    result = inspect_dialog(name, scale=scale)
                    for label in ('prompt', 'hint'):
                        self.assertGreaterEqual(result[label]['height'], result[label]['needed'])
                    self.assertLessEqual(result['prompt']['bounds']['bottom'], result['input']['top'])
                    self.assertLessEqual(result['input']['bottom'], result['hint']['bounds']['top'])
                    self.assertLessEqual(result['hint']['bounds']['bottom'], result['okBounds']['top'])
                    self.assertLessEqual(result['okBounds']['right'], result['cancelBounds']['left'])
                    for item in (result['prompt']['bounds'], result['hint']['bounds'], result['input'], result['okBounds'], result['cancelBounds']):
                        self.assertGreaterEqual(item['left'], 0)
                        self.assertGreaterEqual(item['top'], 0)
                        self.assertLessEqual(item['right'], result['client']['width'])
                        self.assertLessEqual(item['bottom'], result['client']['height'])
                    self.assertTrue(result['masked'])
                    self.assertTrue(result['accept'])
                    self.assertTrue(result['escape'])


class SecretPromptCopyTests(unittest.TestCase):
    def test_native_labels_do_not_contain_markdown_emphasis(self):
        for name, texts in secret_prompt.PROMPTS.items():
            with self.subTest(name=name):
                self.assertNotIn('**', '\n'.join(texts))


if __name__ == '__main__':
    unittest.main()
