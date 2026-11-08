"""Regression coverage for all visible cards and explicit online result sorting."""
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

def main():
    base=find_jsdom_base()
    if base is None: return 2
    def item(name, desc, zh, stars, score, status='new'):
        return dict(name=name,description=desc,description_zh=zh,stars=stars,score=score,
                    status=status,source='github-topic',url='https://github.com/example/'+name)
    payload={'generated':'2026-11-08','categories':{
        '文档写作':[item('pdf-tool','Read documents','阅读文档',10,90)],
        '代码质量':[item('lint-tool','Lint source code','检查代码',50,20,'update_available')]},
        '_sections':{'学术相关':{'icon':'','count':2,'items':[
            item('paper-tool','Read papers','阅读论文',20,95),item('citation-tool','Manage citations','整理引用',100,5,'update_available')]}}}
    page=HERE/'_panel_filters.html'; harness=base/'panel_filters_harness.mjs'
    try:
        html=(ROOT/'panel/fallback.html').read_text(encoding='utf-8')
        page.write_text(html.replace('/* __DATA_PLACEHOLDER__ */',json_for_script(payload)).replace(KEY_PLACEHOLDER,json_for_script('test-key')),encoding='utf-8')
        shutil.copyfile(HERE/'panel_filters_harness.mjs',harness)
        result=subprocess.run(['node',str(harness),str(page)],cwd=base,capture_output=True,text=True,errors='replace',timeout=30)
        print(result.stdout);print(result.stderr)
        return result.returncode
    finally:
        page.unlink(missing_ok=True);harness.unlink(missing_ok=True)

if __name__=='__main__':sys.exit(main())
