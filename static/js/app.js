/* Resilio 管理面板 — 轮询 & 交互逻辑 */
'use strict';

let pollUrl = null;
let pollTimer = null;
const POLL_INTERVAL = 3000;

function getPollInterval() {
  const v = parseInt(localStorage.getItem('rsync_poll_interval'), 10);
  return (v && v >= 1000) ? v : POLL_INTERVAL;
}

function el(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function fmtSpeed(bps) {
  bps = bps || 0;
  if (bps <= 0) return '0 B/s';
  if (bps < 1024) return bps + ' B/s';
  if (bps < 1024 * 1024) return (bps / 1024).toFixed(1) + ' KB/s';
  return (bps / 1024 / 1024).toFixed(2) + ' MB/s';
}
function fmtSize(bytes) {
  bytes = bytes || 0;
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
}

async function fetchJSON(url, options) {
  const resp = await fetch(url, options);
  if (resp.status === 401) { window.location.href = '/login'; throw new Error('未登录'); }
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 非 JSON */ }
  if (!resp.ok || (data && data.ok === false)) {
    throw new Error((data && data.error) || ('请求失败 (' + resp.status + ')'));
  }
  return data;
}

function setConn(state, msg) {
  const s = el('conn-state');
  if (!s) return;
  if (state === 'ok') { s.textContent = '已连接'; s.className = 'ms-2 text-success'; }
  else { s.textContent = '连接失败: ' + msg; s.className = 'ms-2 text-danger'; }
}

function renderPage(data) {
  if (window.PAGE === 'dashboard') updateDashboard(data);
  else if (window.PAGE === 'peers') updatePeers(data);
  else if (window.PAGE === 'sched') updateSched(data);
  else if (window.PAGE === 'guard') updateGuard(data);
}

async function doPoll() {
  if (document.hidden || !pollUrl) return;
  try {
    const data = await fetchJSON(pollUrl);
    renderPage(data);
    setConn('ok');
  } catch (e) {
    setConn('error', e.message);
  }
}

function startPolling(url, interval) {
  pollUrl = url;
  doPoll();
  pollTimer = setInterval(doPoll, interval || getPollInterval());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) doPoll(); });
}

function refreshNow() {
  if (pollUrl) fetchJSON(pollUrl).then(renderPage).catch(() => {});
}

/* ---------- 仪表盘 ---------- */

const STATUS_META = {
  downloading:    { label: '↓ 正在接收',  cls: 'bg-info text-dark' },
  uploading:      { label: '↑ 正在发送',  cls: 'bg-primary text-white' },
  syncing:        { label: '↓↑ 同步中',  cls: 'bg-warning text-dark' },
  incomplete:     { label: '未完成',     cls: 'bg-warning text-dark' },
  indexing:       { label: '正在索引',   cls: 'bg-info text-dark' },
  nopeers:        { label: '等待节点',   cls: 'bg-secondary' },
  pending:        { label: '等待中',     cls: 'bg-secondary' },
  stopped:        { label: '已停止',     cls: 'bg-secondary' },
  neverconnected: { label: '从未连接',   cls: 'bg-secondary' },
  invalid:        { label: '无效',       cls: 'bg-danger' },
  paused:         { label: '已暂停',     cls: 'bg-secondary' },
  error:          { label: '错误',       cls: 'bg-danger' },
  disconnected:   { label: '断开连接',   cls: 'bg-dark' },
  uptodate:       { label: '已同步',     cls: 'bg-success' },
};

let dashboardFolders = [];
let dashboardData = null;
let selectedIds = new Set();
let sortField = 'name';
let sortAsc = true;

function sortFolders(folders) {
  return folders.slice().sort((a, b) => {
    let va = a[sortField];
    let vb = b[sortField];
    if (sortField === 'status') {
      va = (va == null) ? -1 : va;
      vb = (vb == null) ? -1 : vb;
    }
    let cmp;
    if (typeof va === 'number' && typeof vb === 'number') {
      cmp = va - vb;
    } else {
      cmp = String(va == null ? '' : va).localeCompare(String(vb == null ? '' : vb), 'zh');
    }
    return sortAsc ? cmp : -cmp;
  });
}

function updateSortIndicators() {
  document.querySelectorAll('th[data-sort] .sort-ind').forEach(s => { s.textContent = ''; });
  const active = document.querySelector('th[data-sort="' + sortField + '"] .sort-ind');
  if (active) active.textContent = sortAsc ? '▲' : '▼';
}

function setLoadBar(textId, val) {
  const txt = el(textId);
  if (!txt) return;
  const v = Math.max(0, Math.min(100, Math.round(Number(val) || 0)));
  txt.textContent = v + '%';
  txt.classList.remove('text-danger', 'text-warning', 'text-dark');
  txt.classList.add(v >= 90 ? 'text-danger' : v >= 70 ? 'text-warning' : 'text-dark');
}

