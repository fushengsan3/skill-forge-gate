"""Run the workbench upgrade UI against an isolated fake bridge."""
import shutil
import subprocess
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(HERE))
from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER
from test_panel_xss import find_jsdom_base

def main():
    base=find_jsdom_base()
    if base is None:return 2
    page=HERE/'_panel_app_update.html';harness=base/'panel_app_update_harness.mjs'
    try:
        payload={'categories':{},'_app_update':{'ok':True,'current_version':'2026.11.04','edition':'coreless'}}
        html=(ROOT/'panel/fallback.html').read_text(encoding='utf-8')
        page.write_text(html.replace('/* __DATA_PLACEHOLDER__ */',json_for_script(payload)).replace(KEY_PLACEHOLDER,json_for_script('test-key')),encoding='utf-8')
        shutil.copyfile(HERE/'panel_app_update_harness.mjs',harness)
        p=subprocess.run(['node',str(harness),str(page)],cwd=base,capture_output=True,text=True,errors='replace',timeout=30)
        print(p.stdout);print(p.stderr);return p.returncode
    finally:
        page.unlink(missing_ok=True);harness.unlink(missing_ok=True)

if __name__=='__main__':sys.exit(main())
