import fs from 'fs';
import {JSDOM, VirtualConsole} from 'jsdom';
const pause = () => new Promise(resolve => setTimeout(resolve, 60));
function check(value, message) { if (!value) throw new Error(message); console.log('PASS ' + message); }
async function open(file, onlineEdition) {
  const errors = [], vc = new VirtualConsole(); vc.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(fs.readFileSync(file, 'utf8'), {url:'http://localhost/panel.html', runScripts:'dangerously', virtualConsole:vc,
    beforeParse(w) {
      w.alert=()=>{}; w.confirm=()=>true;
      w.fetch=(url)=> {
        if (!onlineEdition) return Promise.reject(new Error('offline fixture'));
        const path=new URL(String(url)).pathname;
        const data=path==='/installed' ? {ok:true,skills:[],edition:{id:onlineEdition,security_checks:onlineEdition==='full'}} :
          path==='/ext-sources' ? {ok:true,sources:[]} : path==='/updates' ? {ok:true,updates:[],checked_names:[]} : {ok:true,pending:[]};
        return Promise.resolve({ok:true,json:()=>Promise.resolve(data)});
      };
    }});
  await pause(); return {dom,w:dom.window,d:dom.window.document,errors};
}
for (const [file, edition] of [[process.argv[2],'full'],[process.argv[3],'coreless']]) {
  const {dom,w,d,errors}=await open(file);
  check(d.getElementById('edition-badge')?.textContent === (edition==='full'?'完整版':'无核版'), edition+' offline edition badge');
  check(d.getElementById('edition-notice').hidden === (edition==='full'), edition+' safety notice visibility');
  if (edition==='coreless') {
    check(d.title.includes('无核版'), 'coreless window title');
    check(!d.querySelector('[data-lang="inspector_step3"]').textContent.includes('验证'), 'coreless install steps do not claim verification');
    check(d.getElementById('edition-notice').textContent.includes('不执行'), 'coreless explains no security verification');
    d.getElementById('btn-en').click();
    check(d.getElementById('edition-badge').textContent==='Coreless', 'coreless English badge');
    check(!d.querySelector('[data-lang="inspector_step3"]').textContent.toLowerCase().includes('verify'), 'coreless English install steps');
  }
  check(errors.length===0, edition+' no runtime errors'); dom.window.close();
}
const live=await open(process.argv[2], 'coreless');
check(live.d.getElementById('edition-badge').textContent==='无核版','connected bridge corrects a stale full-edition page');
check(!live.d.getElementById('edition-notice').hidden,'connected coreless service exposes its boundary');
live.dom.window.close();