function updateMem(txt, m) {
  if (!txt) return;
  if (!m || (m.rss == null && m.cg_current == null)) {
    txt.textContent = '-';
    txt.classList.remove('text-danger', 'text-warning', 'text-success');
    txt.title = '';
    return;
  }
  const rss = m.rss != null ? m.rss : m.cg_current;
  txt.classList.remove('text-danger', 'text-warning', 'text-success');
  if (m.cg_max) {
    const pct = rss / m.cg_max;
    txt.textContent = Math.round(pct * 100) + '%';
    txt.classList.add(pct >= 0.9 ? 'text-danger' : pct >= 0.7 ? 'text-warning' : 'text-success');
  } else {
    txt.textContent = fmtSize(rss);
    txt.classList.add('text-dark');
  }
  let tip = 'rslsync RSS ' + fmtSize(rss);
  if (m.cg_max) tip += ' / cgroup 上限 ' + fmtSize(m.cg_max);
  if (m.vmswap) tip += ' · swap ' + fmtSize(m.vmswap);
  if (m.mem_avail != null) tip += ' · 系统可用 ' + fmtSize(m.mem_avail);
  txt.title = tip;
}

function fmtAgo(ts) {
  if (!ts) return '无';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return '刚刚';
  if (s < 3600) return Math.round(s / 60) + ' 分钟前';
  if (s < 86400) return Math.round(s / 3600) + ' 小时前';
  return Math.round(s / 86400) + ' 天前';
}

function fmtCountdown(ts) {
  const s = Math.max(0, Math.round(ts - Date.now() / 1000));
  if (s < 60) return '1 分钟内';
  if (s < 3600) return Math.round(s / 60) + ' 分钟后';
  if (s < 86400) return Math.round(s / 3600) + ' 小时后';
  return Math.round(s / 86400) + ' 天后';
}

function fmtInterval(sec) {
  if (!sec) return '-';
  if (sec < 60) return sec + ' 秒';
  if (sec < 3600) return Math.round(sec / 60) + ' 分钟';
  return Math.round(sec / 3600) + ' 小时';
}

function updateReconnect(r) {
  const card = el('guard-reconnect-card');
  if (!card) return;
  if (!r || r.total == null) { card.classList.add('d-none'); return; }
  card.classList.remove('d-none');
  const conn = r.connected || 0;
  const total = r.total;
  const disc = r.disconnected != null ? r.disconnected : total - conn;
  const pct = total ? Math.round(conn / total * 100) : 0;
  const ioBlocked = r.io_ready && r.io_pct != null && r.io_pct >= (r.io_gate_pct || 90);
  const bar = el('rc-progress');
  bar.style.width = pct + '%';
  bar.classList.toggle('bg-warning', !!(r.on && disc > 0 && ioBlocked));
  el('rc-count').textContent = '当前已连接 ' + conn + ' / ' + total
    + '（累计重连成功 ' + (r.reconnect_ok_total || 0) + ' 次）';

  const badge = el('rc-badge');
  if (!r.on) { badge.textContent = '已关闭'; badge.className = 'badge bg-secondary'; }
  else if (disc === 0) { badge.textContent = '全部已重连'; badge.className = 'badge bg-success'; }
  else if (ioBlocked) { badge.textContent = 'IO 闸门拦截中'; badge.className = 'badge bg-warning text-dark'; }
  else { badge.textContent = '运行中'; badge.className = 'badge bg-primary'; }

  const next = el('rc-next');
  if (!r.on) next.textContent = '已关闭';
  else if (disc === 0) next.textContent = '无待重连';
  else if (r.next_connect && r.next_connect > Date.now() / 1000) next.textContent = fmtCountdown(r.next_connect);
  else if (ioBlocked) next.textContent = '等待 IO 回落';
  else next.textContent = '可重连';

  el('rc-last').textContent = fmtAgo(r.last_reconnect_ok);
  const io = el('rc-io');
  io.classList.remove('text-warning');
  if (r.io_ready && r.io_pct != null) {
    io.textContent = Math.round(r.io_pct) + '%' + (r.io_gate_pct != null ? '（闸门 ' + r.io_gate_pct + '%）' : '');
    if (ioBlocked) io.classList.add('text-warning');
  } else {
    io.textContent = '-';
  }
  el('rc-interval').textContent = fmtInterval(r.interval);
}

