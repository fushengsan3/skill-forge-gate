// R2 分组 + R3 冻结展示的执行端。由 tests/test_panel_period.py 调用。
//
// 三个独立 DOM，各自验证一件事：
//   A 全新打开（无冻结点）  → 渲染最新一期 + 正确分组
//   B 冻结点在旧的一期      → 渲染那一期 + 出现"推进"角标
//   C 冻结点指向已被截掉的期 → 回落到最新，并清掉这个失效指针
//
// 用法: node panel_period_harness.mjs <已注入数据的.html>
// 退出码 0 = 全过；1 = 有失败。
import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

const PERIOD_KEY = 'skill-forge-frozen-period';
const FIXTURE_POINTER = '2026-09-08';   // 夹具里的第二期
const BOGUS_POINTER = '2026-01-01';     // 不在 PERIODS 里的日期

function makeDom(storedPointer) {
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://localhost/panel.html',
    beforeParse(w) {
      w.alert = () => {};
      w.confirm = () => false;
      w.fetch = () => Promise.reject(new Error('bridge offline'));
      if (storedPointer !== null && storedPointer !== undefined) {
        w.localStorage.setItem(PERIOD_KEY, storedPointer);
      } else {
        w.localStorage.removeItem(PERIOD_KEY);
      }
    },
  });
  return dom.window;
}

// 卡片标题带前置图标（"📦 d-skill"），所以一律用 indexOf 判归属，不用全等。
function titleOf(card) {
  const t = card.querySelector('.card-title');
  return t ? t.textContent : '';
}
function cardNamed(w, name) {
  return Array.from(w.document.querySelectorAll('.card')).find((c) => titleOf(c).indexOf(name) >= 0);
}
function hasCard(cards, name) {
  return cards.some((t) => t.indexOf(name) >= 0);
}

// 顺着 #skills-grid 的兄弟顺序走：group-head 后面跟着它那一组的卡片
function groupsOf(w) {
  const grid = w.document.getElementById('skills-grid');
  const out = [];
  let cur = null;
  for (const node of Array.from(grid.children)) {
    if (node.classList.contains('group-head')) {
      cur = {
        label: node.querySelector('.group-title').textContent,
        count: parseInt(node.querySelector('.group-count').textContent, 10),
        cards: [],
      };
      out.push(cur);
    } else if (node.classList.contains('card')) {
      if (cur) cur.cards.push(titleOf(node));
    }
  }
  return out;
}

const periodChip = (w) => {
  const c = w.document.querySelector('#period-bar .period-chip');
  return c ? c.textContent : '';
};
const advanceChip = (w) => w.document.querySelector('#period-bar .period-chip.ready');
const frozenChip = (w) => w.document.querySelector('#period-bar .period-chip.frozen');
const storedPointer = (w) => w.localStorage.getItem(PERIOD_KEY);

// ---------------------------------------------------------------
// A：全新打开，无冻结点 → 应当渲染**最新一期**
// ---------------------------------------------------------------
const wA = makeDom(null);

