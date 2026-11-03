import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');
let saved = [], releaseSave, fetchCalls = 0, failFetch = false;
const reply = x => Promise.resolve({ok:true, json:() => Promise.resolve(x)});
const dom = new JSDOM(html, {runScripts:'dangerously', url:'http://localhost/panel.html', beforeParse(w) {
  w.alert = () => {};
  w.confirm = () => true;
  w.fetch = (url, opts={}) => {
    const path = new URL(String(url)).pathname, method = opts.method || 'GET';
    if (path === '/ext-sources' && method === 'GET') return reply({sources:[]});
    if (path === '/ext-sources' && method === 'POST') {
      saved = JSON.parse(opts.body).sources;
      return new Promise(resolve => { releaseSave = () => resolve({ok:true,json:() => Promise.resolve({ok:true})}); });
    }
    if (path === '/ext-sources/fetch') {
      fetchCalls++;
      if (failFetch) return reply({ok:false,count:0,skills:[],errors:[{label:'测试源',error:'HTTP 503'}]});
      return reply({ok:true,count:1,skills:[{name:'imported-source',url:'https://github.com/a/skill',source:'external',description:'test'}],errors:[]});
    }
    if (path === '/' && method === 'GET') return reply({pending:[]});
    if (path === '/installed') return reply({ok:true,skills:[]});
    if (path === '/updates') return reply({ok:true,updates:[],checked_names:[]});
    return reply({ok:true});
  };
}});
const w=dom.window, d=w.document, pause=() => new Promise(r => setTimeout(r,80));
function check(ok, text) { if (!ok) throw new Error(text); console.log('PASS ' + text); }
await pause();
d.getElementById('btn-ext-sources').click(); await pause();
d.getElementById('ext-url').value='https://example.com/catalog.json';
d.getElementById('ext-label').value='测试源';
d.getElementById('btn-add-source').click();
d.getElementById('btn-fetch-sources').click(); await pause();
check(saved.length === 1 && fetchCalls === 0, 'waits for source save before fetching');
releaseSave(); await pause(); await pause();
check(fetchCalls === 1, 'fetches after successful save');
check(d.getElementById('skills-grid').textContent.includes('imported-source'), 'fetched skill appears in panel');
failFetch = true;
d.getElementById('btn-fetch-sources').click(); await pause(); releaseSave(); await pause(); await pause();
check(d.body.textContent.includes('HTTP 503'), 'source errors are shown instead of a success message');
console.log('RESULT: PASS');
dom.window.close();