function updateDashboard(data) {
  dashboardData = data;
  const folders = data.folders || [];
  const sorted = sortFolders(folders);
  dashboardFolders = sorted;
  updateSortIndicators();
  updateBatchUI();
  const t = data.total || {};
  const st = data.statuses || {};
  el('stat-count').textContent = t.count != null ? t.count : folders.length;
  el('stat-syncing').textContent = t.syncing != null ? t.syncing : '-';
  el('stat-paused').textContent = t.paused != null ? t.paused : '-';
  el('stat-disconnected').textContent = t.disconnected != null ? t.disconnected : '-';
  el('stat-dl').textContent = fmtSpeed(t.download_speed);
  el('stat-ul').textContent = fmtSpeed(t.upload_speed);
  setLoadBar('stat-cpu', st.cpu);
  setLoadBar('stat-disk', st.disk);
  updateMem(el('stat-mem'), data.memory);
  updateReconnect(data.guard_reconnect);
  const s = data.session || {};
  if (s.max_speed) {
    el('ss-max-down').textContent = fmtSpeed(s.max_speed.down);
    el('ss-max-up').textContent = fmtSpeed(s.max_speed.up);
    // 与官方 WebUI 一致：已转送/终生已转送 = 下行+上行合并为单值（悬停看明细）
    const tr = s.transferred || {};
    const tt = s.total_transferred || {};
    const trSum = (tr.down || 0) + (tr.up || 0);
    const ttSum = (tt.down || 0) + (tt.up || 0);
    el('ss-trans').textContent = fmtSize(trSum);
    el('ss-trans').title = '↓ ' + fmtSize(tr.down || 0) + ' ↑ ' + fmtSize(tr.up || 0);
    el('ss-total').textContent = fmtSize(ttSum);
    el('ss-total').title = '↓ ' + fmtSize(tt.down || 0) + ' ↑ ' + fmtSize(tt.up || 0);
    const ss = el('session-stats');
    if (ss) ss.classList.remove('d-none');
  }
  if (data._ts) {
    el('last-update').textContent = new Date(data._ts * 1000).toLocaleTimeString();
  } else {
    el('last-update').textContent = new Date().toLocaleTimeString();
  }

  const body = el('folder-body');
  if (!folders.length) {
    body.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-4">没有文件夹</td></tr>';
    return;
  }
  body.innerHTML = sorted.map((f, idx) => {
    const s = STATUS_META[f.display] || STATUS_META.uptodate;
    const progress = Math.max(0, Math.min(100, Math.round(f.progress || 0)));
    const active = ['syncing', 'downloading', 'uploading', 'incomplete'].includes(f.display);
    const strip = active ? ' progress-bar-striped progress-bar-animated' : '';
    let statusTitle = '';
    if (f.display === 'error' && f.errors && f.errors.length) {
      const d = f.errors[0].data || {};
      statusTitle = ' title="' + esc(d.description || '同步错误') + '"';
    }
    let statusCell = '<span class="badge ' + s.cls + '"' + statusTitle + '>' + s.label + '</span>';
    if (f.warning && f.errors && f.errors.length) {
      const d = f.errors[0].data || {};
      statusCell += ' <span class="badge bg-danger text-white" title="' + esc(d.description || '同步错误') + '">错误</span>';
    }
    const gradeBadge = f.grade
      ? '<span class="badge ' + (SCHED_GRADE[f.grade] ? SCHED_GRADE[f.grade].cls : 'bg-secondary') +
        ' me-1" title="调度分级 ' + f.grade + '">' + f.grade + '</span>'
      : '';
    return '<tr data-idx="' + idx + '">' +
      '<td class="text-center"><input type="checkbox" class="row-check" data-id="' + esc(f.id) + '"' +
        (selectedIds.has(f.id) ? ' checked' : '') + '></td>' +
      '<td class="fw-semibold text-truncate" title="' + esc(f.name) + '">' + gradeBadge + esc(f.name) + '</td>' +
      '<td class="text-muted small text-truncate" title="' + esc(f.path) + '">' + esc(f.path) + '</td>' +
      '<td>' + statusCell + '</td>' +
      '<td>' +
        '<div class="progress" style="height:8px"><div class="progress-bar' + strip + '" style="width:' + progress + '%"></div></div>' +
        '<div class="small text-muted mt-1">' + progress + '%</div>' +
      '</td>' +
      '<td class="small text-nowrap">' + fmtSpeed(f.download_speed) + '</td>' +
      '<td class="small text-nowrap">' + fmtSpeed(f.upload_speed) + '</td>' +
      '<td><a href="#" class="peers-link" data-idx="' + idx + '" title="在线用户 ' +
        (f.peers_connected != null ? f.peers_connected : 0) + '/' + (f.peers_total != null ? f.peers_total : 0) + '">' +
        (f.peers_connected != null ? f.peers_connected : 0) + '/' + (f.peers_total != null ? f.peers_total : 0) + '</a></td>' +
      '<td class="small text-nowrap">' + fmtSize(f.size) + '</td>' +
      '<td class="text-end">' +
        '<button class="btn btn-sm btn-outline-secondary row-menu" data-idx="' + idx + '" title="操作">⋯</button>' +
      '</td>' +
    '</tr>';
  }).join('');
}

function showFolderPeers(idx) {
  const f = dashboardFolders[idx];
  if (!f) return;
  el('peersModalTitle').textContent = '节点 - ' + (f.name || '');
  const body = el('peersModalBody');
  const peers = f.peers || [];
  if (!peers.length) {
    body.innerHTML = '<p class="text-muted mb-0">暂无节点</p>';
  } else {
    body.innerHTML = peers.map(p => {
      const online = !!p.online;
      const lines = [];
      if (p.downfiles > 0) {
        lines.push('<span class="text-info">↓ 接收 ' + p.downfiles + ' 个文件（' + fmtSize(p.downdiff) + '）</span>');
      }
      if (p.upfiles > 0) {
        lines.push('<span class="text-primary">↑ 发送 ' + p.upfiles + ' 个文件（' + fmtSize(p.updiff) + '）</span>');
      }
      lines.push('对方共 ' + (p.has_files != null ? p.has_files : 0) + ' 个文件');
      if (p.lastsynctime) {
        lines.push('最后同步 ' + new Date(p.lastsynctime * 1000).toLocaleString());
      }
      return '<div class="d-flex justify-content-between align-items-center border-bottom py-2">' +
        '<div>' +
          '<span class="fw-semibold">' + esc(p.name || p.id || '-') + '</span>' +
          '<div class="small text-muted mt-1">' + lines.join(' · ') + '</div>' +
        '</div>' +
        '<span class="badge ' + (online ? 'bg-success' : 'bg-secondary') + ' ms-2">' + (online ? '在线' : '离线') + '</span>' +
      '</div>';
    }).join('');
  }
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('peersModal'));
  modal.show();
}

