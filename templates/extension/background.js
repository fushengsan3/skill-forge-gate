// Skill Forge 启动器 —— 零权限。
//
// 这个扩展**故意**一个 permissions 都不声明，也不声明 host_permissions、
// content_scripts、web_accessible_resources。它做的事只有一件：
// 把本地面板当成普通文件，在新标签页里打开。
//
// 为什么不把面板塞进扩展里当宿主页（决策记录 §7 R5）：
//   面板渲染的是从 GitHub 拉来的、不受信的 description。它自己有 R8 的四层
//   防线（服务端转义 `</`、DOM API 建节点、URL 协议白名单、禁内联 on*），
//   但防线是"降低概率"，不是"证明不可能"。同样一次突围——
//     在普通标签页里：最坏是关掉一个本地页面；
//     在扩展上下文里：直接够到 chrome.*（读所有标签页、发跨域请求、改别的扩展）。
//   收益（省一次点击）远小于代价（把 XSS 从"烦人"升级成"失守"）。
//
// 面板路径是**构建时写死**的，见 daemon/build_extension.py。
// 不要为了"让路径可配置"去加 "storage" 权限 —— 那会凭空引入一项能力，
// 只为了让用户少改一行；而且扩展读不到 USERPROFILE，本来也没法自己找到面板。

// 由 daemon/build_extension.py 替换成真实的 file:/// 绝对地址。
// 保持模板状态时它是个非 URL 的占位串：这样"忘了构建"会立刻表现为
// 浏览器报错，而不是悄悄打开一个 404 或空白页。
const PANEL_URL = '__PANEL_URL__';

const HELP_PAGE = 'help.html';
const BADGE_COLOR = '#e94560';

// 文件访问开关没开时给图标挂个"!"，让用户知道"点了没反应"不是坏了。
function setBadge(text) {
  try {
    chrome.action.setBadgeText({ text: text });
    if (text) chrome.action.setBadgeBackgroundColor({ color: BADGE_COLOR });
  } catch (e) {
    // 角标只是提示，拿不到就算了，不能因此让点击流程失败
  }
}

// 扩展有没有被允许读 file:// —— 这是个**用户手动勾选**的开关，
// manifest 里声明的任何权限都换不来它（Edge/Chrome 都是如此）。
//
// 返回值是 Promise：chrome 的老 API 是回调式的，包一层好让 onClicked 里能 await。
// 取不到这个 API（不同浏览器版本差异）时乐观返回 true，让 tabs.create 自己去撞——
// 撞墙了浏览器会显示自己的错误页，比我们凭空拦下、什么都不做要好排查。
function isFileAccessAllowed() {
  return new Promise(function (resolve) {
    try {
      if (!chrome.extension || !chrome.extension.isAllowedFileSchemeAccess) {
        resolve(true);
        return;
      }
      chrome.extension.isAllowedFileSchemeAccess(function (allowed) {
        resolve(!!allowed);
      });
    } catch (e) {
      resolve(true);
    }
  });
}

function openHelp() {
  setBadge('!');
  chrome.tabs.create({ url: chrome.runtime.getURL(HELP_PAGE) });
}

chrome.action.onClicked.addListener(function () {
  isFileAccessAllowed().then(function (allowed) {
    if (!allowed) {
      openHelp();
      return;
    }
    setBadge('');
    chrome.tabs.create({ url: PANEL_URL });
  });
});
