// R8 回归测试的执行端：在真实 DOM 里加载面板，用恶意数据驱动它，断言脚本没被执行。
// 由 tests/test_panel_xss.py 调用，本身不负责生成 HTML。
//
// 用法: node panel_xss_harness.mjs <已注入数据的.html>
// 退出码 0 = 全部通过；1 = 有断言失败。
import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

const HOSTILE_NAME = "evil');alert('xss4');//";
const INSTALLED_HOSTILE = "evil');alert('xss_uninstall');//";

const window0 = {};
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/panel.html',
  beforeParse(window) {
    // 只要有任何一处注入成功，这些钩子就会被触发
    window.__XSS__ = null;
    window.alert = (m) => { window.__XSS__ = 'alert:' + m; };
    window.confirm = () => false;
    window.eval = () => { throw new Error('eval blocked in harness'); };
    // bridge 假装在线，但只在 /installed 上返回一份"名字带攻击载荷"的已装列表
    window.fetch = (url, opts) => {
      window.__lastFetch = { url: String(url), headers: (opts && opts.headers) || {} };
      if (String(url).indexOf('installed') >= 0) {
        return Promise.resolve({
          json: () => Promise.resolve({
            ok: true,
            skills: [{
              name: INSTALLED_HOSTILE,
              installed_sha: 'abcdef1234567890',
              installed_at: '2026-01-01T00:00:00',
              type: 'skill',
            }],
          }),
        });
      }
      return Promise.reject(new Error('bridge offline'));
    };
  },
});

const w = dom.window;
const d = w.document;

// 已装列表默认是收起的，主动调一次让它渲染（fetch 已被替换成返回恶意名单）
w.loadInstalled();

function finish() {
  // 1) R8-1 + R8-2：没有任何注入被执行
  check('R8-1/R8-2 未执行注入脚本', w.__XSS__ === null, w.__XSS__);

  // 2) 页面真的渲染了卡片（否则下面的断言是空跑）
  const cards = d.querySelectorAll('.card');
  check('卡片已渲染', cards.length >= 4, '渲染出 ' + cards.length + ' 张卡片');

  // 3) R8-2：name 里的 <img onerror> 必须是纯文本，不能变成真实元素
  const injectedImgs = d.querySelectorAll('img[onerror], img[src="x"]');
  check('R8-2 数据里的 <img> 未被解析成元素', injectedImgs.length === 0,
    injectedImgs.length + ' 个可疑 img 元素');

  // 4) R8-2：name 原样出现在 title 的 textContent 里（证明按文本处理）
  const titles = Array.from(d.querySelectorAll('.card-title')).map((n) => n.textContent);
  const hasRawName = titles.some((t) => t.indexOf('<img src=x onerror') >= 0);
  check('R8-2 恶意名称按纯文本呈现', hasRawName, titles.join(' | ').slice(0, 120));

  // 5) R8-3：javascript: 协议不得出现在任何 href 上
  const jsHrefs = Array.from(d.querySelectorAll('a[href]'))
    .map((a) => a.getAttribute('href'))
    .filter((h) => /^\s*javascript:/i.test(h));
  check('R8-3 javascript: 链接未被渲染', jsHrefs.length === 0, jsHrefs.join(', '));

  // 6) R8-4：页面里不得存在内联 onclick 属性
  const inlineOnclick = d.querySelectorAll('[onclick]');
  check('R8-4 无内联 onclick 属性', inlineOnclick.length === 0,
    inlineOnclick.length + ' 个内联 onclick');

  // 7) R8-4 + 存储型：已装列表里那个攻击性名字不能逃逸
  const unBtn = d.querySelector('.btn-uninstall');
  check('已装列表已渲染', !!unBtn, unBtn ? '找到卸载按钮' : '未找到');
  if (unBtn) {
    let threw = null;
    try { unBtn.dispatchEvent(new w.Event('click')); } catch (e) { threw = e; }
    check('R8-4 点击卸载按钮不抛错、不执行', threw === null && w.__XSS__ === null,
      threw ? String(threw) : String(w.__XSS__));
  }
  const instName = d.querySelector('.installed-item .name');
  check('已装列表名字按纯文本呈现',
    instName && instName.textContent.indexOf("alert('xss_uninstall')") >= 0,
    instName ? instName.textContent : '(缺失)');

  // 8) R7 —— 面板必须真的把密钥发出去（bridge 侧只验证了它接受这个头）
  // 注：const 声明在经典脚本里是脚本作用域，取不到 w.BRIDGE_KEY，
  // 所以期望值由驱动端从命令行传进来。
  const EXPECTED_KEY = process.argv[3] || '';
  try {
    w.bridgeFetch('installed');
  } catch (e) {
    check('R7 bridgeFetch 可调用', false, String(e));
  }
  const sentHeaders = (w.__lastFetch && w.__lastFetch.headers) || {};
  check('R7 请求带上注入的密钥',
    !!EXPECTED_KEY && sentHeaders['X-Skill-Forge-Key'] === EXPECTED_KEY,
    JSON.stringify(sentHeaders));

  // 9) toast 不再解析 HTML
  try {
    w.showToast('<img src=x onerror="window.__XSS__=\'toast\'">', '<b>sub</b>');
    const toast = d.querySelector('#toast-container .toast');
    check('toast 按纯文本呈现', toast && toast.querySelectorAll('img,b').length === 0,
      toast ? toast.innerHTML.slice(0, 100) : '(缺失)');
    check('toast 未执行注入', w.__XSS__ === null, String(w.__XSS__));
  } catch (e) {
    check('toast 调用未抛错', false, String(e));
  }

  const failed = results.filter((r) => !r.ok);
  for (const r of results) {
    console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.detail ? ' — ' + r.detail : ''));
  }
  console.log('');
  console.log('总计 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
  dom.window.close();
  process.exit(failed.length === 0 ? 0 : 1);
}

// 等面板底部的异步逻辑跑完
setTimeout(finish, 300);
