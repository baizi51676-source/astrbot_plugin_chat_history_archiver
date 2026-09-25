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
  { key: 'llm_summary_max_chars', label: '单群总结字数', type: 'int', hint: '默认 1000（100-5000）' },
  { key: 'llm_briefing_max_chars', label: '简报字数', type: 'int', hint: '默认 2000（200-8000）' },
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
  range: { start: '', end: '', label: '' },
  picking: '',
  cal: { left: { y: 0, m: 0 } },
  config: {},
  chat: [],
  offset: 0,
  pageSize: 50,
  names: {},
  namesKey: '',
  hasMore: false,
  loadingMore: false,
  summaries: {},
  summariesKey: '',
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

const VIEWS = ['overview', 'messages', 'stats', 'search', 'config'];
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
  $('range-wrap').classList.toggle('hidden', v !== 'stats');

  if (v === 'overview') loadOverview();
  if (v === 'config') loadConfig();
  if (BAR_VIEWS.indexOf(v) >= 0) {
    ensureTargets(false).then(() => {
      if (v === 'messages') loadDates().then(() => loadMessages(true));
      if (v === 'stats') { loadDates().then(() => { loadStats(); loadSummary(); }); }
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
    loadBrief();
  } catch (e) {
    toast('总览加载失败：' + errText(e), true);
  }
}

async function loadBrief() {
  const box = $('brief-text');
  if (!box) return;
  try {
    const res = await apiGet('briefing', { days: rangeDays(), get: 1 });
    if (res && res.brief) {
      box.innerHTML = '<div class="summary-title">简报 ' + esc(res.date || '')
        + '（已缓存）</div><div class="summary-body">' + esc(res.brief) + '</div>';
    }
  } catch (e) {
    /* 没有缓存简报（404）时保持初始提示 */
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
    if (!state.range.start && !state.range.end && state.dates.length) {
      state.date = state.dates[state.dates.length - 1].date;
    }
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
    const mparams = {
      chat: state.target.chat,
      target_id: state.target.target,
      limit: state.pageSize,
      offset: state.offset,
    };
    if (state.range.start || state.range.end) {
      mparams.start = state.range.start || '';
      mparams.end = state.range.end || '';
    }
    const data = await apiGet('messages', mparams);
    const items = data.items || [];
    state.total = data.total || 0;
    state.hasMore = !!data.has_more;
    state.chat = reset ? items : items.concat(state.chat);
    renderChat();
    const label = (data.target && data.target.label) || state.target.target;
    const rng = (data.start && data.end && data.start !== data.end)
      ? (data.start + ' ~ ' + data.end)
      : (data.start || data.end || state.date || '全部');
    $('msg-meta').textContent = label + ' · ' + rng + ' · 共 ' + state.total + ' 条'
      + (state.offset ? '（已向前加载 ' + state.offset + ' 条）' : '');
    updateMsgHint();
  } catch (e) {
    toast('消息加载失败：' + errText(e), true);
  }
}

function chatHtml() {
  if (!state.chat.length) return '<div class="muted pad">该范围没有消息</div>';
  let html = '';
  let last = '';
  state.chat.forEach((m) => {
    const d = m.date || '';
    if (d && d !== last) {
      html += '<div class="day-sep">' + esc(d) + '</div>';
      last = d;
    }
    html += bubble(m);
  });
  return html;
}

function renderChat() {
  const box = $('chat');
  box.innerHTML = chatHtml();
  if (!state.offset) box.scrollTop = box.scrollHeight;
  ensureNames(false).then(() => { /* 名称到手后重绘一次 */
    if (state.namesKey) {
      const again = chatHtml();
      if (again && again !== box.innerHTML) box.innerHTML = again;
    }
  });
  ensureAvatars();
}

/* bubble() 见文件末尾的 v2.3.0 增强段 */

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

/* ---------- v2.3.2：DeepSeek 风格日期范围选择器 ---------- */

function pad2(n) { return (n < 10 ? '0' : '') + n; }

function dstr(d) {
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

function parseD(s) {
  const p = String(s || '').split('-');
  return p.length === 3 ? new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2])) : null;
}

