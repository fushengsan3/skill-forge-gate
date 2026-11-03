"""Exercise third-party source management through the panel."""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER
from test_panel_xss import find_jsdom_base
from test_panel_install import PAYLOAD


def main():
    base = find_jsdom_base()
    if base is None:
        print('SKIP: jsdom 未安装'); return 2
    template = (ROOT/'panel/fallback.html').read_text(encoding='utf-8')
    template = template.replace('/* __DATA_PLACEHOLDER__ */', json_for_script(PAYLOAD))
    template = template.replace(KEY_PLACEHOLDER, json_for_script('test-key'))
    html = HERE/'_panel_external_sources.html'
    harness = base/'panel_external_sources_harness.mjs'
    try:
        html.write_text(template, encoding='utf-8')
        shutil.copyfile(HERE/'panel_external_sources_harness.mjs', harness)
        proc = subprocess.run(['node', str(harness), str(html)], cwd=base,
                              capture_output=True, text=True, errors='replace')
        print(proc.stdout)
        if proc.stderr: print(proc.stderr)
        return proc.returncode
    finally:
        html.unlink(missing_ok=True)
        harness.unlink(missing_ok=True)


if __name__ == '__main__': sys.exit(main())