/* ---------- 节点 ---------- */

function updatePeers(data) {
  const peers = data.peers || [];
  el('last-update').textContent = new Date().toLocaleTimeString();
  const body = el('peers-body');
  if (!peers.length) {
    body.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">暂无节点</td></tr>';
    return;
  }
  body.innerHTML = peers.map(p => {
    const online = !!p.online;
    const folders = (p.folders || []).join(', ') || '-';
    const name = p.name || p.id || '-';
    const id = p.id || '-';
    return '<tr>' +
      '<td class="fw-semibold text-truncate" title="' + esc(name) + '">' + esc(name) + '</td>' +
      '<td class="text-muted small text-truncate" title="' + esc(id) + '">' + esc(id) + '</td>' +
      '<td><span class="badge ' + (online ? 'bg-success' : 'bg-secondary') + '">' + (online ? '在线' : '离线') + '</span></td>' +
      '<td class="small text-nowrap">' + fmtSpeed(p.down) + '</td>' +
      '<td class="small text-nowrap">' + fmtSpeed(p.up) + '</td>' +
      '<td class="small text-truncate" title="' + esc(folders) + '">' + esc(folders) + '</td>' +
    '</tr>';
  }).join('');
}

const SCHED_GRADE = {
  D: { cls: 'bg-danger', label: 'D', desc: '有下载需求' },
  C: { cls: 'bg-warning text-dark', label: 'C', desc: '上传需求、做种不足' },
  B: { cls: 'bg-success', label: 'B', desc: '做种充足' },
  A: { cls: 'bg-secondary', label: 'A', desc: '无需求' },
};

function updateSched(data) {
  const list = data.sched || [];
  if (data._ts) {
    el('last-update').textContent = new Date(data._ts * 1000).toLocaleTimeString();
  } else {
    el('last-update').textContent = new Date().toLocaleTimeString();
  }
  const body = el('sched-body');
  if (!list.length) {
    body.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-4">没有文件夹</td></tr>';
    return;
  }
  body.innerHTML = list.map(s => {
    const g = SCHED_GRADE[s.grade] || SCHED_GRADE.A;
    const state = s.running
      ? '<span class="badge bg-success">运行</span>'
      : '<span class="badge bg-secondary">暂停</span>';
    const rec = s.grade === 'D' ? '优先运行（有下载需求）'
      : s.grade === 'C' ? '建议运行（有上传需求、做种不足）'
      : s.grade === 'B' ? '可暂停（做种充足）'
      : '可暂停（无需求）';
    return '<tr>' +
      '<td class="fw-semibold text-truncate" title="' + esc(s.name) + '">' + esc(s.name) + '</td>' +
      '<td><span class="badge ' + g.cls + '" title="' + g.desc + '">' + g.label + '</span></td>' +
      '<td class="fw-bold">' + (s.score != null ? s.score : '-') + '</td>' +
      '<td>' + s.need + '</td>' +
      '<td>' + s.seeded + '</td>' +
      '<td>' + s.dneed + '</td>' +
      '<td>' + state + '</td>' +
      '<td class="small text-muted">' + rec + '</td>' +
    '</tr>';
  }).join('');
}

async function loadSchedHistoryDetail() {
  const box = el('sched-history-body');
  if (!box) return;
  const limit = (el('hist-limit') && el('hist-limit').value) || '50';
  const showSummary = !!(el('hist-show-summary') && el('hist-show-summary').checked);
  // 刷新文件夹名映射（id 前缀 -> 真实名）：用全量 folders（含断开文件夹），sched 排除了断开项
  const nameMap = {};
  try {
    const st = await fetchJSON('/api/status');
    (st.folders || []).forEach(f => { if (f.id) nameMap[f.id.slice(0, 8)] = f.name; });
  } catch (_) { /* 映射失败则显示 id 前缀 */ }
  let history;
  try {
    history = (await fetchJSON('/api/guard/history?limit=' + limit)).history || [];
  } catch (err) {
    box.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-4">' + esc(err.message) + '</td></tr>';
    return;
  }
  if (!history.length) {
    box.innerHTML = '<tr><td colspan="10" class="text-center text-muted py-4">暂无调度记录（调度器启用后产生）</td></tr>';
    return;
  }
  const rows = history.map(line => renderHistoryLine(line, nameMap))
    .filter(r => showSummary || !r.summary);
  box.innerHTML = rows.length
    ? rows.map(r => r.html).join('')
    : '<tr><td colspan="10" class="text-center text-muted py-4">当前时段无「暂停/恢复/断开/重连」动作记录（勾选「显示周期汇总」可查看全部）</td></tr>';
}