function todayD() {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

const DAY_MS = 86400000;

function rangeDays() {
  const s = parseD(state.range.start);
  const e = parseD(state.range.end);
  if (s && e) return Math.max(1, Math.round((e - s) / DAY_MS) + 1);
  if (state.range.label === '当天' || state.range.label === '今天'
      || state.range.label === '昨天') return 1;
  return state.days || 30;
}

const PRESETS = [
  ['全部时间', 'all'], ['当天', 'cur'], ['今天', 'today'], ['昨天', 'yes'],
  ['近 7 天', 'd7'], ['近 30 天', 'd30'],
  ['本月', 'tm'], ['上月', 'lm'], ['自定义', 'custom'],
];

function presetRange(key) {
  const t = todayD();
  if (key === 'all') return ['', '', '全部时间'];
  if (key === 'today') return [dstr(t), dstr(t), '今天'];
  if (key === 'yes') {
    const y = new Date(t.getTime() - DAY_MS);
    return [dstr(y), dstr(y), '昨天'];
  }
  if (key === 'd7') return [dstr(new Date(t.getTime() - 6 * DAY_MS)), dstr(t), '近 7 天'];
  if (key === 'd30') return [dstr(new Date(t.getTime() - 29 * DAY_MS)), dstr(t), '近 30 天'];
  if (key === 'tm') return [dstr(new Date(t.getFullYear(), t.getMonth(), 1)), dstr(t), '本月'];
  if (key === 'lm') {
    return [dstr(new Date(t.getFullYear(), t.getMonth() - 1, 1)),
      dstr(new Date(t.getFullYear(), t.getMonth(), 0)), '上月'];
  }
  if (key === 'cur') {
    const d = state.range.end || state.date || latestDate() || dstr(t);
    return [d, d, '当天'];
  }
  return null;
}

function renderPresets() {
  const box = $('rp-presets');
  if (!box) return;
  box.innerHTML = PRESETS.map((p) =>
    '<button class="rp-item' + (state.range.label === p[0] ? ' on' : '')
    + '" data-preset="' + p[1] + '">' + esc(p[0]) + '</button>').join('');
}

function calShift(n) {
  const c = state.cal.left || { y: todayD().getFullYear(), m: todayD().getMonth() };
  let m = c.m + n;
  let y = c.y;
  while (m < 0) { m += 12; y -= 1; }
  while (m > 11) { m -= 12; y += 1; }
  state.cal.left = { y: y, m: m };
  renderCalendar();
}

function monthGrid(y, m) {
  const dim = new Date(y, m + 1, 0).getDate();
  const startWd = new Date(y, m, 1).getDay();
  const today = dstr(todayD());
  const s = state.range.start;
  const e = state.range.end;
  const wk = ['日', '一', '二', '三', '四', '五', '六'];
  let html = '<div class="rp-month"><div class="rp-mtitle">' + y + ' 年 ' + (m + 1)
    + ' 月</div><div class="rp-wk">'
    + wk.map((w) => '<span>' + w + '</span>').join('') + '</div><div class="rp-days">';
  for (let i = 0; i < startWd; i += 1) html += '<span class="rp-day empty"></span>';
  for (let d = 1; d <= dim; d += 1) {
    const ds = y + '-' + pad2(m + 1) + '-' + pad2(d);
    const cls = ['rp-day'];
    if (ds === today) cls.push('today');
    if (s && ds === s) cls.push('edge');
    if (e && ds === e) cls.push('edge');
    if (s && e && ds > s && ds < e) cls.push('mid');
    if (s && !e && ds === s) cls.push('sel');
    if (ds > today) cls.push('future');
    html += '<span class="' + cls.join(' ') + '" data-day="' + ds + '">' + d + '</span>';
  }
  return html + '</div></div>';
}

function renderCalendar() {
  const box = $('rp-grids');
  if (!box) return;
  const t = todayD();
  const c = state.cal.left || { y: t.getFullYear(), m: t.getMonth() };
  let m2 = c.m + 1;
  let y2 = c.y;
  if (m2 > 11) { m2 = 0; y2 += 1; }
  box.innerHTML = monthGrid(c.y, c.m) + monthGrid(y2, m2);
  const hint = $('rp-hint');
  if (hint) {
    if (state.range.start && !state.range.end) {
      hint.textContent = '开始 ' + state.range.start + '，请选择结束日期';
    } else if (state.picking === 'start') {
      hint.textContent = '请选择开始日期';
    } else {
      hint.textContent = '选择开始 / 结束日期';
    }
  }
}

function openRangePop() {
  const pop = $('range-pop');
  if (!pop) return;
  const d = parseD(state.range.end) || parseD(state.range.start) || todayD();
  state.cal.left = { y: d.getFullYear(), m: d.getMonth() };
  state.picking = '';
  renderPresets();
  renderCalendar();
  pop.classList.remove('hidden');
}

function closeRangePop() {
  const pop = $('range-pop');
  if (pop) pop.classList.add('hidden');
}

function applyRange(start, end, label, silent) {
  const s = start || '';
  const e = end || '';
  state.range = { start: s, end: e, label: label || (s + ' ~ ' + e) };
  state.picking = '';
  state.days = rangeDays();
  const lb = $('range-label');
  if (lb) lb.textContent = state.range.label;
  if (e || s) state.date = e || s;
  renderPresets();
  closeRangePop();
  if (silent) return;
  if (state.view === 'search') { runSearch(); return; }
  if (state.view === 'messages') { loadMessages(true); return; }
  loadStats();
  renderSummary();
}

function initRangePicker() {
  const btn = $('btn-range');
  const pop = $('range-pop');
  if (!btn || !pop) return;
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (pop.classList.contains('hidden')) openRangePop(); else closeRangePop();
  });
  const prev = $('rp-prev');
  const next = $('rp-next');
  if (prev) prev.addEventListener('click', (ev) => { ev.stopPropagation(); calShift(-1); });
  if (next) next.addEventListener('click', (ev) => { ev.stopPropagation(); calShift(1); });
  pop.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const p = ev.target.closest && ev.target.closest('[data-preset]');
    if (p) {
      const key = p.dataset.preset;
      if (key === 'custom') {
        state.range = { start: '', end: '', label: '自定义' };
        state.picking = 'start';
        const lb = $('range-label');
        if (lb) lb.textContent = '自定义';
        renderPresets();
        renderCalendar();
        return;
      }
      const pr = presetRange(key);
      if (pr) applyRange(pr[0], pr[1], pr[2]);
      return;
    }
    const day = ev.target.closest && ev.target.closest('[data-day]');
    if (!day || day.classList.contains('future') || day.classList.contains('empty')) return;
    const ds = day.dataset.day;
    if (!state.picking || state.picking === 'start') {
      state.picking = 'end';
      state.range = { start: ds, end: '', label: '自定义' };
      renderCalendar();
      return;
    }
    let s = state.range.start || ds;
    let e = ds;
    if (e < s) { const t = s; s = e; e = t; }
    applyRange(s, e, '自定义');
  });
  document.addEventListener('click', () => closeRangePop());
}