setTimeout(() => {
  check('A1 默认渲染最新一期 (2026-09-15)', periodChip(wA).indexOf('2026-09-15') >= 0, periodChip(wA));
  check('A2 无冻结点时不显示"已冻结"', !frozenChip(wA));
  check('A3 已是最新时不显示"推进"角标', !advanceChip(wA));

  const gA = groupsOf(wA);
  check('A4 渲染出两个分组', gA.length === 2, gA.map((g) => g.label).join(' | '));
  check('A5 本期新增只有 1 个（d-skill）',
    gA[0] && gA[0].count === 1 && hasCard(gA[0].cards, 'd-skill') && gA[0].cards.length === 1,
    gA[0] ? gA[0].cards.join(',') : '(无)');
  check('A6 更早发现有 3 个',
    gA[1] && gA[1].count === 3 && gA[1].cards.length === 3,
    gA[1] ? gA[1].cards.join(',') : '(无)');

  // R1：两个时间维度都要出现在卡片上
  const dCard = cardNamed(wA, 'd-skill');
  const badges = dCard ? Array.from(dCard.querySelectorAll('.badge-date')).map((b) => b.textContent) : [];
  check('A7 卡片显示「首次发现」日期',
    badges.some((b) => b.indexOf('首次发现') >= 0 && b.indexOf('2026-09-15') >= 0), badges.join(' | '));
  check('A8 卡片显示「发布时间」（created_at）',
    badges.some((b) => b.indexOf('发布') >= 0 && b.indexOf('2025-03-04') >= 0), badges.join(' | '));
  check('A9 有 pushed_at 时标成「最近推送」',
    badges.some((b) => b.indexOf('最近推送') >= 0 && b.indexOf('2026-09-12') >= 0), badges.join(' | '));

  // R1 的关键点：拿不到 pushed_at 时**不能冒充**成"最近推送"，
  // 只能如实标成另一个含义的名字（上游变更 = 元数据变动，不等于代码更新）
  const cCard = cardNamed(wA, 'c-skill');
  const cBadges = cCard ? Array.from(cCard.querySelectorAll('.badge-date')).map((b) => b.textContent) : [];
  check('A10 只有 updated_at 时标成「上游变更」而非「最近推送」',
    cBadges.some((b) => b.indexOf('上游变更') >= 0) && !cBadges.some((b) => b.indexOf('最近推送') >= 0),
    cBadges.join(' | '));

  // -------------------------------------------------------------
  // B：冻结点在第二期 → 渲染第二期，并出现"推进"角标
  // -------------------------------------------------------------
  const wB = makeDom(FIXTURE_POINTER);

  setTimeout(() => {
    check('B1 冻结在 2026-09-08，不跟到最新', periodChip(wB).indexOf('2026-09-08') >= 0, periodChip(wB));
    check('B2 显示"已冻结"标记', !!frozenChip(wB));
    check('B3 出现"推进"角标（有新一期）', !!advanceChip(wB),
      advanceChip(wB) ? advanceChip(wB).textContent : '(无)');

    const gB = groupsOf(wB);
    check('B4 第二期：本期新增是 c-skill',
      gB[0] && gB[0].cards.length === 1 && hasCard(gB[0].cards, 'c-skill'),
      gB[0] ? gB[0].cards.join(',') : '(无)');
    check('B5 第二期：更早发现 2 个（不含第三期才出现的 d）',
      gB[1] && gB[1].count === 2 && !hasCard(gB[1].cards, 'd-skill'),
      gB[1] ? gB[1].cards.join(',') : '(无)');

    // 手动推进 → 应该走到第三期，因为那是最新一期，冻结自动解除
    advanceChip(wB).dispatchEvent(new wB.Event('click'));

    setTimeout(() => {
      check('B6 推进后到达第三期', periodChip(wB).indexOf('2026-09-15') >= 0, periodChip(wB));
      check('B7 推进到最新后自动解冻', !frozenChip(wB));
      check('B8 解冻后 localStorage 指针已清除', storedPointer(wB) === null, String(storedPointer(wB)));
      check('B9 被截掉的期数如实提示',
        !!wB.document.querySelector('#period-bar .period-chip.dim'),
        wB.document.querySelector('#period-bar').textContent.slice(0, 80));

      // -----------------------------------------------------------
      // C：指针指向一个已被 MAX_PERIODS 截掉的期 → 回落最新 + 清指针
      // -----------------------------------------------------------
      const wC = makeDom(BOGUS_POINTER);
      setTimeout(() => {
        check('C1 失效指针回落到最新一期', periodChip(wC).indexOf('2026-09-15') >= 0, periodChip(wC));
        check('C2 失效指针已被清除（不装作还冻着）', storedPointer(wC) === null, String(storedPointer(wC)));
        check('C3 失效指针不显示"已冻结"', !frozenChip(wC));

        const failed = results.filter((r) => !r.ok);
        for (const r of results) {
          console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.detail ? ' — ' + r.detail : ''));
        }
        console.log('');
        console.log('总计 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
        process.exit(failed.length === 0 ? 0 : 1);
      }, 120);
    }, 120);
  }, 120);
}, 120);
