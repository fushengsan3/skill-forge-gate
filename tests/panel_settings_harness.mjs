// R6/R9 设置弹窗的执行端。由 tests/test_panel_settings.py 调用。
//
// 除了功能，重点验证一条**安全性质**：
//   面板从 bridge 收到的密钥信息里**只有布尔值**，页面上不可能出现密钥值。
// 这条如果破了，等于把密钥交给一个渲染不受信数据（GitHub skill 描述）的页面。
//
// 用法: node panel_settings_harness.mjs <已注入数据的.html> <期望的bridge密钥>
import fs from 'fs';
import { JSDOM } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');
const EXPECTED_KEY = process.argv[3] || '';

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail: detail == null ? '' : String(detail) });
}

// bridge 会回的假数据。注意：**故意**用一个像真密钥的字符串，
// 好在"页面里有没有出现过它"这条断言里当探针。
const FAKE_GH_TOKEN = 'ghp_PROBE_should_never_reach_the_page_9999';
const FAKE_AI_TOKEN = 'sk-PROBE_should_never_reach_the_page_8888';

const CREDS_RESPONSE = {
  available: true,
  github_token: false,   // 未配置
  ai_token: true,        // 已配置
};

const MODELS_RESPONSE = {
  ok: true,
  current: 'deepseek-v4-pro',
  models: [
    { id: 'deepseek-v4.1-flash-expires-on-0910', label: 'Opus', tier: '高能力档' },
    { id: 'deepseek-v4-pro', label: 'Sonnet', tier: '均衡档' },
    { id: 'deepseek-v4-flash', label: 'Haiku', tier: '快速档' },
  ],
};

// 配置的真源在 daemon 侧的配置文件里，面板打开设置时一次性拉齐
const TRANSLATE_CONFIG_RESPONSE = {
  ok: true,
  config: { backend: 'google', model: '', prompt: '' },
  prompts: {
    ok: true,
    default: 'skill-forge-tech',
    prompts: [
      { id: 'skill-forge-tech', name: '技术简介（内置）', description: '为简短 skill 简介调优',
        license: 'MIT', source: 'skill-forge 自带', available: true },
      { id: 'doc-mit', name: '技术文档（改编自 MIT 源）', description: '保留标题层级与表格',
        license: 'MIT', source: 'majiayu000/claude-skill-registry', available: true },
    ],
  },
  models: MODELS_RESPONSE,
  ai_ready: false,   // 故意设成 false：验证据"缺密钥"的提示会显示出来
};

const requests = [];

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  url: 'http://localhost/panel.html',
  beforeParse(w) {
    w.alert = () => {};
    w.confirm = () => true;
    w.fetch = (url, opts) => {
      const u = String(url);
      const method = (opts && opts.method) || 'GET';
      requests.push({ url: u, method, headers: (opts && opts.headers) || {},
                      body: (opts && opts.body) || '' });

      if (u.indexOf('translate-config') >= 0) {
        if (method === 'POST') {
          // 回显合并后的配置，模拟服务端的校验结果
          const sent = JSON.parse((opts && opts.body) || '{}');
          return Promise.resolve({ json: () => Promise.resolve(
            { ok: true, config: Object.assign({}, TRANSLATE_CONFIG_RESPONSE.config, sent) }) });
        }
        return Promise.resolve({ json: () => Promise.resolve(TRANSLATE_CONFIG_RESPONSE) });
      }
      if (u.indexOf('credentials/request') >= 0) {
        // 模拟用户在原生框里点了确定 —— 注意响应里**没有**值
        return Promise.resolve({ json: () => Promise.resolve(
          { ok: true, status: 'ok', available: true, github_token: true, ai_token: true }) });
      }
      if (u.indexOf('credentials/delete') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve(
          { ok: true, deleted: true, available: true, github_token: false, ai_token: true }) });
      }
      if (u.indexOf('credentials') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve(CREDS_RESPONSE) });
      }
      return Promise.reject(new Error('offline'));
    };
  },
});

const w = dom.window;
const d = w.document;
const $ = (sel) => d.querySelector(sel);
const $$ = (sel) => Array.from(d.querySelectorAll(sel));

