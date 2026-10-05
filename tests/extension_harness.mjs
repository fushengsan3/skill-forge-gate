// R5 的行为测试：把**构建产物**里的 background.js 丢进一个假 chrome 环境跑，
// 看点击图标之后到底开的是哪个页面。
//
// 为什么要真跑而不是只读代码：
//   "文件访问没开就引导用户去开开关"这条分支，是用户唯一会遇到的失败路径，
//   而它只在 chrome.extension.isAllowedFileSchemeAccess 回 false 时触发 ——
//   光看代码看不出它到底有没有被接上。
//
// 用法: node extension_harness.mjs <构建后的 background.js> <期望的面板URL>
// 退出码 0 = 全部通过。

import fs from 'fs';
import vm from 'vm';

const code = fs.readFileSync(process.argv[2], 'utf8');
const expectedPanelUrl = process.argv[3];

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

// 造一个假的 chrome。allowed === null 表示"这个浏览器上根本没有这个 API"。
function makeChrome(allowed) {
  const calls = { tabs: [], badges: [] };
  const listeners = [];

  const chrome = {
    action: {
      onClicked: { addListener: (fn) => listeners.push(fn) },
      setBadgeText: (o) => calls.badges.push(o.text),
      setBadgeBackgroundColor: () => {},
    },
    tabs: { create: (o) => calls.tabs.push(o.url) },
    runtime: {
      id: 'fake-extension-id',
      getURL: (p) => 'chrome-extension://fake-extension-id/' + p,
    },
  };
  if (allowed !== null) {
    chrome.extension = { isAllowedFileSchemeAccess: (cb) => cb(allowed) };
  }
  return { chrome, calls, listeners };
}

// 把一组监听器全部触发，然后等微任务队列排空（回调里有 Promise）
async function click(chrome, listeners) {
  for (const fn of listeners) fn();
  await new Promise((r) => setTimeout(r, 0));
}

function run(code, allowed) {
  const env = makeChrome(allowed);
  const ctx = vm.createContext({ chrome: env.chrome, console });
  vm.runInContext(code, ctx);
  return env;
}

const isBuilt = code.indexOf('__PANEL_URL__') === -1;

async function main() {
  check('0 构建产物里没有残留占位符', isBuilt);

  // ---- 1. 开关已开：直接开面板 ----
  {
    const env = run(code, true);
    check('1a 注册了 action.onClicked 监听器', env.listeners.length === 1,
          `${env.listeners.length} 个`);
    await click(env.chrome, env.listeners);
    check('1b 开关已开时打开的是面板', env.calls.tabs[0] === expectedPanelUrl,
          env.calls.tabs[0]);
    check('1c 只开一个标签页', env.calls.tabs.length === 1, `${env.calls.tabs.length} 个`);
    check('1d 不弹说明页', !env.calls.tabs.some((u) => String(u).indexOf('help.html') >= 0));
  }

  // ---- 2. 开关未开：引导去 help.html，并挂角标提示 ----
  {
    const env = run(code, false);
    await click(env.chrome, env.listeners);
    check('2a 开关未开时不打开面板',
          !env.calls.tabs.some((u) => u === expectedPanelUrl));
    check('2b 开关未开时打开说明页',
          env.calls.tabs[0] === 'chrome-extension://fake-extension-id/help.html',
          env.calls.tabs[0]);
    check('2c 挂上 "!" 角标，用户知道点了没反应不是坏了',
          env.calls.badges.indexOf('!') >= 0, JSON.stringify(env.calls.badges));
  }

  // ---- 3. 浏览器没有这个 API：乐观放行，交给 tabs.create 去撞 ----
  //       "凭空拦下、什么都不做"是最难排查的失败方式，宁可让浏览器报自己的错。
  {
    const env = run(code, null);
    await click(env.chrome, env.listeners);
    check('3 取不到 isAllowedFileSchemeAccess 时仍然尝试打开面板',
          env.calls.tabs[0] === expectedPanelUrl, env.calls.tabs[0]);
  }

  // ---- 4. 成功打开时不该留着上一次的角标 ----
  {
    const env = run(code, true);
    await click(env.chrome, env.listeners);
    check('4 成功打开面板时清掉角标',
          env.calls.badges.length === 0 || env.calls.badges[env.calls.badges.length - 1] === '',
          JSON.stringify(env.calls.badges));
  }

  // 输出
  const failed = results.filter((r) => !r.ok);
  for (const r of results) {
    console.log(`  [${r.ok ? 'PASS' : 'FAIL'}] ${r.name}${r.ok || !r.detail ? '' : ' — ' + r.detail}`);
  }
  console.log('');
  console.log(`Total: ${results.length}, Failed: ${failed.length}`);
  process.exit(failed.length ? 1 : 0);
}

main();
