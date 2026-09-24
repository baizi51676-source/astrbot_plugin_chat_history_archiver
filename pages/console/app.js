/* 历史消息归档控制台 —— 总览 / 消息查看 / 统计 / 搜索 / 总结 / 配置
   依赖 AstrBot 注入的 window.AstrBotPluginPage bridge（受限 iframe）。 */
let bridge = window.AstrBotPluginPage;

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
    options: [['auto', '自动探测'], ['napcat', 'NapCat'], ['snowluma', 'SnowLuma']] },
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
  { key: 'ui_default_tab', label: '默认打开页面', type: 'text', hint: 'overview / messages / stats / config' },
  { key: 'ui_messages_page_size', label: '消息分页大小', type: 'int', hint: '10-500' },
  { key: 'ui_avatar_cache', label: '缓存发言人头像', type: 'bool', hint: '缓存到导出目录 avatars/（7 天更新）' },
  { key: 'llm_summary_enabled', label: 'LLM 每日总结', type: 'bool', hint: '默认关闭；开启后按时间自动总结（产生费用）' },
  { key: 'llm_summary_provider', label: '总结用模型', type: 'text', hint: '留空 = 聊天主模型' },
  { key: 'llm_summary_time', label: '自动总结时间', type: 'text', hint: 'HH:MM' },
  { key: 'llm_summary_trend_days', label: '趋势合并天数', type: 'int', hint: '1-30' },
  { key: 'llm_briefing_enabled', label: '总览简报', type: 'bool', hint: '默认关闭' },
];

const AMP = String.fromCharCode(38);
const ESC_MAP = { 38: AMP + 'amp;', 60: AMP + 'lt;', 62: AMP + 'gt;', 34: AMP + 'quot;', 39: AMP + '#39;' };

const state = {
  overview: null,
  targets: [],
  target: null,
  dates: [],
  date: '',
  days: 30,
  config: {},
  chat: [],
  offset: 0,
  pageSize: 50,
};
const AVATARS = {};

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

function targetKey(t) { return t.chat + ':' + t.target; }

async function apiGet(path, params) {
  return bridge.apiGet('console/' + path, params || {});
}

async function apiPost(path, body) {
  return bridge.apiPost('console/' + path, body || {});
}

const VIEWS = ['overview', 'messages', 'stats', 'search', 'summary', 'config'];
const BAR_VIEWS = ['messages', 'stats', 'search'];

