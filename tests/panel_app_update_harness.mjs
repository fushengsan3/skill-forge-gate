import fs from 'fs';
import {JSDOM,VirtualConsole} from 'jsdom';
const errors=[],calls=[],vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e));
let state={ok:true,current_version:'2026.11.04',edition:'coreless',available:true,latest_version:'2026.11.19',tag:'v2026.11.19',notes:'<img src=x onerror="window.bad=true">新版说明',phase:'idle',deferred:''};
const dom=new JSDOM(fs.readFileSync(process.argv[2],'utf8'),{url:'http://localhost/panel.html',runScripts:'dangerously',virtualConsole:vc,beforeParse(w){
 w.fetch=async(url,opts={})=>{
  const path=new URL(url).pathname;calls.push({path,body:opts.body});
  if(path==='/app-update/defer')state={...state,deferred:JSON.parse(opts.body).tag};
  if(path==='/app-update/install')state={...state,phase:'failed',install_error:'升级未完成，当前版本已保留。'};
  return {ok:true,json:async()=>path.startsWith('/app-update')?{...state}:{ok:true,skills:[],pending:[],sources:[],updates:[],checked_names:[]}};
 };
}});
const w=dom.window,d=w.document;let fails=0;
const pause=()=>new Promise(r=>setTimeout(r,70));
function check(ok,label){console.log((ok?'PASS ':'FAIL ')+label);if(!ok)fails++;}
await pause();
check(!!d.getElementById('btn-app-check'),'manager upgrade controls exist');
if(!d.getElementById('btn-app-check')){dom.window.close();process.exit(1);}
check(d.getElementById('app-update-status').textContent.includes('2026.11.04'),'shows current application version');
check(!d.getElementById('app-update-offer').hidden,'offers published update without auto installing');
check(!calls.some(c=>c.path==='/app-update/install'),'opening panel never installs an update');
check(!d.querySelector('#app-update-notes img')&&!w.bad,'release notes are plain text');
d.getElementById('btn-app-defer').click();await pause();
check(d.getElementById('app-update-offer').hidden,'keep current version hides the offer');
check(state.current_version==='2026.11.04'&&!calls.some(c=>c.path==='/app-update/install'),'cancel preserves current version');
await w.refreshAppUpdate(false);await pause();
check(d.getElementById('app-update-offer').hidden,'the same deferred release stays hidden');
d.getElementById('btn-app-check').click();await pause();
check(!d.getElementById('app-update-offer').hidden,'manual check can reopen a deferred release');
d.getElementById('btn-app-install').click();await pause();
check(calls.filter(c=>c.path==='/app-update/install').length===1,'update starts only after explicit selection');
check(JSON.parse(calls.find(c=>c.path==='/app-update/install').body).tag==='v2026.11.19','installation is pinned to the displayed release');
check(d.getElementById('app-update-status').textContent.includes('保留'),'failed upgrade displays recovery result');
d.getElementById('btn-en').click();
check(d.getElementById('btn-app-check').textContent==='Check workbench update','upgrade controls support English');
check(errors.length===0,'no JavaScript runtime errors');dom.window.close();
const legacyCalls=[];
const legacy=new JSDOM(fs.readFileSync(process.argv[2],'utf8'),{url:'http://localhost/old-panel.html',runScripts:'dangerously',virtualConsole:vc,beforeParse(win){
 win.fetch=async(url,opts={})=>{legacyCalls.push({path:new URL(url).pathname,method:opts.method||'GET'});return {ok:true,json:async()=>({ok:true,pending:[],skills:[],sources:[],updates:[]})};};
}});
await pause();legacy.window.document.getElementById('btn-app-check').click();await pause();
check(legacy.window.document.getElementById('btn-app-check').disabled,'old bridge cannot start unsupported update actions');
check(!legacyCalls.some(c=>c.path.startsWith('/app-update')&&c.method==='POST'),'no upgrade POST is sent to an old bridge');
legacy.window.close();if(fails)process.exit(1);