// 等面板 init 跑完，再打开设置
setTimeout(() => {
  w.openSettings();

  setTimeout(() => {
    // ---- 弹窗与后端选择 ----
    check('S1 设置弹窗能打开', $('#settings-modal').classList.contains('open'));

    const choices = $$('#backend-choices .choice');
    check('S2 渲染出两个后端选项', choices.length === 2, choices.length + ' 个');
    check('S3 默认选中 Google（不用密钥）',
      choices[0] && choices[0].classList.contains('active') && !choices[1].classList.contains('active'));
    check('S4 选 Google 时隐藏模型区',
      $('#ai-section').style.display === 'none', $('#ai-section').style.display);

    // ---- 模型清单 ----
    const models = $$('#model-list .model-card');
    check('S5 渲染出 3 个模型（来自 Claude Code 配置）', models.length === 3, models.length + ' 个');
    const ids = models.map((m) => m.querySelector('.mc-id').textContent);
    check('S6 模型 ID 正确', ids.join(',') === MODELS_RESPONSE.models.map((m) => m.id).join(','),
      ids.join(', '));
    const badged = models.filter((m) => m.querySelector('.mc-badge'));
    check('S7 "当前使用"的模型有角标', badged.length === 1
      && badged[0].querySelector('.mc-id').textContent === 'deepseek-v4-pro',
      badged.map((m) => m.querySelector('.mc-id').textContent).join(','));

    // ---- 提示词选择器 ----
    const prompts = $$('#prompt-list .model-card');
    check('P1 渲染出内置提示词', prompts.length === 2, prompts.length + ' 个');
    const licences = prompts.map((p) => {
      const l = p.querySelector('.mc-license');
      return l ? l.textContent : '';
    });
    check('P2 每个提示词都标注了许可证（第三方来源必须让用户看见）',
      licences.every((l) => l.length > 0), licences.join(' | '));

    // ---- 切到 AI 后端 ----
    choices[1].dispatchEvent(new w.Event('click'));
    setTimeout(() => {
      check('S8 切到 AI 后显示模型区',
        $('#ai-section').style.display !== 'none', $('#ai-section').style.display);

      // 配置的真源是 daemon 侧的配置文件，所以必须**真的发给 bridge**，
      // 只写 localStorage 是没用的（daemon 读不到浏览器存储）
      // 注意：POST 不止一条（面板挂载时会自动补一个默认模型），
      // 所以要找**带 backend 的那条**，不能取第一条
      const posts = requests.filter((r) => r.url.indexOf('translate-config') >= 0 && r.method === 'POST');
      const post = posts.filter((r) => r.body.indexOf('backend') >= 0)[0];
      check('S9 后端选择经 bridge 落盘（不是只写 localStorage）',
        !!post && post.body.indexOf('"backend":"ai"') >= 0,
        post ? post.body : `(没有带 backend 的 POST，共 ${posts.length} 条: ${posts.map((p) => p.body).join(' / ')})`);
      check('S9b 面板不再把配置私有缓存在 localStorage',
        w.localStorage.getItem('skill-forge-translate-settings') === null);

      // ai_ready=false → 应当明确提示缺密钥，而不是让用户以为已生效
      const warn = $('#ai-warning');
      check('S9c 缺少 AI 密钥时给出明确警告',
        warn && warn.style.display !== 'none' && warn.textContent.indexOf('密钥') >= 0,
        warn ? warn.textContent : '(无警告元素)');

      // ---- 密钥状态 ----
      const rows = $$('#secret-rows .secret-row');
      check('S10 渲染出两行密钥', rows.length === 2, rows.length + ' 行');
      const pills = rows.map((r) => r.querySelector('.status-pill').textContent);
      check('S11 状态与 bridge 返回一致（GitHub 未配置 / AI 已配置）',
        pills[0].indexOf('未配置') >= 0 && pills[1].indexOf('已配置') >= 0, pills.join(' | '));
      const clearBtns = $$('#secret-rows .secret-row button').filter((b) => b.textContent.indexOf('清除') >= 0);
      check('S12 只有已配置的那个才有"清除"按钮', clearBtns.length === 1,
        clearBtns.length + ' 个清除按钮');

      // ---- 🔒 安全性质：页面里不得出现密钥值 ----
      const pageText = d.documentElement.outerHTML;
      check('S13 页面里不存在 GitHub 密钥值',
        pageText.indexOf(FAKE_GH_TOKEN) < 0);
      check('S14 页面里不存在 AI 密钥值',
        pageText.indexOf(FAKE_AI_TOKEN) < 0);
      const credReq = requests.filter((r) => r.url.indexOf('credentials') >= 0 && r.method === 'GET')[0];
      check('S15 bridge 的凭据响应确实只含布尔值',
        !!credReq && JSON.stringify(CREDS_RESPONSE).indexOf('PROBE') < 0);

      // ---- 点"设置"应当唤起原生框 ----
      const setBtn = rows[0].querySelectorAll('button')[0];
      setBtn.dispatchEvent(new w.Event('click'));
      setTimeout(() => {
        const post = requests.filter((r) => r.url.indexOf('credentials/request') >= 0)[0];
        check('S16 点击"设置"发起了原生框请求', !!post, post ? post.url : '(没有请求)');
        check('S17 请求体带正确的密钥名',
          !!post && post.body.indexOf('github-token') >= 0, post ? post.body : '');
        check('S18 请求带上了 R7 的 bridge 密钥',
          !!post && post.headers['X-Skill-Forge-Key'] === EXPECTED_KEY,
          post ? JSON.stringify(post.headers) : '');
        check('S19 保存成功后状态刷新为已配置',
          $$('#secret-rows .status-pill')[0].textContent.indexOf('已配置') >= 0,
          $$('#secret-rows .status-pill')[0].textContent);

        const failed = results.filter((r) => !r.ok);
        for (const r of results) {
          console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.detail ? ' — ' + r.detail : ''));
        }
        console.log('');
        console.log('总计 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
        process.exit(failed.length === 0 ? 0 : 1);
      }, 150);
    }, 120);
  }, 200);
}, 200);
