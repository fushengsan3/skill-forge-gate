// 说明页的交互。**必须是外部文件**：MV3 的扩展页 CSP 是 script-src 'self'，
// 内联 <script> 和内联 on* 处理器都会被直接拦掉 —— 写成内联的话这个页面
// 点按钮会毫无反应，而且控制台外的用户看不到任何原因。
(function () {
  'use strict';

  // Edge 用 edge://extensions，Chrome 用 chrome://extensions。
  // 两者互不认，给错了地址栏会直接搜"chrome://extensions"这个词。
  var isEdge = /Edg\//.test(navigator.userAgent);
  var scheme = isEdge ? 'edge' : 'chrome';

  var extUrl = scheme + '://extensions/';
  try {
    // 带上 id 可以直接跳到本扩展的详情页，省掉"在列表里找"这一步
    if (chrome && chrome.runtime && chrome.runtime.id) {
      extUrl = scheme + '://extensions/?id=' + chrome.runtime.id;
    }
  } catch (e) {
    // chrome.* 拿不到就退回列表页，够用
  }

  var urlInput = document.getElementById('ext-url');
  if (urlInput) urlInput.value = extUrl;

  var hint = document.getElementById('copy-hint');

  function flash(el, text) {
    if (el) el.textContent = text;
  }

  // 兼容路径：老一点的浏览器 / 被策略关掉剪贴板 API 的情况。
  // 用一个跑出视口的临时 textarea 承载选区，避免动到用户可见的输入框。
  function legacyCopy(text, ok, fail) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    var done = false;
    try {
      done = document.execCommand('copy');
    } catch (e) {
      done = false;
    }
    document.body.removeChild(ta);
    (done ? ok : fail)();
  }

  function wire(buttonId, getValue, hintEl, okText) {
    var btn = document.getElementById(buttonId);
    if (!btn) return;
    btn.addEventListener('click', function () {
      var text = getValue();
      var ok = function () { flash(hintEl, okText); };
      var fail = function () { flash(hintEl, '复制失败：请手动选中上面的文字，按 Ctrl+C'); };

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(ok, function () {
          legacyCopy(text, ok, fail);
        });
      } else {
        legacyCopy(text, ok, fail);
      }
    });
  }

  wire('copy-btn', function () { return urlInput ? urlInput.value : ''; },
       hint, '已复制 —— 粘到地址栏回车');

  wire('copy-panel', function () {
    var el = document.getElementById('panel-path');
    return el ? el.value : '';
  }, document.getElementById('panel-hint'), '已复制');
})();