function latestDate() {
  const arr = state.dates || [];
  return arr.length ? arr[arr.length - 1].date : '';
}

function curDate() {
  return state.range.end || state.range.start || state.date || latestDate() || '';
}

async function loadStats() {
  syncTargetFromSelect();
  if (!state.target) return;
  try {
    const params = {
      chat: state.target.chat,
      target_id: state.target.target,
    };
    if (state.range.start || state.range.end) {
      params.start = state.range.start || '';
      params.end = state.range.end || '';
    } else if (state.range.label === '全部时间') {
      params.days = 365;
    } else {
      params.days = rangeDays();
    }
    const data = await apiGet('stats', params);
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

/* 阶段 3 已实现，占位提示函数已移除 */

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
  const chatBox = $('chat');
  if (chatBox) {
    chatBox.addEventListener('scroll', () => {
      if (chatBox.scrollTop <= 24) loadOlder();
    });
  }
  $('sel-target').addEventListener('change', () => {
    syncTargetFromSelect();
    state.chat = [];
    state.offset = 0;
    loadDates().then(() => {
      if (state.view === 'stats') { loadStats(); loadSummary(); } else loadMessages(true);
    });
  });
  initRangePicker();
  $('btn-reload').addEventListener('click', () => {
    if (state.view === 'messages') { loadDates().then(() => loadMessages(true)); }
    else if (state.view === 'stats') { loadDates().then(() => { loadStats(); loadSummary(); }); }
  });
  $('btn-reload-cfg').addEventListener('click', loadConfig);
  $('btn-save-cfg').addEventListener('click', saveConfig);
  $('btn-names').addEventListener('click', () => {
    toast('正在刷新成员昵称…');
    ensureNames(true).then(() => { renderChat(); });
  });
  $('btn-search').addEventListener('click', runSearch);
  $('q').addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });
  $('btn-archive').addEventListener('click', triggerArchive);
  $('btn-sum-gen').addEventListener('click', () => genSummary(false));
  $('btn-sum-force').addEventListener('click', () => genSummary(true));
  $('btn-brief').addEventListener('click', showBriefing);
  document.addEventListener('click', (e) => {
    const chip = e.target.closest && e.target.closest('.reply-chip');
    if (chip) { onReplyClick(chip.dataset.reply); return; }
    const go = e.target.closest && e.target.closest('[data-goto-date]');
    if (go) { gotoHit(go.dataset.gotoDate, go.dataset.gotoSeq); }
    const hist = e.target.closest && e.target.closest('[data-hit-date]');
    if (hist) {
      const d = hist.dataset.hitDate;
      $('summary-text').innerHTML = '<div class="summary-title">' + esc(d) + '（历史）</div>'
        + '<div class="summary-body">' + esc(state.summaries[d] || '') + '</div>';
    }
  });

  try {
    const cfg = await apiGet('config');
    state.config = (cfg && cfg.config) || {};
    state.pageSize = Math.max(10, Math.min(500, Number(state.config.ui_messages_page_size) || 50));
  } catch (e) { /* 忽略，用默认值 */ }

  const pr = presetRange('d30');
  if (pr) applyRange(pr[0], pr[1], pr[2], true);
  const tab = state.config.ui_default_tab || 'overview';
  switchView(VIEWS.indexOf(tab) >= 0 ? tab : 'overview');
}

