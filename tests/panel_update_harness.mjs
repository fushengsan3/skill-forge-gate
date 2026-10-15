// 卡片「安装 / 重装 / 更新」四态 + 功能板块高星榜的执行端。
// 由 tests/test_panel_update.py 调用。
//
// 用法: node panel_update_harness.mjs <已注入数据的.html>
// 退出码 0 = 全部通过；1 = 有断言失败。
import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

// 已装名单里有两个：一个无更新，一个有更新。
// 第三个 "plain-new" 没装过 —— 三种状态各来一个，才能验"按钮不是只有一种"。
const INSTALLED = [
  { name: 'plain-installed', installed_sha: 'aaaa1111', installed_at: '2026-01-01T00:00:00', type: 'skill' },
  { name: 'has-update', installed_sha: 'bbbb2222', installed_at: '2026-02-01T00:00:00', type: 'skill' },
];

let updatePost = null;       // 记下 POST /update 的 body
let updateCalls = 0;

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
      if (u.indexOf('update') >= 0 && method === 'POST') {
        updateCalls++;
        try { updatePost = JSON.parse(opts.body || '{}'); } catch (e) { updatePost = null; }
        return Promise.resolve({
          json: () => Promise.resolve({
            ok: true, name: 'has-update', sha: 'cccc3333',
            verify: { summary: '4 层通过，1 层跳过（L5 沙箱）',
                      layers: [{ name: 'L5 沙箱', verdict: 'SKIPPED' }] },
          }),
        });
      }
      if (u.indexOf('updates') >= 0) {
        return Promise.resolve({
          json: () => Promise.resolve({ ok: true, checked: 2, targets: 2, partial: false,
                                        count: 1, updates: [], cached: false }),
        });
      }
      if (u.indexOf('installed') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, skills: INSTALLED }) });
      }
      return Promise.reject(new Error('offline'));
    };
  },
});

const w = dom.window;
const d = w.document;

// 找到某张卡片的按钮：按标题里的名字定位，避免依赖卡片顺序
function cardBtn(name) {
  const titles = Array.from(d.querySelectorAll('.card-title'));
  const t = titles.find((el) => el.textContent.indexOf(name) >= 0);
  if (!t) return null;
  const card = t.closest('.card');
  return card ? card.querySelector('.card-actions button') : null;
}
const btnText = (n) => { const b = cardBtn(n); return b ? b.textContent : '(没找到卡片)'; };
const badgeTexts = (n) => {
  const titles = Array.from(d.querySelectorAll('.card-title'));
  const t = titles.find((el) => el.textContent.indexOf(n) >= 0);
  if (!t) return [];
  const card = t.closest('.card');
  return card ? Array.from(card.querySelectorAll('.badge')).map((x) => x.textContent) : [];
};

setTimeout(() => {
  // ---- 1. 按钮四态 -------------------------------------------------
  check('① 未安装 → 「📦 一键安装」', btnText('plain-new').indexOf('一键安装') >= 0, btnText('plain-new'));
  check('② 已装且无更新 → 「🔄 重装」', btnText('plain-installed').indexOf('重装') >= 0, btnText('plain-installed'));

  // ★ 这条是本次修的核心：isInstalled 曾经短路掉 isUpdate，
  //   于是"有更新"的卡片渲染成「✅ 已安装」，更新按钮永远点不到。
  const upd = btnText('has-update');
  check('③ ★有更新 → 「🔄 更新到最新」（不是「已安装」）',
        upd.indexOf('更新到最新') >= 0 && upd.indexOf('已安装') < 0, upd);

  // ---- 2. 可点性 ---------------------------------------------------
  const b1 = cardBtn('plain-installed'), b2 = cardBtn('has-update');
  check('④ 重装按钮可点（不是 disabled）', b1 && b1.disabled === false);
  check('⑤ 更新按钮可点（不是 disabled）', b2 && b2.disabled === false);
  check('⑥ 已装的卡片带更新徽章（有更新那个）',
        badgeTexts('has-update').some((x) => x.indexOf('🔄') >= 0), badgeTexts('has-update').join('|'));

  // ---- 3. 功能板块 -------------------------------------------------
  const boards = Array.from(d.querySelectorAll('.section-board'));
  check('⑦ 板块渲染出来了', boards.length >= 1, String(boards.length));
  const titles = Array.from(d.querySelectorAll('.section-board .board-title')).map((x) => x.textContent);
  check('⑧ 板块标题带图标与名称',
        titles.some((t) => t.indexOf('🎓') >= 0 && t.indexOf('学术相关') >= 0), titles.join(' | '));
  const counts = Array.from(d.querySelectorAll('.section-board .board-count')).map((x) => x.textContent);
  // count 是**全量**、items 是前 N 条：两个数都要出现，否则会被读成"一共就这么多"
  check('⑨ 计数同时给出全量与列出条数',
        counts.some((c) => c.indexOf('共') >= 0 && c.indexOf('4') >= 0), counts.join(' | '));
  const boardCards = d.querySelectorAll('.section-board .card');
  check('⑩ 板块里的项目渲染成了完整卡片', boardCards.length >= 1, String(boardCards.length));
  check('⑪ 板块卡片也有可点的操作按钮',
        d.querySelector('.section-board .card-actions button') !== null);
  check('⑫ 板块卡片不显示「🏷 undefined」',
        !Array.from(d.querySelectorAll('.section-board .card')).some(
          (c) => c.textContent.indexOf('undefined') >= 0),
        '有卡片渲染出了 undefined');

  // ---- 4. 点「更新到最新」真的发请求 --------------------------------
  if (b2) b2.dispatchEvent(new w.Event('click'));

  setTimeout(() => {
    check('⑬ 点击发出了 POST /update', updateCalls === 1, String(updateCalls));
    check('⑭ 请求体只有 name', updatePost && updatePost.name === 'has-update'
          && Object.keys(updatePost).length === 1, JSON.stringify(updatePost));
    // ★ 安全性质：URL 必须由 bridge 从 sources.json 取。
    //   前端能传 URL 就等于给面板开了"命令常驻 bridge 装任意仓库"的口子。
    check('⑮ ★请求体里**没有** url（URL 只能由 bridge 从 sources.json 取）',
          !updatePost || !('url' in updatePost), JSON.stringify(updatePost));
    // 更新成功后那条的"有更新"状态要被就地清掉，否则徽章会一直骗人
    check('⑯ 更新成功后按钮变成「重装」（有更新状态已清）',
          btnText('has-update').indexOf('重装') >= 0, btnText('has-update'));

    const failed = results.filter((r) => !r.ok);
    for (const r of results) {
      console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.detail ? ' — ' + r.detail : ''));
    }
    console.log('');
    console.log('总计 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
    process.exit(failed.length === 0 ? 0 : 1);
  }, 180);
}, 200);
