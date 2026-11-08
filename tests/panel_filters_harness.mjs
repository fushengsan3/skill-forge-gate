import fs from 'fs';
import {JSDOM,VirtualConsole} from 'jsdom';
const errors=[],vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e));
const dom=new JSDOM(fs.readFileSync(process.argv[2],'utf8'),{url:'http://localhost/panel.html',runScripts:'dangerously',virtualConsole:vc,beforeParse(w){
 w.fetch=async(url)=>({ok:true,json:async()=>String(url).includes('ai-search')?{ok:true,mode:'ai',skills:[
  {name:'semantic-first',description:'Create images',description_zh:'生成图片',stars:1,score:99,status:'new',category:'设计'},
  {name:'most-stars',description:'Draw diagrams',description_zh:'绘制图表',stars:500,score:1,status:'new',category:'设计'}
 ]}:{ok:true,skills:[],pending:[],sources:[],updates:[],checked_names:[],partial:true}});
}});
const w=dom.window,d=w.document;let fails=0;
const pause=()=>new Promise(resolve=>setTimeout(resolve,70));
function check(ok,label){console.log((ok?'PASS ':'FAIL ')+label);if(!ok)fails++;}
function change(id,value,event='change'){const el=d.getElementById(id);el.value=value;el.dispatchEvent(new w.Event(event,{bubbles:true}));}
function names(selector){return [...d.querySelectorAll(selector+' .card-title')].map(x=>x.textContent);}
await pause();
change('search-input','  阅读  ','input');
check(names('#skills-grid').length===1 && names('#skills-grid')[0].includes('pdf-tool'),'Chinese keyword matches translated discovery text and ignores outer spaces');
check(names('#sections-panel').length===1 && names('#sections-panel')[0].includes('paper-tool'),'keyword filters recommendation boards');
change('search-input','','input');change('filter-category','代码质量');
check(names('#skills-grid').length===1 && names('#sections-panel').length===0,'category applies to all visible cards');
change('filter-category','all');change('filter-status','update_available');
check(names('#skills-grid').length===1 && names('#sections-panel').length===1 && names('#sections-panel')[0].includes('citation-tool'),'status applies to both lists');
change('filter-status','all');change('filter-sort','stars');
check(names('#skills-grid')[0].includes('lint-tool') && names('#sections-panel')[0].includes('citation-tool'),'star sorting applies to both lists');
change('filter-sort','score');
check(names('#skills-grid')[0].includes('pdf-tool') && names('#sections-panel')[0].includes('paper-tool'),'score sorting applies to both lists');
change('search-input','论文','input');
check(d.getElementById('empty-state').style.display==='none' && names('#sections-panel').length===1,'board-only matches do not show an empty result warning');
change('search-input','no-such-skill-unique','input');
check(names('#skills-grid').length===0 && names('#sections-panel').length===0 && d.getElementById('empty-state').style.display==='block','no matches clears every visible list');
change('search-input','图片','input');d.getElementById('btn-ai-search').click();await pause();
check(d.getElementById('filter-sort').value==='relevance','online results identify relevance sort explicitly');
change('filter-sort','stars');
check(names('#skills-grid')[0].includes('most-stars'),'explicit sorting overrides AI ranking');
change('filter-category','设计');
check(names('#skills-grid').length===2,'online-only categories are selectable');
change('search-input','','input');
check(d.getElementById('filter-sort').value!=='relevance','clearing online search restores valid local sort');
check(errors.length===0,'no JavaScript runtime errors');dom.window.close();if(fails)process.exit(1);