boot();

/* ---------- v2.3.0 增强：@昵称、引用跳转、自己人靠右、搜索、总结 ---------- */

function selfIds() {
  const raw = state.config.archive_bots;
  if (Array.isArray(raw)) return raw.map((x) => String(x));
  if (typeof raw === 'string') return raw.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
  return [];
}

function isSelf(uid) {
  return !!uid && selfIds().indexOf(String(uid)) >= 0;
}

function textWithAt(text) {
  return esc(text || '').replace(/\[At:(\d+)\]/g, (m, qq) => {
    const nm = state.names[qq];
    return nm ? ('<span class="at">@' + esc(nm) + '</span>')
      : ('<span class="at">@' + qq + '</span>');
  });
}

function replyChip(m) {
  const rep = m.reply;
  if (!rep || typeof rep !== 'object') return '';
  const who = rep.nickname || (rep.qq ? ('QQ' + rep.qq) : '引用消息');
  const tip = rep.text ? String(rep.text).slice(0, 60) : '';
  const key = rep.id || rep.message_id || rep.seq || '';
  return '<div class="reply-chip" data-reply="' + esc(key) + '">引用 ' + esc(who)
    + (tip ? '：' + esc(tip) : '') + '</div>';
}

function bubble(m) {
  const uid = m.sender_id || '';
  const nick = m.sender_name || uid || '?';
  const cached = AVATARS[uid];
  const inner = cached ? '<img class="av-img" src="' + cached + '" alt="" />'
    : esc(String(nick).slice(0, 1));
  const cls = isSelf(uid) ? 'bubble self' : 'bubble';
  return '<div class="' + cls + '" data-seq="' + esc(m.seq == null ? '' : m.seq) + '">'
    + '<div class="av" data-uid="' + esc(uid) + '">' + inner + '</div>'
    + '<div class="body"><div class="who"><b>' + esc(nick) + '</b> <span>'
    + esc(m.time || '') + '</span></div>' + replyChip(m)
    + '<div class="txt">' + textWithAt(m.text) + '</div></div></div>';
}

async function ensureNames(refresh) {
  syncTargetFromSelect();
  if (!state.target) return;
  const key = targetKey(state.target);
  if (!refresh && state.namesKey === key && Object.keys(state.names).length) return;
  try {
    const data = await apiGet('members', {
      chat: state.target.chat,
      target_id: state.target.target,
      refresh: refresh ? 1 : 0,
    });
    state.names = (data && data.names) || {};
    state.namesKey = key;
  } catch (e) {
    state.names = {};
  }
}