function actionRow(time, cycle, badge, badgeCls, name, grade, metrics, note) {
  const gm = SCHED_GRADE[grade] || null;
  return '<tr>' +
    '<td class="small text-muted text-nowrap">' + esc(time) + '</td>' +
    '<td class="small text-muted">' + (cycle ? '#' + cycle : '') + '</td>' +
    '<td><span class="badge ' + badgeCls + '">' + esc(badge) + '</span></td>' +
    '<td class="fw-semibold">' + esc(name) + '</td>' +
    '<td>' + (gm ? '<span class="badge ' + gm.cls + '" title="' + gm.desc + '">' + esc(grade) + '</span>' : '') + '</td>' +
    '<td>' + metrics[0] + '</td><td>' + metrics[1] + '</td><td>' + metrics[2] + '</td>' +
    '<td class="small text-nowrap">' + (metrics[3] ? metrics[3] + ' KB/s' : '') + '</td>' +
    '<td class="small text-muted">' + esc(note) + '</td>' +
    '</tr>';
}

function renderHistoryLine(line, nameMap) {
  const mTime = line.match(/\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]/);
  const mCycle = line.match(/周期#(\d+)/);
  const time = mTime ? (mTime[1].slice(5) + ' ' + mTime[2]) : '';
  const cycle = mCycle ? mCycle[1] : '';
  const mFold = line.match(/fold=(\w+)/);
  const foldId = mFold ? mFold[1] : '';
  const name = foldId ? (nameMap[foldId] || ('id:' + foldId)) : '';

  // 断开 / 重连 动作行（guard 冗余逻辑，无 动作=；周期与指标列留空，备注放明细）
  const mDisc = line.match(/冗余做种断开: fold=\w+ seeded=(\d+)>(\d+)/);
  if (mDisc) {
    return { summary: false, html: actionRow(time, '', '断开', 'bg-danger text-white', name, '',
      ['', '', '', ''], '做种 ' + mDisc[1] + ' 超阈值 ' + mDisc[2]) };
  }
  if (line.indexOf('定期重连: fold=') >= 0) {
    return { summary: false, html: actionRow(time, '', '重连', 'bg-primary text-white', name, '',
      ['', '', '', ''], '已重连') };
  }

  const mAction = line.match(/动作=(\S+)/);
  if (mAction && mFold) {
    const gm = line.match(/C([DCBA])\b/);
    const grade = gm ? gm[1] : '-';
    const isPause = mAction[1] === '暂停';
    const need = (line.match(/need=(\d+)/) || [])[1] || '0';
    const dneed = (line.match(/dneed=(\d+)/) || [])[1] || '0';
    const seed = (line.match(/seed=(\d+)/) || [])[1] || '0';
    const up = (line.match(/up=(\d+)KB/) || [])[1] || '0';
    return { summary: false, html: actionRow(time, cycle, mAction[1],
      (isPause ? 'bg-warning text-dark' : 'bg-success'), name, grade,
      [need, dneed, seed, up], '') };
  }
  // 周期汇总行：只切掉首个 "] "（时间戳后）之前的部分，否则 调度[on] 内的 "] " 会把内容腰斩
  const _di = line.indexOf('] ');
  const rest = (_di >= 0 ? line.slice(_di + 2) : line)
    .replace(/^调度\[\S+\]\s*周期#\d+\s*/, '');
  return { summary: true, html:
    '<tr><td colspan="10" class="small text-muted py-1">' + esc(time) + ' · 周期#' + cycle + ' · ' + esc(rest) + '</td></tr>' };
}

if (window.PAGE === 'sched_history') {
  const sel = el('hist-limit');
  const sw = el('hist-show-summary');
  if (sel) sel.addEventListener('change', loadSchedHistoryDetail);
  if (sw) sw.addEventListener('change', loadSchedHistoryDetail);
}

/* ---------- 文件夹操作（事件委托） ---------- */

document.addEventListener('click', (e) => {
  const menuBtn = e.target.closest('.row-menu');
  if (menuBtn) {
    const r = menuBtn.getBoundingClientRect();
    showContextMenu(parseInt(menuBtn.dataset.idx, 10), r.left, r.bottom + 2);
    return;
  }
  const item = e.target.closest('#ctx-menu .ctx-item');
  if (item) {
    const key = item.dataset.ctx;
    const f = dashboardFolders[ctxFolderIdx];
    hideContextMenu();
    if (f) {
      if (key === 'pause') pauseFolder(f.id);
      else if (key === 'resume') resumeFolder(f.id);
      else if (key === 'disconnect') disconnectFolder(f.id, f.name);
      else if (key === 'connect') connectFolder(f.id, f.name);
      else if (key === 'remove') removeFolder(f.id, f.name);
    }
    return;
  }
  if (!e.target.closest('#ctx-menu')) hideContextMenu();
  const th = e.target.closest('th[data-sort]');
  if (th) {
    const f2 = th.dataset.sort;
    if (sortField === f2) {
      sortAsc = !sortAsc;
    } else {
      sortField = f2;
      sortAsc = true;
    }
    updateSortIndicators();
    if (window.PAGE === 'dashboard' && dashboardData) updateDashboard(dashboardData);
    return;
  }
  const link = e.target.closest('.peers-link');
  if (link) {
    e.preventDefault();
    showFolderPeers(parseInt(link.dataset.idx, 10));
    return;
  }
});

document.addEventListener('contextmenu', (e) => {
  const row = e.target.closest('#folder-body tr[data-idx]');
  if (row) {
    e.preventDefault();
    showContextMenu(parseInt(row.dataset.idx, 10), e.clientX, e.clientY);
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') hideContextMenu();
});
window.addEventListener('scroll', hideContextMenu, true);

let ctxFolderIdx = -1;

function buildCtxItems(f) {
  const items = [];
  if (f.display === 'disconnected') {
    items.push({ key: 'connect', label: '连接' });
  } else {
    items.push(f.paused
      ? { key: 'resume', label: '恢复' }
      : { key: 'pause', label: '暂停' });
    items.push({ key: 'disconnect', label: '断开连接' });
  }
  items.push({ key: 'remove', label: '删除', danger: true });
  return items;
}

function showContextMenu(idx, x, y) {
  const f = dashboardFolders[idx];
  if (!f) return;
  ctxFolderIdx = idx;
  const menu = el('ctx-menu');
  menu.innerHTML = buildCtxItems(f).map(it =>
    '<button class="ctx-item' + (it.danger ? ' danger' : '') + '" data-ctx="' + it.key + '">' + it.label + '</button>'
  ).join('');
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.classList.remove('d-none');
  const rect = menu.getBoundingClientRect();
  if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
  if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
}

function hideContextMenu() {
  const menu = el('ctx-menu');
  if (menu) menu.classList.add('d-none');
  ctxFolderIdx = -1;
}

async function connectFolder(id, name) {
  // 从其它已连接文件夹的路径推断基础保存目录（如 /sync），预填 基础路径/文件夹名
  let base = '';
  for (const f of dashboardFolders) {
    if (f.id === id || !f.path) continue;
    const m = String(f.path).replace(/\/+$/, '').match(/^(.*)\/[^/]+$/);
    if (m) { base = m[1]; break; }
  }
  const def = base ? base + '/' + name : name;
  const path = prompt('输入「' + name + '」在服务器端的保存路径：', def);
  if (!path) return;
  try {
    await fetchJSON('/api/folder/' + encodeURIComponent(id) + '/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path }),
    });
    refreshNow();
  } catch (err) { alert(err.message); }
}

/* ---------- 批量选择与操作 ---------- */

function rerenderDashboard() {
  if (dashboardData) updateDashboard(dashboardData);
  else refreshNow();
}

function updateBatchUI() {
  const bar = el('batch-bar');
  if (!bar) return;
  const n = selectedIds.size;
  const sa = el('sel-all');
  if (sa) sa.checked = n > 0 && n === dashboardFolders.filter(f => f.id).length;
  if (n > 0) {
    bar.classList.remove('d-none');
    el('batch-count').textContent = '已选 ' + n + ' 个文件夹';
  } else {
    bar.classList.add('d-none');
  }
}

async function batchAction(action) {
  const ids = Array.from(selectedIds);
  if (!ids.length) return;
  const msgs = {
    pause: '暂停选中的 ' + ids.length + ' 个文件夹？',
    resume: '恢复选中的 ' + ids.length + ' 个文件夹？',
    disconnect: '断开连接选中的 ' + ids.length + ' 个文件夹？（保留磁盘文件，仅从本设备断开，可重新连接）',
  };
  if (msgs[action] && !confirm(msgs[action])) return;
  try {
    for (const id of ids) {
      await fetchJSON('/api/folder/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
    }
    selectedIds.clear();
    updateBatchUI();
    refreshNow();
  } catch (err) { alert(err.message); }
}

document.addEventListener('change', (e) => {
  const selAll = el('sel-all');
  if (e.target === selAll) {
    if (selAll.checked) dashboardFolders.forEach(f => { if (f.id) selectedIds.add(f.id); });
    else selectedIds.clear();
    updateBatchUI();
    rerenderDashboard();
    return;
  }
  if (e.target.classList && e.target.classList.contains('row-check')) {
    const id = e.target.dataset.id;
    if (e.target.checked) selectedIds.add(id);
    else selectedIds.delete(id);
    updateBatchUI();
  }
});

async function pauseFolder(id) {
  await fetchJSON('/api/folder/' + encodeURIComponent(id) + '/pause', { method: 'POST' });
  refreshNow();
}
async function resumeFolder(id) {
  await fetchJSON('/api/folder/' + encodeURIComponent(id) + '/resume', { method: 'POST' });
  refreshNow();
}
async function disconnectFolder(id, name) {
  if (!confirm('确定从本设备断开「' + name + '」？（保留磁盘文件，可重新连接）')) return;
  try {
    await fetchJSON('/api/folder/' + encodeURIComponent(id) + '/disconnect', { method: 'POST' });
    refreshNow();
  } catch (err) { alert(err.message); }
}
async function removeFolder(id, name) {
  if (!confirm('确定从所有设备移除文件夹「' + name + '」？（磁盘文件保留）')) return;
  try {
    await fetchJSON('/api/folder/' + encodeURIComponent(id) + '/remove', { method: 'POST' });
    refreshNow();
  } catch (err) { alert(err.message); }
}

/* ---------- 设置页 ---------- */

function showMsg(id, text, ok) {
  const s = el(id);
  if (!s) return;
  s.textContent = text;
  s.className = 'ms-2 small ' + (ok ? 'text-success' : 'text-danger');
}

async function loadSettings() {
  try {
    const data = await fetchJSON('/api/settings');
    const s = data.settings || {};
    el('dlrate').value = (s.download_limit != null && s.download_limit > 0) ? s.download_limit : 0;
    el('ulrate').value = (s.upload_limit != null && s.upload_limit > 0) ? s.upload_limit : 0;
    el('settings-info').textContent =
      '设备: ' + (s.devicename || '-') + ' | 监听端口: ' + (s.listeningport || '-') +
      ' | Web UI 端口: ' + (s.webui_port || '-');
  } catch (e) {
    el('settings-info').textContent = e.message;
  }
}

/* ---------- 守护页 ---------- */

const RESTART_REASON = {
  oom: 'OOM 被杀',
  fakedeath_webui: '假死(WebUI 无响应)',
  fakedeath_threads: '假死(线程数异常)',
};

function updateGuard(data) {
  const s = data.status || {};
  el('g-nrestarts').textContent = s.nrestarts != null ? s.nrestarts : '-';
  el('g-last-backup').textContent = s.last_backup ? new Date(s.last_backup * 1000).toLocaleString() : '-';
  const fk = s.fail_streak || 0;
  const lk = s.low_thread_streak || 0;
  const healthy = fk === 0 && lk === 0;
  const h = el('g-health');
  h.textContent = healthy ? '正常' : ('异常 fail=' + fk + ' threads=' + lk);
  h.className = 'fs-6 ' + (healthy ? 'text-success' : 'text-danger');
  el('g-sched-mode').textContent = s.sched_mode != null ? s.sched_mode : '-';
  const sm = el('g-safe-mode');
  if (sm) {
    sm.textContent = s.safe_mode ? '已激活' : '未激活';
    sm.className = 'badge ' + (s.safe_mode ? 'bg-danger' : 'bg-secondary');
  }
  const st = el('guard-state');
  if (st) {
    if (s.last_restart) {
      const reason = RESTART_REASON[s.last_restart_reason] || s.last_restart_reason || '';
      st.textContent = '最近重启 ' + new Date(s.last_restart * 1000).toLocaleString() + (reason ? '（' + reason + '）' : '');
      st.className = 'me-2 text-danger';
    } else {
      st.textContent = '';
      st.className = 'me-2';
    }
  }
}

function showGuardError(msg) {
  const st = el('guard-state');
  if (st) { st.textContent = msg; st.className = 'me-2 text-danger'; }
}

const SCHED_FIELD_MAP = {
  mode: 'sc-mode', max_running: 'sc-max_running', seed_limit: 'sc-seed_limit',
  run_min_stay: 'sc-run_min_stay', preheat_sec: 'sc-preheat_sec',
  w_need: 'sc-w_need', w_scar: 'sc-w_scar', w_speed: 'sc-w_speed',
  w_download: 'sc-w_download', w_time: 'sc-w_time', w_wait: 'sc-w_wait',
  safe_oom_threshold: 'sc-safe_oom_threshold', safe_oom_window: 'sc-safe_oom_window',
  safe_exit_quiet: 'sc-safe_exit_quiet',
  disconnect_seeded_on: 'sc-disconnect_seeded_on', disconnect_seeded_min: 'sc-disconnect_seeded_min',
  reconnect_on: 'sc-reconnect_on', reconnect_interval: 'sc-reconnect_interval',
  reconnect_max_backoff: 'sc-reconnect_max_backoff', reconnect_mem_gate_pct: 'sc-reconnect_mem_gate_pct',
  reconnect_io_gate_pct: 'sc-reconnect_io_gate_pct',
};

function fillSchedForm(s) {
  for (const k in SCHED_FIELD_MAP) {
    const elm = el(SCHED_FIELD_MAP[k]);
    if (!elm) continue;
    const v = s[k];
    elm.value = (v == null || v === '') ? '' : v;
  }
}

async function loadGuard() {
  try {
    updateGuard(await fetchJSON('/api/guard/status'));
  } catch (err) { showGuardError(err.message); }
  try {
    const sc = await fetchJSON('/api/guard/sched');
    fillSchedForm(sc.sched || {});
  } catch (err) { showGuardError(err.message); }
}

if (window.PAGE === 'guard') {
  const refresh = el('guard-refresh');
  if (refresh) refresh.addEventListener('click', loadGuard);
  const save = el('sc-save');
  if (save) save.addEventListener('click', async () => {
    const patch = {};
    for (const k in SCHED_FIELD_MAP) {
      const elm = el(SCHED_FIELD_MAP[k]);
      const v = elm ? elm.value.trim() : '';
      if (v !== '') patch[k] = v;
    }
    if (!Object.keys(patch).length) return;
    try {
      await fetchJSON('/api/guard/sched', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      showMsg('sc-msg', '已保存，守护服务重启中…', true);
      setTimeout(loadGuard, 2000);
    } catch (err) { showMsg('sc-msg', err.message, false); }
  });
}

/* ---------- 添加文件夹（仪表盘） ---------- */

if (window.PAGE === 'dashboard') {
  const addBtn = el('btn-add-folder');
  if (addBtn) addBtn.addEventListener('click', () => {
    el('addf-secret').value = '';
    el('addf-path').value = '';
    el('addf-name').value = '';
    el('addf-msg').textContent = '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('addFolderModal')).show();
  });
  const batchPause = el('batch-pause');
  const batchResume = el('batch-resume');
  const batchDisc = el('batch-disconnect');
  const batchClear = el('batch-clear');
  if (batchPause) batchPause.addEventListener('click', () => batchAction('pause'));
  if (batchResume) batchResume.addEventListener('click', () => batchAction('resume'));
  if (batchDisc) batchDisc.addEventListener('click', () => batchAction('disconnect'));
  if (batchClear) batchClear.addEventListener('click', () => {
    selectedIds.clear();
    updateBatchUI();
    rerenderDashboard();
  });
  const submit = el('addf-submit');
  if (submit) submit.addEventListener('click', async () => {
    const secret = el('addf-secret').value.trim();
    const path = el('addf-path').value.trim();
    const name = el('addf-name').value.trim();
    const msg = el('addf-msg');
    if (!secret || !path) {
      msg.textContent = '请填写密钥与保存路径';
      msg.className = 'small text-danger';
      return;
    }
    try {
      await fetchJSON('/api/folder/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret: secret, path: path, name: name }),
      });
      bootstrap.Modal.getOrCreateInstance(document.getElementById('addFolderModal')).hide();
      refreshNow();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = 'small text-danger';
    }
  });
}

if (window.PAGE === 'settings') {
  const gUrl = el('guard-url');
  const gTok = el('guard-token');
  if (gUrl) {
    fetchJSON('/api/guard/config').then(d => {
      gUrl.value = d.url || '';
      if (gTok) gTok.value = d.token || '';
    }).catch(() => {});
  }
  const gSave = el('guard-save');
  if (gSave) gSave.addEventListener('click', async () => {
    try {
      await fetchJSON('/api/guard/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: gUrl ? gUrl.value.trim() : '', token: gTok ? gTok.value.trim() : '' }),
      });
    } catch (err) {
      showMsg('guard-msg', '保存失败: ' + err.message, false);
      return;
    }
    // 保存后测试连接，立即反馈地址/令牌是否正确
    try {
      await fetchJSON('/api/guard/status');
      showMsg('guard-msg', '已保存，连接成功', true);
    } catch (err) {
      showMsg('guard-msg', '已保存，但连接失败: ' + err.message, false);
    }
  });

  const intervalSel = el('poll-interval');
  if (intervalSel) {
    intervalSel.value = String(getPollInterval());
    intervalSel.addEventListener('change', () => {
      localStorage.setItem('rsync_poll_interval', intervalSel.value);
      showMsg('interval-msg', '已保存（' + (parseInt(intervalSel.value, 10) / 1000) + ' 秒）', true);
    });
  }

  const saveBtn = el('save-limits');
  if (saveBtn) saveBtn.addEventListener('click', async () => {
    const dl = parseInt(el('dlrate').value, 10) || 0;
    const ul = parseInt(el('ulrate').value, 10) || 0;
    try {
      await fetchJSON('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ download_limit: dl, upload_limit: ul }),
      });
      showMsg('settings-msg', '已保存', true);
      loadSettings();
    } catch (e) { showMsg('settings-msg', e.message, false); }
  });

  const pauseAll = el('btn-pause-all');
  const resumeAll = el('btn-resume-all');
  if (pauseAll) pauseAll.addEventListener('click', async () => {
    if (!confirm('暂停所有文件夹？')) return;
    try {
      await fetchJSON('/api/pause-all', { method: 'POST' });
      showMsg('batch-msg', '已暂停所有', true);
    } catch (e) { showMsg('batch-msg', e.message, false); }
  });
  if (resumeAll) resumeAll.addEventListener('click', async () => {
    try {
      await fetchJSON('/api/resume-all', { method: 'POST' });
      showMsg('batch-msg', '已恢复所有', true);
    } catch (e) { showMsg('batch-msg', e.message, false); }
  });

  const importForm = el('import-form');
  if (importForm) importForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const data = await fetchJSON('/api/import', { method: 'POST', body: new FormData(importForm) });
      const lines = (data.results || []).map(r =>
        (r.name ? esc(r.name) + ': ' : '') + (r.ok ? '成功' : (r.message || '失败')));
      el('import-msg').innerHTML = lines.join('<br>') || '无结果';
      el('import-msg').className = 'ms-2 small';
    } catch (err) { showMsg('import-msg', err.message, false); }
  });
}
