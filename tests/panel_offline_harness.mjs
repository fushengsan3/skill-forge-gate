// R4-a 回归测试的执行端：bridge **离线**时，面板仍要显示已安装名单。
// 由 tests/test_panel_offline.py 调用，本身不负责生成 HTML。
//
// 用法: node panel_offline_harness.mjs <带_installed的.html> <不带_installed的.html>
// 退出码 0 = 全部通过；1 = 有断言失败。
import fs from 'fs';
import { JSDOM } from 'jsdom';

const withEmbedHtml = fs.readFileSync(process.argv[2], 'utf8');
const bareHtml = fs.readFileSync(process.argv[3], 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

// 用户报的那个 BUG 的原文。修好之后这句话不该再显示给用户。
const OLD_BUG_TEXT = '无法加载安装列表';

// 只看"用户看得见的"文本。
// 注意：body.textContent 会把 <script>/<style> 的内容也算进来（脚本里的注释、
// i18n 词表都算），拿它做"页面上不该出现某句话"的断言会误报。
function visibleText(d) {
  const clone = d.body.cloneNode(true);
  clone.querySelectorAll('script,style').forEach((n) => n.remove());
  return clone.textContent;
}

function makeDom(html, fetchImpl, onReady) {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://localhost/panel.html',
    beforeParse(window) {
      window.alert = (m) => { window.__lastAlert = String(m); };
      window.confirm = () => true;      // 假装用户点了"确定"
      window.fetch = fetchImpl;
    },
  });
  if (onReady) onReady(dom.window);
  return dom;
}

const offline = () => Promise.reject(new Error('ECONNREFUSED'));
const itemsIn = (w) => w.document.querySelectorAll('.installed-item').length;

// ---------------------------------------------------------------
// 场景 1：bridge 离线，但页面里嵌了名单 → 名单必须照样显示
// ---------------------------------------------------------------
const dom1 = makeDom(withEmbedHtml, offline);

setTimeout(() => {
  const w = dom1.window;
  const d = w.document;

  w.loadInstalled();   // 已装面板默认收起，主动触发一次渲染

  setTimeout(() => {
    check('① 离线时仍渲染出嵌入的名单', itemsIn(w) === 2, itemsIn(w) + ' 项');
    check('② 角标数量来自嵌入数据', d.getElementById('installed-count').textContent === '2',
      d.getElementById('installed-count').textContent);
    check('③ 有"这是快照"的提示', !!d.querySelector('.installed-note.stale'));
    check('④ 不再出现"Bridge 未运行，无法加载安装列表"',
      visibleText(d).indexOf(OLD_BUG_TEXT) < 0,
      visibleText(d).indexOf(OLD_BUG_TEXT) >= 0 ? '页面上仍可见该句' : '');
    check('⑤ 没有误报成"无已安装"', !d.querySelector('.installed-note:not(.stale):not(.warn)'));

    // R4-d：离线卸载必须明说做不到，而不是静默失败
    const unBtn = d.querySelector('.btn-uninstall');
    let threw = null;
    try { unBtn.dispatchEvent(new w.Event('click')); } catch (e) { threw = e; }
    setTimeout(() => {
      const toast = d.querySelector('#toast-container .toast');
      check('⑥ 离线卸载：不抛错', threw === null, threw ? String(threw) : '');
      check('⑦ 离线卸载：明确提示做不到',
        !!toast && toast.textContent.indexOf('无法卸载') >= 0,
        toast ? toast.textContent : '(无 toast)');

      runScenario2();
    }, 80);
  }, 120);
}, 60);

// ---------------------------------------------------------------
// 场景 2：先拿到实时数据，随后 bridge 掉线 → 不得回退到旧快照
// ---------------------------------------------------------------
const FRESH_SKILLS = [
  { name: 'alpha-skill', installed_sha: 'aaaaaaaa', installed_at: '2026-02-01T00:00:00', type: 'skill' },
  { name: 'beta-skill', installed_sha: 'bbbbbbbb', installed_at: '2026-02-02T00:00:00', type: 'skill' },
  { name: 'gamma-skill', installed_sha: 'cccccccc', installed_at: '2026-02-03T00:00:00', type: 'skill' },
];

function runScenario2() {
  let bridgeUp = true;
  const flaky = (url) => {
    if (bridgeUp && String(url).indexOf('installed') >= 0) {
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, skills: FRESH_SKILLS }) });
    }
    return Promise.reject(new Error('offline'));
  };

  const dom2 = makeDom(withEmbedHtml, flaky);
  const w = dom2.window;
  const d = w.document;

  w.loadInstalled();
  setTimeout(() => {
    check('⑧ 实时数据覆盖嵌入快照（3 项而非 2 项）', itemsIn(w) === 3, itemsIn(w) + ' 项');
    check('⑨ 拿到实时数据后不再显示"快照"提示', !d.querySelector('.installed-note.stale'));

    bridgeUp = false;
    w.loadInstalled();   // 内存里已有新名单，嵌入的那份是旧的

    setTimeout(() => {
      check('⑩ bridge 掉线后不回退到旧快照', itemsIn(w) === 3, itemsIn(w) + ' 项');

      // 场景 3：连嵌入名单都没有（比如用户直接打开了模板）
      const dom3 = makeDom(bareHtml, offline);
      const w3 = dom3.window;
      w3.loadInstalled();
      setTimeout(() => {
        check('⑪ 无嵌入名单 + 离线：如实说明，不装作有数据',
          !!w3.document.querySelector('.installed-note.warn'),
          w3.document.querySelector('#installed-list').textContent.slice(0, 60));
        finish();
      }, 120);
    }, 120);
  }, 120);
}

function finish() {
  const failed = results.filter((r) => !r.ok);
  for (const r of results) {
    console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.detail ? ' — ' + r.detail : ''));
  }
  console.log('');
  console.log('总计 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
  process.exit(failed.length === 0 ? 0 : 1);
}
