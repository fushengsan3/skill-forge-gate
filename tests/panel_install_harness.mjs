// R4-c 回归测试的执行端：装完一个 skill 后，面板的已装名单要自动跟上。
// 由 tests/test_panel_install.py 调用。
//
// 用法: node panel_install_harness.mjs <已注入数据的.html>
// 退出码 0 = 全部通过；1 = 有断言失败。
import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

// 安装前 sources.json 里有一个；安装后 bridge 会多报一个
const BEFORE = [
  { name: 'alpha-skill', installed_sha: 'aaaa1111', installed_at: '2026-01-01T00:00:00', type: 'skill' },
];
const AFTER = BEFORE.concat([
  { name: 'gamma-skill', installed_sha: 'cccc3333', installed_at: '2026-03-01T00:00:00', type: 'skill' },
]);

let installedCalls = 0;
let installPosted = false;
let installedResult = BEFORE;

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/panel.html',
  beforeParse(window) {
    window.alert = () => {};
    window.confirm = () => true;
    window.__errors = [];
    window.fetch = (url, opts) => {
      const u = String(url);
      const method = (opts && opts.method) || 'GET';
      if (u.indexOf('install') >= 0 && method === 'POST') {
        installPosted = true;
        installedResult = AFTER;      // 模拟 bridge 真的改了 sources.json
        return Promise.resolve({
          json: () => Promise.resolve({ ok: true, success: 1, processed: 1, failed: 0 }),
        });
      }
      if (u.indexOf('installed') >= 0) {
        installedCalls++;
        return Promise.resolve({
          json: () => Promise.resolve({ ok: true, skills: installedResult }),
        });
      }
      return Promise.reject(new Error('offline'));
    };
  },
});

const w = dom.window;
const d = w.document;
const names = () => Array.from(d.querySelectorAll('.installed-item .name')).map((n) => n.textContent);

// 初始：桥在线，一个已装
w.initInstalled();

setTimeout(() => {
  check('① 初始名单已渲染', names().length === 1, names().join(', '));
  const callsBefore = installedCalls;

  // 加入队列并安装（queueInstall / triggerInstall 都是函数声明，挂在 window 上）
  w.queueInstall('gamma-skill', 'https://github.com/a/gamma', false);
  w.triggerInstall();

  setTimeout(() => {
    check('② 确实向 bridge 发了安装请求', installPosted);
    check('③ 装完自动重新拉取名单', installedCalls > callsBefore,
      `${callsBefore} → ${installedCalls}`);
    check('④ 新装的 skill 出现在名单里', names().some((n) => n.indexOf('gamma-skill') >= 0),
      names().join(', '));
    check('⑤ 角标数量已更新', d.getElementById('installed-count').textContent === '2',
      d.getElementById('installed-count').textContent);
    check('⑥ 安装队列已清空',
      d.getElementById('queue-badge').classList.contains('visible') === false);

    const failed = results.filter((r) => !r.ok);
    for (const r of results) {
      console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.detail ? ' — ' + r.detail : ''));
    }
    console.log('');
    console.log('总计 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
    process.exit(failed.length === 0 ? 0 : 1);
  }, 150);
}, 120);
