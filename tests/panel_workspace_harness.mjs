import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const html = fs.readFileSync(process.argv[2], 'utf8');
const pause = () => new Promise(resolve => setTimeout(resolve, 40));
function check(value, message) { if (!value) throw new Error(message); console.log('PASS ' + message); }
async function openPanel({saved, blocked = false, offline = false} = {}) {
  const errors = [], calls = [];
  let resolvePreview;
  const console = new VirtualConsole(); console.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM(html, {runScripts: 'dangerously', url: 'http://localhost/panel.html', virtualConsole: console,
    beforeParse(w) {
      if (saved) w.localStorage.setItem('skill-forge-theme', saved);
      if (blocked) Object.defineProperty(w, 'localStorage', {get() { throw new Error('storage disabled'); }});
      w.alert = () => {}; w.confirm = () => true;
      w.fetch = (url, opts = {}) => {
        const path = new URL(String(url)).pathname; calls.push(path);
        if (offline) return Promise.reject(new Error('offline'));
        const reply = value => Promise.resolve({ok: true, json: () => Promise.resolve(value)});
        if (path === '/preview-install') return new Promise(resolve => { resolvePreview = value => resolve(reply(value)); });
        if (path === '/installed') return reply({ok: true, skills: [{name: 'alpha-skill', tool: 'claude', type: 'remote'}, {name: 'alpha-skill', tool: 'codex', type: 'remote'}]});
        if (path === '/updates') return reply({ok: true, updates: [], checked_names: [], partial: false});
        if (path === '/ext-sources') return reply({ok: true, sources: []});
        if (path === '/') return reply({ok: true, pending: []});
        throw new Error('Unexpected request: ' + path);
      };
    }});
  await pause();
  return {dom, w: dom.window, d: dom.window.document, errors, calls, finishPreview: value => resolvePreview(value)};
}
function luminance(hex) {
  const rgb = hex.match(/[a-f\d]{2}/gi).map(x => parseInt(x, 16) / 255).map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4);
  return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
}
function contrast(a, b) { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); }

const app = await openPanel(), {w, d} = app;
const select = d.getElementById('theme-select');
check(d.documentElement.dataset.theme === 'mist-harbor' && select.selectedOptions[0].textContent === '雾港蓝', 'first launch defaults to 雾港蓝');
check([...select.options].map(x => x.textContent).join('、') === '雾港蓝、岩苔绿、暖陶橘、烟霞紫、潮汐青、麦穗金、夜航靛、山莓红、杏砂棕、石墨银', 'all ten approved themes remain selectable');
const count = app.calls.length, accents = new Set();
for (const option of select.options) {
  select.value = option.value; select.dispatchEvent(new w.Event('change'));
  const css = d.documentElement.style, value = key => css.getPropertyValue('--' + key);
  accents.add(value('accent'));
  check(w.localStorage.getItem('skill-forge-theme') === option.value && d.documentElement.dataset.theme === option.value, option.textContent + ' applies and persists');
  for (const [fg, bg] of [['ink', 'paper'], ['muted', 'paper'], ['side-ink', 'sidebar'], ['side-muted', 'sidebar'], ['nav-ink', 'nav'], ['button-ink', 'button']]) {
    if (contrast(value(fg), value(bg)) < 4.5) throw new Error(`${option.textContent}: insufficient ${fg}/${bg} contrast`);
  }
}
check(accents.size === 10 && app.calls.length === count, 'themes have distinct accents, readable text and no network requests');
for (const view of ['installed', 'versions', 'sources', 'discover']) {
  d.querySelector('[data-view="' + view + '"]').click(); await pause();
  check(d.querySelector('[aria-current="page"]').dataset.view === view && d.getElementById('discovery-view').hidden === (view !== 'discover'), view + ' navigation updates the visible workspace');
}
d.getElementById('btn-versions').click(); await pause();
check(!d.getElementById('version-view-hint').hidden && d.getElementById('installed-list').textContent.includes('Codex 独立版本'), 'version view keeps tool variants separate and exposes history actions');
d.getElementById('btn-close-installed').click();
d.getElementById('btn-en').click();
check(d.getElementById('workspace-title').textContent === 'Find your next capability', 'workspace headings follow the language selection');
d.getElementById('btn-zh').click();
const opener = d.getElementById('btn-discover'); opener.focus();
w.beginInstallPreview('example', 'https://github.com/a/example');
check(d.getElementById('module-inspector').classList.contains('has-preview') && d.getElementById('btn-confirm-modules').disabled, 'module inspector opens with explicit loading state');
d.getElementById('btn-close-modules').dispatchEvent(new w.KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
app.finishPreview({ok: true, branch: 'main', sha: 'a'.repeat(40), modules: []}); await pause();
check(!d.getElementById('module-inspector').classList.contains('has-preview') && d.activeElement === opener && d.getElementById('module-list').children.length === 0, 'Escape restores focus and ignores a late preview response');
check(app.errors.length === 0, 'workspace interactions have no script errors');
app.dom.window.close();

for (const saved of ['rock-moss', 'haze-violet', 'mist-harbor', 'unknown-theme']) {
  const app = await openPanel({saved});
  check(app.d.documentElement.dataset.theme === (saved === 'unknown-theme' ? 'mist-harbor' : saved), 'reload restores ' + saved + ' or safely falls back');
  app.dom.window.close();
}
const unavailable = await openPanel({blocked: true, offline: true});
unavailable.d.getElementById('theme-select').value = 'night-indigo';
unavailable.d.getElementById('theme-select').dispatchEvent(new unavailable.w.Event('change'));
check(unavailable.d.documentElement.dataset.theme === 'night-indigo' && unavailable.d.getElementById('theme-note').textContent.includes('本次有效') && !unavailable.errors.length, 'themes still work with storage disabled and bridge offline');
unavailable.dom.window.close();
console.log('RESULT: PASS');