function switchView(v) {
  state.view = v;
  document.querySelectorAll('.nav-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === v));
  VIEWS.forEach((name) => {
    const el = $('view-' + name);
    if (el) el.classList.toggle('hidden', name !== v);
  });
  $('topbar').classList.toggle('hidden', BAR_VIEWS.indexOf(v) < 0);
  $('sel-days').classList.toggle('hidden', v !== 'stats');

  if (v === 'overview') loadOverview();
  if (v === 'config') loadConfig();
  if (BAR_VIEWS.indexOf(v) >= 0) {
    ensureTargets(false).then(() => {
      if (v === 'messages') loadDates().then(() => loadMessages(true));
      if (v === 'stats') { loadDates(); loadStats(); }
    }).catch((e) => toast('目标加载失败：' + errText(e), true));
  }
}

async function ensureTargets(force) {
  if (state.targets.length && !force) return;
  const data = await apiGet('targets');
  state.targets = (data && data.targets) || [];
  const sel = $('sel-target');
  const cur = sel.value;
  sel.innerHTML = state.targets.map((t) =>
    '<option value="' + esc(targetKey(t)) + '">' + esc(t.name || t.label)
    + '（' + (t.chat === 'private' ? '私聊' : '群聊') + ' ' + esc(t.target) + '）</option>').join('');
  if (cur && state.targets.some((t) => targetKey(t) === cur)) sel.value = cur;
  if (!sel.value && state.targets.length) sel.value = targetKey(state.targets[0]);
  syncTargetFromSelect();
}

function syncTargetFromSelect() {
  const v = $('sel-target').value || '';
  const parts = v.split(':');
  state.target = (parts.length === 2 && parts[0] && parts[1])
    ? { chat: parts[0], target: parts[1] } : null;
}

async function loadOverview() {
  try {
    const d = await apiGet('state');
    state.overview = d;
    renderOverview(d);
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
    '<div class="card"><div class="k">' + esc(pair[0]) + '</div><div class="v">'
    + esc(pair[1]) + '</div></div>').join('');

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

async function loadDates() {
  syncTargetFromSelect();
  if (!state.target) return;
  try {
    const data = await apiGet('dates', { chat: state.target.chat, target_id: state.target.target });
    state.dates = (data && data.dates) || [];
    const sel = $('sel-date');
    const cur = sel.value;
    sel.innerHTML = state.dates.map((d) =>
      '<option value="' + esc(d.date) + '">' + esc(d.date) + '（' + d.count + ' 条）</option>').join('');
    if (cur && state.dates.some((d) => d.date === cur)) sel.value = cur;
    state.date = sel.value || '';
  } catch (e) {
    toast('日期列表加载失败：' + errText(e), true);
  }
}

async function loadMessages(reset) {
  syncTargetFromSelect();
  if (!state.target) {
    $('chat').innerHTML = '<div class="muted pad">暂无归档目标</div>';
    return;
  }
  if (reset) {
    state.offset = 0;
    state.chat = [];
  }
  try {
    const data = await apiGet('messages', {
      chat: state.target.chat,
      target_id: state.target.target,
      date: reset ? ($('sel-date').value || '') : (state.date || ''),
      limit: state.pageSize,
      offset: state.offset,
    });
    state.date = data.date || '';
    const items = data.items || [];
    state.total = data.total || 0;
    state.chat = reset ? items : items.concat(state.chat);
    renderChat();
    const label = (data.target && data.target.label) || state.target.target;
    $('msg-meta').textContent = label + ' · ' + state.date + ' · 当日共 ' + state.total + ' 条'
      + (state.offset ? '（已向前加载 ' + state.offset + ' 条）' : '');
  } catch (e) {
    toast('消息加载失败：' + errText(e), true);
  }
}

function renderChat() {
  const box = $('chat');
  box.innerHTML = state.chat.length
    ? state.chat.map(bubble).join('')
    : '<div class="muted pad">该日没有消息</div>';
  if (!state.offset) box.scrollTop = box.scrollHeight;
  ensureAvatars();
}

function bubble(m) {
  const uid = m.sender_id || '';
  const nick = m.sender_name || uid || '?';
  const cached = AVATARS[uid];
  const inner = cached ? '<img class="av-img" src="' + cached + '" alt="" />'
    : esc(String(nick).slice(0, 1));
  return '<div class="bubble"><div class="av" data-uid="' + esc(uid) + '">' + inner
    + '</div><div class="body"><div class="who"><b>' + esc(nick) + '</b> <span>'
    + esc(m.time || '') + '</span></div><div class="txt">' + esc(m.text || '')
    + '</div></div></div>';
}

function ensureAvatars() {
  const uids = [];
  state.chat.forEach((m) => {
    const uid = m.sender_id;
    if (uid && AVATARS[uid] === undefined && uids.indexOf(uid) < 0) uids.push(uid);
  });
  uids.slice(0, 40).forEach((uid) => {
    apiGet('avatar', { user_id: uid }).then((res) => {
      AVATARS[uid] = (res && res.data_url) ? res.data_url : '';
      if (AVATARS[uid]) {
        document.querySelectorAll('.av[data-uid="' + uid + '"]').forEach((el) => {
          el.innerHTML = '<img class="av-img" src="' + AVATARS[uid] + '" alt="" />';
        });
      }
    }).catch(() => { AVATARS[uid] = ''; });
  });
}

async function loadStats() {
  syncTargetFromSelect();
  if (!state.target) return;
  try {
    const data = await apiGet('stats', {
      chat: state.target.chat,
      target_id: state.target.target,
      days: state.days,
    });
    renderStats(data);
  } catch (e) {
    toast('统计加载失败：' + errText(e), true);
    $('stat-cards').innerHTML = '<div class="card loading">统计加载失败</div>';
  }
}

function bars(rows, maxHint) {
  const max = maxHint || Math.max.apply(null, rows.map((r) => r.value).concat([1]));
  return '<div class="bars">' + rows.map((r) =>
    '<div class="bar-row"><span class="bar-label">' + esc(r.label) + '</span>'
    + '<span class="bar"><i style="width:' + Math.round((r.value / max) * 100) + '%"></i></span>'
    + '<span class="bar-val">' + r.value + '</span></div>').join('') + '</div>';
}

function renderStats(d) {
  const daily = (d && d.daily) || [];
  const total = (d && d.total) || 0;
  const days = (d && d.days) || daily.length || 1;
  const avg = days ? Math.round(total / days) : 0;
  const top = ((d && d.top_senders) || [])[0];
  const cards = [
    ['统计天数', days + ' 天'],
    ['消息总数', total.toLocaleString()],
    ['日均消息', avg.toLocaleString()],
    ['最活跃', top ? ((top.sender_name || top.user_id) + '（' + top.count + '）') : '—'],
  ];
  $('stat-cards').innerHTML = cards.map((pair) =>
    '<div class="card"><div class="k">' + esc(pair[0]) + '</div><div class="v">'
    + esc(pair[1]) + '</div></div>').join('');

  $('stat-daily').innerHTML = daily.length
    ? bars(daily.map((r) => ({ label: r.date.slice(5), value: r.count })))
    : '<div class="muted pad">暂无数据</div>';

  const senders = (d && d.top_senders) || [];
  $('stat-senders').innerHTML = senders.length
    ? bars(senders.map((s) => ({ label: (s.sender_name || s.user_id) , value: s.count })))
    : '<div class="muted pad">暂无数据</div>';

  const hourly = (d && d.hourly) || [];
  $('stat-hourly').innerHTML = hourly.length
    ? bars(hourly.map((v, i) => ({ label: (i < 10 ? '0' : '') + i + ' 时', value: v })))
    : '<div class="muted pad">暂无数据</div>';
}

function stageNotice(boxId, what) {
  const el = $(boxId);
  if (el) el.innerHTML = '<div class="muted pad">' + esc(what) + ' 将在下一阶段（阶段 3）上线，当前为阶段 2 测试包。</div>';
}

async function loadConfig() {
  try {
    const data = await apiGet('config');
    state.config = (data && data.config) || {};
    if (state.config.ui_messages_page_size) {
      state.pageSize = Math.max(10, Math.min(500, Number(state.config.ui_messages_page_size) || 50));
    }
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
    const res = await apiPost('config', { patch: patch });
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
  document.querySelectorAll('.nav-btn').forEach((b) =>
    b.addEventListener('click', () => switchView(b.dataset.view)));
  $('btn-refresh').addEventListener('click', loadOverview);
  $('btn-more').addEventListener('click', () => { state.offset += state.pageSize; loadMessages(false); });
  $('sel-target').addEventListener('change', () => {
    syncTargetFromSelect();
    state.chat = [];
    state.offset = 0;
    loadDates();
    if (state.view === 'stats') loadStats(); else loadMessages(true);
  });
  $('sel-date').addEventListener('change', () => { state.offset = 0; loadMessages(true); });
  $('sel-days').addEventListener('change', () => { state.days = Number($('sel-days').value) || 30; loadStats(); });
  $('btn-reload').addEventListener('click', () => {
    if (state.view === 'messages') { loadDates().then(() => loadMessages(true)); }
    else if (state.view === 'stats') { loadDates(); loadStats(); }
  });
  $('btn-reload-cfg').addEventListener('click', loadConfig);
  $('btn-save-cfg').addEventListener('click', saveConfig);
  $('btn-search').addEventListener('click', () => stageNotice('search-result', '搜索功能'));
  $('btn-summ-date').addEventListener('click', () => stageNotice('summary-box', 'LLM 总结'));
  $('btn-briefing').addEventListener('click', () => stageNotice('summary-box', '总览简报'));

  try {
    const cfg = await apiGet('config');
    state.config = (cfg && cfg.config) || {};
    state.pageSize = Math.max(10, Math.min(500, Number(state.config.ui_messages_page_size) || 50));
  } catch (e) { /* 忽略，用默认值 */ }

  const tab = state.config.ui_default_tab || 'overview';
  switchView(VIEWS.indexOf(tab) >= 0 ? tab : 'overview');
}

boot();
