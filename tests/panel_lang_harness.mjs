// #51 的回归执行端：在真实 DOM 里加载面板，断言搜索框的 placeholder 真的被设上了。
//
// 为什么必须是真 DOM 而不是 grep：这条缺陷的形态就是"那段代码**永远走不到**"，
// 静态看一眼 `applyLang` 会觉得它明明处理了 INPUT —— grep 只会给你假通过。
// （这个项目已经因为"从来没匹配上的 grep"吃过一次亏，见 tests/test_panel_sim.py 里
//   那条写了很久的 `s.source === 'jeremylongshore'`。）
//
// 由 tests/test_panel_lang.py 调用。用法: node panel_lang_harness.mjs <panel.html>
import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/panel.html',
  beforeParse(window) {
    // 面板在加载时会起一堆副作用：拉 /installed、读 localStorage、弹窗。
    // 全部挡掉 —— 这个 harness 只关心 applyLang 对 DOM 做了什么。
    window.alert = () => {};
    window.confirm = () => false;
    window.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: false }) });
    try {
      window.localStorage.setItem = () => {};
      window.localStorage.getItem = () => null;
    } catch (e) { /* 某些 jsdom 配置下 localStorage 只读 */ }
  },
});

const w = dom.window;
const input = w.document.getElementById('search-input');
check('找到 #search-input', !!input);

const zh = input ? input.placeholder : '';
check('★ 中文下 placeholder 被设上了（#51 之前它**永远**是空串）',
      !!zh && zh.length > 0, JSON.stringify(zh));

// 切到英文再验一次 —— 证明它是跟着语言走的，不是一句写死的默认值。
// `setLang` 是函数声明，所以挂在 window 上可以调用；
// 而 `LANG` / `currentLang` 是 const/let，脚本作用域内，window 上取不到 ——
// 所以这里只能通过它来切，不去直接读那两张表。
let switched = true;
try {
  w.setLang('en');
} catch (e) {
  switched = false;
  check('切到英文不抛异常', false, e.message);
}
if (switched) {
  const en = input ? input.placeholder : '';
  check('★ 切到英文后 placeholder 跟着变（说明它在读 LANG 表，不是写死的）',
        !!en && en !== zh, `${JSON.stringify(zh)} → ${JSON.stringify(en)}`);
  check('英文下同样非空', !!en && en.length > 0, JSON.stringify(en));
}

for (const r of results) {
  console.log(`  [${r.ok ? 'PASS' : 'FAIL'}] ${r.name}${r.detail ? ' — ' + r.detail : ''}`);
}
process.exit(results.every((r) => r.ok) ? 0 : 1);
