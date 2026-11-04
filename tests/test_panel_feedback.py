"""Run the feedback UI without network access or sending mail."""
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
        print('SKIP: jsdom unavailable')
        return 2
    page = HERE / '_panel_feedback.html'
    harness = base / 'panel_feedback_harness.mjs'
    try:
        payload = dict(PAYLOAD, _edition={'id': 'coreless'}, _private_fixture='LOCAL_DATA_NOT_FOR_FEEDBACK')
        html = (ROOT / 'panel/fallback.html').read_text(encoding='utf-8')
        html = html.replace('/* __DATA_PLACEHOLDER__ */', json_for_script(payload))
        html = html.replace(KEY_PLACEHOLDER, json_for_script('PRIVATE-BRIDGE-KEY'))
        page.write_text(html, encoding='utf-8')
        shutil.copyfile(HERE / 'panel_feedback_harness.mjs', harness)
        result = subprocess.run(['node', str(harness), str(page)], cwd=base,
                                capture_output=True, text=True, errors='replace', timeout=30)
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        return result.returncode
    finally:
        page.unlink(missing_ok=True)
        harness.unlink(missing_ok=True)


if __name__ == '__main__':
    sys.exit(main())
