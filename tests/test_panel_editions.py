"""Check edition labels and warnings in the actual panel JavaScript."""
import shutil
import subprocess
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(HERE))
from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER
from test_panel_xss import find_jsdom_base
from test_panel_install import PAYLOAD


def main():
    base=find_jsdom_base()
    if base is None:
        print('SKIP: jsdom unavailable'); return 2
    files=[]; harness=base/'panel_editions_harness.mjs'
    try:
        for edition in ('full','coreless'):
            payload=dict(PAYLOAD,_edition={'id':edition,'security_checks':edition=='full'})
            html=(ROOT/'panel/fallback.html').read_text(encoding='utf-8')
            html=html.replace('/* __DATA_PLACEHOLDER__ */',json_for_script(payload)).replace(KEY_PLACEHOLDER,json_for_script('test-key'))
            path=HERE/f'_panel_{edition}.html'; path.write_text(html,encoding='utf-8'); files.append(path)
        shutil.copyfile(HERE/'panel_editions_harness.mjs',harness)
        result=subprocess.run(['node',str(harness),*(str(p) for p in files)],cwd=base,capture_output=True,text=True,errors='replace',timeout=30)
        print(result.stdout)
        if result.stderr: print(result.stderr)
        return result.returncode
    finally:
        for path in files: path.unlink(missing_ok=True)
        harness.unlink(missing_ok=True)


if __name__=='__main__': sys.exit(main())
