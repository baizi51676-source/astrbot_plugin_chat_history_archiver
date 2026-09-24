/* 历史消息归档控制台（总览 / 消息查看 / 配置）
   依赖 AstrBot 自动注入的 window.AstrBotPluginPage bridge。 */
let bridge = window.AstrBotPluginPage;

/* AstrBot 把 bridge SDK 注入在 </body> 前（即本脚本之后），
   因此这里必须等它加载完成，否则 apiGet/apiPost 全部不可用。 */
function waitBridge(timeoutMs) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const tick = () => {
      if (window.AstrBotPluginPage) {
        bridge = window.AstrBotPluginPage;
        resolve(true);
        return;
      }
      if (Date.now() - t0 > (timeoutMs || 6000)) {
        resolve(false);
        return;
      }
      setTimeout(tick, 50);
    };
    tick();
  });
}
const $ = (id) => document.getElementById(id);

const FIELDS = [
  { key: 'backend', label: '后端协议', type: 'select',
    options: [['auto', '自动探测'], ['napcat', 'NapCat'], ['snowluma', 'SnowLuma']],
    hint: 'auto = 根据消息特征自动识别后端' },
  { key: 'export_dir', label: '导出目录', type: 'text',
    hint: '相对 AstrBot 工作目录，如 data/workspaces/napcat_exports' },
  { key: 'auto_export', label: '自动归档', type: 'bool', hint: '开启后按间隔定时增量归档' },
  { key: 'interval_seconds', label: '循环间隔（秒）', type: 'int', hint: '最小 30 秒' },
  { key: 'startup_verify', label: '启动检查补全', type: 'bool', hint: '启动时自动补齐缺失或不全的归档记录' },
  { key: 'verify_days', label: '启动检查天数', type: 'int', hint: '1-30 天' },
  { key: 'whitelist', label: '自动归档群白名单', type: 'list', wide: true,
    hint: '每行一个群号；留空 = 全部群（手动归档不受限）' },
  { key: 'auto_export_friends', label: '定时同时导出私聊', type: 'bool' },
  { key: 'archive_bots', label: '多 bot 归档白名单', type: 'list', wide: true,
    hint: '每行一个登录 QQ 号或平台实例 id；留空 = 全部 aiocqhttp 实例' },
  { key: 'aliases', label: '对话别名', type: 'list', wide: true,
    hint: '每行一条「别名,群号或QQ号」，例如：闲聊群,748791823' },
  { key: 'count_per_batch', label: '单次拉取条数', type: 'int', hint: '1-200' },
  { key: 'admin_only', label: '仅管理员可用工具', type: 'bool' },
  { key: 'auto_clean', label: '自动清理历史文件', type: 'bool', hint: '仅清理未被手动归档过的目标' },
  { key: 'clean_days', label: '历史保留天数', type: 'int' },
];

const AMP = String.fromCharCode(38);
const ESC_MAP = {
  38: AMP + 'amp;',
  60: AMP + 'lt;',
  62: AMP + 'gt;',
  34: AMP + 'quot;',
  39: AMP + '#39;',
};

const state = {
  overview: null,
  config: null,
  msg: { target: '', date: '', offset: 0, limit: 200, total: 0, chat: [] },
};

function toast(msg, isErr) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (isErr ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast hidden'; }, 3500);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>\u0022\u0027]/g,
    (c) => ESC_MAP[c.charCodeAt(0)] || c);
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
  return (n / 1073741824).toFixed(2) + ' GB';
}

function errText(e) {
  if (!e) return '未知错误';
  if (typeof e === 'string') return e;
  return e.message || JSON.stringify(e);
}