async function onReplyClick(key) {
  if (!key) return;
  syncTargetFromSelect();
  if (!state.target) return;
  toast('正在定位引用消息…');
  try {
    const loc = await apiGet('locate', {
      chat: state.target.chat,
      target_id: state.target.target,
      id: key,
    });
    if (loc && loc.date) {
      if (loc.date !== state.range.start || loc.date !== state.range.end) {
        applyRange(loc.date, loc.date, loc.date, true);
        state.offset = 0;
        await loadMessages(true);
      }
      highlightAt(loc.index);
    } else {
      toast('该引用消息不在归档中', true);
    }
  } catch (e) {
    toast('定位失败：' + errText(e), true);
  }
}

function highlightAt(index) {
  const box = $('chat');
  if (!box) return;
  const nodes = box.querySelectorAll('.bubble');
  if (!nodes.length) return;
  const total = state.chat.length;
  let node = null;
  if (index >= 0 && index < total) node = nodes[Math.max(0, total - 1 - index)] || null;
  if (!node) return;
  node.classList.add('hl');
  node.scrollIntoView({ block: 'center' });
  setTimeout(() => node.classList.remove('hl'), 2000);
}

async function runSearch() {
  syncTargetFromSelect();
  if (!state.target) {
    $('search-result').innerHTML = '<div class="muted pad">请先选择目标</div>';
    return;
  }
  const box = $('search-result');
  box.innerHTML = '<div class="muted pad">搜索中…</div>';
  try {
    const data = await apiGet('search', {
      chat: state.target.chat,
      target_id: state.target.target,
      q: $('q').value || '',
      user: $('q-user').value || '',
      limit: 200,
      start: state.range.start || '',
      end: state.range.end || '',
    });
    const items = (data && data.items) || [];
    if (!items.length) {
      box.innerHTML = '<div class="muted pad">没有匹配的消息</div>';
      return;
    }
    box.innerHTML = '<div class="muted pad">共 ' + (data.total || items.length) + ' 条，显示最近 ' + items.length + ' 条</div>'
      + items.map((m) =>
        '<div class="hit"><span class="hit-time">' + esc(m.time) + '</span>'
        + '<b>' + esc(m.sender_name || m.sender_id) + '</b>'
        + '<span class="hit-text">' + textWithAt(m.text) + '</span>'
        + '<button class="ghost mini" data-goto-date="' + esc(m.date)
        + '" data-goto-seq="' + esc(m.seq == null ? '' : m.seq) + '">定位</button></div>').join('');
  } catch (e) {
    box.innerHTML = '<div class="muted pad">搜索失败：' + esc(errText(e)) + '</div>';
  }
}

async function gotoHit(date, seq) {
  if (date) applyRange(date, date, date, true);
  state.offset = 0;
  await loadMessages(true);
  switchView('messages');
  if (seq) setTimeout(() => highlightAt(parseInt(seq, 10)), 300);
}

async function genSummary(force) {
  syncTargetFromSelect();
  if (!state.target) return;
  const box = $('summary-text');
  const sumDate = curDate();
  if (!sumDate) {
    box.innerHTML = '<span class="muted">请先在上方选择日期范围</span>';
    return;
  }
  box.innerHTML = '<span class="muted">正在生成…（队列串行，请稍等）</span>';
  try {
    const res = await apiPost('summary', {
      chat: state.target.chat,
      target_id: state.target.target,
      date: sumDate,
      trend: !!($('sum-trend') && $('sum-trend').checked),
      force: !!force,
    });
    box.innerHTML = '<div class="summary-title">' + esc((res && res.date) || '')
      + ((res && res.cached) ? '（已缓存）' : '（新生成）') + '</div>'
      + '<div class="summary-body">' + esc((res && res.summary) || '') + '</div>';
    if (res && res.date && res.summary) {
      state.summaries[res.date] = res.summary;
      renderSummary();
    }
  } catch (e) {
    box.innerHTML = '<span class="muted">总结失败：' + esc(errText(e)) + '</span>';
  }
}

async function showBriefing() {
  const box = $('brief-text') || $('summary-text');
  box.innerHTML = '<span class="muted">正在获取简报…</span>';
  try {
    let res = null;
    try {
      res = await apiGet('briefing', { days: rangeDays(), get: 1 });
    } catch (e) {
      res = null;
    }
    if (!res || !res.brief) {
      res = await apiGet('briefing', { days: rangeDays() });
    }
    box.innerHTML = '<div class="summary-title">简报 ' + esc((res && res.date) || '')
      + ((res && res.cached) ? '（已缓存）' : '') + '</div>'
      + '<div class="summary-body">' + esc((res && res.brief) || '（空）') + '</div>';
  } catch (e) {
    box.innerHTML = '<span class="muted">简报失败：' + esc(errText(e)) + '</span>';
  }
}

