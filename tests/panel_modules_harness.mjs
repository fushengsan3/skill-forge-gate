import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');
let saved = [], installs = 0, searchCalls = 0, searchBody = null, lastUninstall = null, historyRequest = null, rollbackRequest = null;
const responses = {
  ok:true, branch:'main', sha:'a'.repeat(40), modules:[
    {name:'shared', path:'skills/shared', platform:'generic', codex_supported:true, description:'通用功能'},
    {name:'shared', path:'configs/codex/skills/shared', platform:'codex', codex_supported:true, description:'Codex 功能 <img src=x onerror=alert(1)>'},
    {name:'other', path:'skills/other', platform:'generic', codex_supported:true, description:'其他能力'}]
};
const dom = new JSDOM(html, {runScripts:'dangerously', url:'http://localhost/panel.html', beforeParse(w) {
  w.alert = () => {};
  w.confirm = () => true;
  w.fetch = (url, opts={}) => {
    const path = new URL(String(url)).pathname, method = opts.method || 'GET';
    const reply = x => Promise.resolve({ok:true, json:() => Promise.resolve(x)});
    if (path === '/preview-install') return reply(responses);
    if (path === '/ai-search') { searchCalls++; searchBody = JSON.parse(opts.body); return reply({ok:true,scope:'online',mode:'ai',skills:[{name:'fresh-online-skill',description:'绘图',url:'https://github.com/example/fresh',source:'search'}]}); }
    if (path === '/' && method === 'POST') { saved = JSON.parse(opts.body).pending; return reply({ok:true}); }
    if (path === '/' && method === 'GET') return reply({pending:[]});
    if (path === '/installed') return reply({ok:true,skills:[]});
    if (path === '/updates') return reply({ok:true,updates:[],checked_names:[],partial:false});
    if (path === '/uninstall') { lastUninstall = JSON.parse(opts.body); return reply({ok:true}); }
    if (path === '/version-history') { historyRequest = JSON.parse(opts.body); return reply({ok:true, warning:'', commits:[{sha:'b'.repeat(40),date:'2026-01-01',message:'old version'}], backups:[{name:'shared-20260101-120000',sha:''}]}); }
    if (path === '/rollback') { rollbackRequest = JSON.parse(opts.body); return reply({ok:true,version_unknown:true}); }
    if (path === '/install') {
      installs++;
      if (installs === 1) {
        const pending = [{...saved[0], selected_modules:[saved[0].selected_modules[1]]}];
        return reply({ok:true,processed:1,success:0,failed:1,pending,results:[{name:'example',ok:false,
          modules:[{name:'shared',target:'claude',ok:true,verify:{summary:'通过',layers:[]}},
                   {name:'shared',target:'codex',ok:false,error:'L3 拒绝',verify:{layers:[{name:'L3 内容',verdict:'REJECT',reason:'详细原因'}]}}]}]});
      }
      return reply({ok:true,processed:1,success:1,failed:0,pending:[],results:[]});
    }
    return Promise.reject(new Error('unexpected fetch: ' + path));
  };
}});
const w=dom.window, d=w.document, pause=() => new Promise(r => setTimeout(r,80));
function check(condition, message) { if (!condition) throw new Error(message); console.log('PASS ' + message); }
await pause();
w.beginInstallPreview('example','https://github.com/a/b'); await pause();
check(d.getElementById('module-modal').classList.contains('open'), 'first install opens module selector');
check(d.getElementById('module-list').textContent.includes('Codex 功能'), 'functions and tool variants are shown');
check(!d.querySelector('#module-list img'), 'repository descriptions render as text, not HTML');
// Deselect the unrelated module; keep the default Claude and Codex variants.
const rows = [...d.querySelectorAll('#module-list .module-row')];
rows[2].querySelector('input').checked = false;
d.getElementById('btn-confirm-modules').click(); await pause();
check(saved[0].selected_modules.length === 2, 'only selected tool versions enter the queue');
check(saved[0].selected_modules.some(x => x.path.startsWith('configs/codex') && x.target === 'codex'), 'Codex selection is explicit');
w.triggerInstall(); await pause(); await pause();
const result=d.getElementById('install-result-modal');
check(result.classList.contains('open') && result.textContent.includes('L3 拒绝'), 'brief failure shown in result dialog');
const detail=result.querySelector('details');
check(detail && !detail.open && detail.textContent.includes('详细原因'), 'detailed reason is collapsed by default');
check(d.getElementById('btn-continue-install').style.display !== 'none', 'retry is offered for failed modules');
d.getElementById('btn-continue-install').click(); await pause(); await pause();
check(installs === 2 && saved[0].selected_modules.length === 1, 'retry retains only the failed selection');
d.getElementById('search-input').value='需要绘图';
d.getElementById('btn-ai-search').click(); await pause();
check(searchCalls === 1 && d.getElementById('search-status').textContent.includes('线上') && d.getElementById('search-status').textContent.includes('AI 匹配'), 'online search reports its source and ranking mode');
check(searchBody && !('candidates' in searchBody), 'online search does not send the old local catalogue');
check(d.getElementById('skills-grid').textContent.includes('fresh-online-skill') && !d.getElementById('skills-grid').textContent.includes('alpha-skill'), 'online search shows newly fetched skills');
d.getElementById('search-input').value='';
d.getElementById('search-input').dispatchEvent(new w.Event('input'));
check(d.getElementById('skills-grid').textContent.includes('alpha-skill') && !d.getElementById('skills-grid').textContent.includes('fresh-online-skill'), 'clearing search restores the existing catalogue');
w.renderInstalledList([{name:'shared',tool:'claude',type:'remote'}, {name:'shared',tool:'codex',type:'remote'}], false);
const installedRows = [...d.querySelectorAll('.installed-item')];
check(installedRows[0].textContent.includes('Claude Code') && installedRows[1].textContent.includes('Codex 独立版本'), 'installed versions show their target tools');
installedRows[1].querySelector('.btn-ghost').click(); await pause();
check(historyRequest && historyRequest.tool === 'codex', 'history request identifies the selected tool');
check(d.getElementById('version-modal').textContent.includes('版本未知'), 'legacy backups are marked version unknown');
[...d.querySelectorAll('#version-list button')].find(b => b.textContent === '回退').click(); await pause();
check(rollbackRequest && rollbackRequest.tool === 'codex' && rollbackRequest.backup === 'shared-20260101-120000', 'rollback identifies the selected backup and tool');
// Refreshing after rollback empties the mocked installed list; recreate it for uninstall.
w.renderInstalledList([{name:'shared',tool:'claude',type:'remote'}, {name:'shared',tool:'codex',type:'remote'}], false);
const installedAfterRollback = [...d.querySelectorAll('.installed-item')];
installedAfterRollback[1].querySelector('.btn-uninstall').click(); await pause();
check(lastUninstall && lastUninstall.tool === 'codex', 'uninstall identifies the Codex version');
w.queueInstall('legacy', 'https://github.com/a/b'); await pause();
w.triggerInstall(); await pause();
check(d.getElementById('module-modal').classList.contains('open') && installs === 2, 'legacy queue item waits for module selection');
console.log('RESULT: PASS');
dom.window.close();