function switchView(v) {
  document.querySelectorAll('.nav-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === v));
  ['overview', 'messages', 'config'].forEach((name) => {
    const el = $('view-' + name);
    if (el) el.classList.toggle('hidden', name !== v);
  });
  if (v === 'overview') loadOverview();
  if (v === 'messages') loadTargetsForMessages();
  if (v === 'config') loadConfig();
}

async function loadOverview() {
  try {
    const data = await bridge.apiGet('state');
    state.overview = data;
    renderOverview(data);
  } catch (e) {
    toast('总览加载失败：' + errText(e), true);
  }
}

function renderOverview(d) {
  const s = (d && d.summary) || {};
  const cards = [
    ['归档目标', (s.targets || 0) + ' 个'],
    ['群聊 / 私聊', (s.groups || 0) + ' / ' + (s.privates || 0)],
    ['已归档天数', (s.days || 0) + ' 天'],
    ['归档文件', (s.files || 0) + ' 个'],
    ['消息总条数', (s.rows || 0).toLocaleString()],
    ['占用体积', fmtSize(s.size)],
    ['最近归档', s.last_date || '—'],
  ];
  $('cards').innerHTML = cards.map((pair) =>
    '<div class="card"><div class="k">' + esc(pair[0]) + '</div>' +
    '<div class="v">' + esc(pair[1]) + '</div></div>').join('');

  const rows = (d && d.targets) || [];
  $('targets-body').innerHTML = rows.length ? rows.map((t) =>
    '<tr><td>' + esc(t.name || t.label) + '</td>'
    + '<td>' + esc((t.aliases || []).join('、') || '—') + '</td>'
    + '<td>' + esc(t.target) + '</td>'
    + '<td>' + (t.chat === 'private' ? '私聊' : '群聊') + '</td>'
    + '<td>' + t.days + '</td>'
    + '<td>' + (t.rows || 0).toLocaleString() + '</td>'
    + '<td>' + fmtSize(t.size) + '</td>'
    + '<td>' + esc(t.last_date || '—') + '</td></tr>').join('')
    : '<tr><td colspan="8" class="muted">暂无归档目标</td></tr>';

  $('export-dir').textContent = '导出目录：' + (d.export_dir || '—')
    + '（后端：' + (d.backend || 'auto') + '）';
  $('foot-ver').textContent = 'v' + ((d && d.version) || '2.3.0');
}

async function loadTargetsForMessages() {
  try {
    const data = await bridge.apiGet('targets');
    const list = (data && data.targets) || [];
    const sel = $('msg-target');
    const cur = sel.value;
    sel.innerHTML = list.map((t) =>
      '<option value="' + esc(t.target) + '">' + esc(t.name || t.label)
      + '（' + (t.chat === 'private' ? '私聊' : '群聊') + ' ' + esc(t.target) + '）</option>').join('');
    if (!list.length) {
      $('chat').innerHTML = '<div class="muted pad">暂无归档目标</div>';
      return;
    }
    if (cur && list.some((t) => t.target === cur)) sel.value = cur;
    await loadMessages(true);
  } catch (e) {
    toast('目标列表加载失败：' + errText(e), true);
  }
}

async function loadMessages(reset) {
  const target = $('msg-target').value;
  if (!target) return;
  if (reset) {
    state.msg.offset = 0;
    state.msg.chat = [];
  }
  state.msg.target = target;
  const dateArg = reset ? ($('msg-date').value || '') : (state.msg.date || '');
  try {
    const data = await bridge.apiGet('messages', {
      target: target,
      date: dateArg,
      limit: state.msg.limit,
      offset: state.msg.offset,
    });
    const dates = data.dates || [];
    $('msg-date').innerHTML = dates.map((d) =>
      '<option value="' + esc(d) + '"' + (d === data.date ? ' selected' : '') + '>' + esc(d) + '</option>').join('');
    state.msg.date = data.date || '';
    state.msg.total = data.total || 0;
    const msgs = data.messages || [];
    state.msg.chat = reset ? msgs : msgs.concat(state.msg.chat);
    renderChat();
    const label = (data.target && data.target.label) || target;
    $('msg-meta').textContent = label + ' · ' + state.msg.date + ' · 当日共 ' + state.msg.total + ' 条'
      + (state.msg.offset ? '（已向前加载 ' + state.msg.offset + ' 条）' : '');
  } catch (e) {
    toast('消息加载失败：' + errText(e), true);
  }
}

function renderChat() {
  const box = $('chat');
  box.innerHTML = state.msg.chat.length
    ? state.msg.chat.map(bubble).join('')
    : '<div class="muted pad">该日没有消息</div>';
  if (!state.msg.offset) box.scrollTop = box.scrollHeight;
}

function bubble(m) {
  const nick = m.nickname || m.user_id || '?';
  const head = String(nick).slice(0, 1);
  return '<div class="bubble"><div class="av">' + esc(head) + '</div><div class="body">'
    + '<div class="who"><b>' + esc(nick) + '</b> <span>' + esc(m.t || '') + '</span></div>'
    + '<div class="txt">' + esc(m.content || '') + '</div></div></div>';
}

async function loadConfig() {
  try {
    const data = await bridge.apiGet('config');
    state.config = (data && data.config) || {};
    renderConfig();
  } catch (e) {
    toast('配置加载失败：' + errText(e), true);
  }
}

function ctrlFor(f, v) {
  if (f.type === 'bool') {
    return '<input type="checkbox" data-key="' + f.key + '"' + (v ? ' checked' : '') + ' />';
  }
  if (f.type === 'int') {
    return '<input type="number" data-key="' + f.key + '" value="'
      + esc(v == null ? '' : v) + '" style="width:130px" />';
  }
  if (f.type === 'select') {
    return '<select data-key="' + f.key + '">' + f.options.map((opt) =>
      '<option value="' + opt[0] + '"' + (String(v) === opt[0] ? ' selected' : '') + '>'
      + opt[1] + '</option>').join('') + '</select>';
  }
  if (f.type === 'list') {
    const text = Array.isArray(v) ? v.join('\n') : (v == null ? '' : String(v));
    return '<textarea data-key="' + f.key + '" spellcheck="false">' + esc(text) + '</textarea>';
  }
  return '<input type="text" data-key="' + f.key + '" value="'
    + esc(v == null ? '' : v) + '" style="width:100%" />';
}

function renderConfig() {
  const cfg = state.config || {};
  $('config-form').innerHTML = FIELDS.map((f) => {
    const wide = (f.wide || f.type === 'list') ? ' wide' : '';
    return '<div class="field' + wide + '"><div class="label">' + esc(f.label) + '</div>'
      + '<div class="ctrl">' + ctrlFor(f, cfg[f.key]) + '</div>'
      + (f.hint ? '<div class="hint">' + esc(f.hint) + '</div>' : '') + '</div>';
  }).join('');
}

async function saveConfig() {
  const patch = {};
  document.querySelectorAll('#config-form [data-key]').forEach((el) => {
    const key = el.dataset.key;
    const f = FIELDS.find((x) => x.key === key);
    if (!f) return;
    if (f.type === 'bool') {
      patch[key] = !!el.checked;
    } else if (f.type === 'int') {
      patch[key] = parseInt(el.value, 10) || 0;
    } else if (f.type === 'list') {
      patch[key] = el.value.split('\n').map((x) => x.trim()).filter(Boolean);
    } else {
      patch[key] = el.value;
    }
  });
  try {
    const res = await bridge.apiPost('config', { patch: patch });
    toast('已保存：' + (((res && res.saved) || []).join('、')));
    await loadConfig();
  } catch (e) {
    toast('保存失败：' + errText(e), true);
  }
}

async function boot() {
  const bridged = await waitBridge(6000);
  if (!bridged) {
    $('cards').innerHTML = '<div class="card loading">页面桥接未就绪：请刷新页面重试</div>';
    toast('页面桥接未就绪：请刷新页面重试', true);
    return;
  }
  if (bridge && bridge.ready) {
    try { await bridge.ready(); } catch (e) { /* 旧版 bridge 无 ready 时忽略 */ }
  }
  document.querySelectorAll('.nav-btn').forEach((b) =>
    b.addEventListener('click', () => switchView(b.dataset.view)));
  $('btn-refresh').addEventListener('click', loadOverview);
  $('btn-load-msg').addEventListener('click', () => loadMessages(true));
  $('msg-target').addEventListener('change', () => loadMessages(true));
  $('msg-date').addEventListener('change', () => loadMessages(true));
  $('btn-more').addEventListener('click', () => {
    state.msg.offset += state.msg.limit;
    loadMessages(false);
  });
  $('btn-reload-cfg').addEventListener('click', loadConfig);
  $('btn-save-cfg').addEventListener('click', saveConfig);
  switchView('overview');
}

boot();