async function triggerArchive() {
  try {
    const res = await apiPost('archive', {});
    toast((res && res.busy) ? '已有归档任务在执行' : '已触发归档，稍后刷新查看');
    setTimeout(loadOverview, 3000);
  } catch (e) {
    toast('触发失败：' + errText(e), true);
  }
}

/* ---------- v2.3.0：滚动加载 + 按目标总结 / 总结历史 ---------- */

function updateMsgHint() {
  const el = $('msg-hint');
  if (!el) return;
  if (state.loadingMore) {
    el.textContent = '正在加载更早消息…';
    return;
  }
  if (!state.chat.length) {
    el.textContent = '';
    return;
  }
  el.textContent = state.hasMore ? '向上滑动自动加载更早消息' : '已经到最早一条了';
}

async function loadOlder() {
  if (state.loadingMore || !state.hasMore || state.view !== 'messages') return;
  const box = $('chat');
  if (!box) return;
  state.loadingMore = true;
  updateMsgHint();
  const prevHeight = box.scrollHeight;
  const prevTop = box.scrollTop;
  state.offset += state.pageSize;
  try {
    await loadMessages(false);
  } finally {
    state.loadingMore = false;
    updateMsgHint();
  }
  /* 保持视口位置：向上加载后内容变长，不跳走 */
  box.scrollTop = box.scrollHeight - prevHeight + prevTop;
}

async function loadSummary(force) {
  syncTargetFromSelect();
  if (!state.target) return;
  const key = targetKey(state.target);
  if (!force && state.summariesKey === key) {
    renderSummary();
    return;
  }
  const hist = $('summary-history');
  if (hist) hist.innerHTML = '<div class="muted pad">加载中…</div>';
  try {
    const data = await apiGet('summary_get', {
      chat: state.target.chat,
      target_id: state.target.target,
    });
    const items = (data && data.items) || [];
    const map = {};
    items.forEach((it) => { if (it && it.date) map[it.date] = it.summary || ''; });
    state.summaries = map;
    state.summariesKey = key;
  } catch (e) {
    state.summaries = {};
    state.summariesKey = key;
    if (hist) hist.innerHTML = '<div class="muted pad">总结历史加载失败：' + esc(errText(e)) + '</div>';
  }
  renderSummary();
}

function renderSummary() {
  const box = $('summary-text');
  const date = curDate();
  const multi = !!(state.range.start && state.range.end && state.range.start !== state.range.end);
  const btnGen = $('btn-sum-gen');
  if (btnGen) btnGen.textContent = date ? ('总结 ' + date) : '总结所选日期';
  const cur = (state.summaries || {})[date];
  const t = state.target;
  const label = t ? ((t.chat === 'private' ? '私聊 ' : '群聊 ') + t.target) : '';
  if (box) {
    if (cur) {
      box.innerHTML = '<div class="summary-title">' + esc(label + ' · ' + date
      + (multi ? '（范围结束日）' : '') + '（已生成）') + '</div>'
        + '<div class="summary-body">' + esc(cur) + '</div>';
    } else {
      box.innerHTML = '<span class="muted">' + esc(label)
        + (date ? (' · ' + esc(date)) : '') + ' 还没有总结，可点上方按钮生成。'
        + (multi ? '（当前范围含多天，总结按结束日 ' + esc(date) + ' 生成）' : '')
        + '</span>';
    }
  }
  const hist = $('summary-history');
  if (!hist) return;
  const dates = Object.keys(state.summaries || {}).sort().reverse();
  if (!dates.length) {
    hist.innerHTML = '<div class="muted pad">该目标暂无历史总结</div>';
    return;
  }
  hist.innerHTML = dates.map((d) =>
    '<div class="hist-item" data-hit-date="' + esc(d) + '">'
    + '<span class="hist-date">' + esc(d) + '</span>'
    + '<span class="hist-preview">' + esc(String(state.summaries[d] || '').slice(0, 80)) + '</span>'
    + '</div>').join('');
}
