/* ──────────────────────────────────────────────────────────────────────────
   app.js — S.A.M.S. Teacher PWA
   ─────────────────────────────────────────────────────────────────────────
   Phone companion to the central monitoring dashboard (Module 4). Teachers
   receive bullying-incident alerts in real time and respond on the go.

   Served same-origin from the FastAPI backend at /m/, so it talks to the
   SAME endpoints the desktop dashboard uses:
     GET  /api/alerts/?status=&severity=&per_page=   → feed
     GET  /api/alerts/stats                          → counters
     GET  /api/alerts/{id}                           → full detail
     PUT  /api/alerts/{id}/resolve                   → resolve w/ notes
     WS   /ws/dashboard                              → live alert push

   NOTE: the login screen is a UI SHELL only. It stores a display name + role
   in localStorage so the app has an identity to show; it does NOT yet
   authenticate against the backend. Real JWT login + RBAC (FR23) is a separate
   planned iteration. No password is ever stored.
   ────────────────────────────────────────────────────────────────────────── */

'use strict';

// ── Config (same origin as the backend that serves this PWA) ────────────────
const API    = '';  // relative → same host:port as the page
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/dashboard`;
const SESSION_KEY = 'sams.teacher.session';

// ── State ───────────────────────────────────────────────────────────────────
let alerts       = [];
let selectedId   = null;
let filterStatus = 'active';   // active | all | resolved
let filterSev    = null;       // null | high | medium | low
let ws           = null;
let currentAudio = null;
let session      = null;

// ── DOM helpers ─────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Session / login shell ───────────────────────────────────────────────────
function loadSession() {
  try { session = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); }
  catch { session = null; }
}

function login(ev) {
  ev.preventDefault();
  const name = $('login-name').value.trim() || 'Teacher';
  const role = $('login-role').value;
  session = { name, role, since: new Date().toISOString() };
  localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  startApp();
}

function logout() {
  localStorage.removeItem(SESSION_KEY);
  session = null;
  if (ws) { try { ws.onclose = null; ws.close(); } catch {} ws = null; }
  $('app').hidden = true;
  $('login-view').hidden = false;
}

// ── Boot ────────────────────────────────────────────────────────────────────
function init() {
  loadSession();
  $('login-form').addEventListener('submit', login);
  $('btn-logout').addEventListener('click', logout);
  $('btn-refresh').addEventListener('click', () => { loadAlerts(); loadStats(); });
  $('detail-back').addEventListener('click', closeDetail);
  document.querySelectorAll('.chip').forEach((c) =>
    c.addEventListener('click', () => setFilter(c.dataset.filter, c)));

  if (session) startApp();
  else { $('login-view').hidden = false; $('app').hidden = true; }
}

function startApp() {
  $('login-view').hidden = true;
  $('app').hidden = false;
  $('who').textContent = `${session.name} · ${session.role}`;
  connectWS();
  loadAlerts();
  loadStats();
  // Gentle background refresh as a safety net behind the live socket.
  clearInterval(window._alertPoll); clearInterval(window._statPoll);
  window._alertPoll = setInterval(loadAlerts, 30000);
  window._statPoll  = setInterval(loadStats, 15000);
}

// ── WebSocket live feed ─────────────────────────────────────────────────────
function connectWS() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    setWs('connected', 'Live');
    setInterval(() => ws.readyState === 1 && ws.send('ping'), 25000);
  };
  ws.onmessage = (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === 'ALERT') handleIncomingAlert(msg);
  };
  ws.onclose = () => { setWs('error', 'Reconnecting…'); setTimeout(connectWS, 3000); };
  ws.onerror = () => setWs('error', 'Offline');
}

function setWs(cls, text) {
  $('ws-dot').className = `ws-dot ${cls}`;
  $('ws-status').textContent = text;
}

function handleIncomingAlert(msg) {
  if (alerts.some((a) => a.alert_id === msg.alert_id)) return;
  alerts.unshift({
    alert_id: msg.alert_id, event_id: msg.event_id, severity: msg.severity,
    status: 'active', location_name: msg.location_name, transcript: msg.transcript,
    threat_score: msg.threat_score, classification: msg.classification,
    audio_url: msg.audio_url, created_at: msg.timestamp,
  });
  renderList();
  loadStats();
  notify(msg);
}

// ── Notifications: toast + sound + vibration ────────────────────────────────
function notify(msg) {
  const t = document.createElement('div');
  t.className = `toast ${msg.severity}`;
  t.innerHTML = `
    <div class="toast-head">
      <span class="sev-badge ${msg.severity}">${escHtml(msg.severity)}</span>
      <span class="toast-title">${escHtml(msg.location_name || 'Incident')}</span>
    </div>
    <div class="toast-body">${escHtml((msg.transcript || '').slice(0, 90))}</div>`;
  t.addEventListener('click', () => { t.remove(); selectAlert(msg.alert_id); });
  $('toasts').appendChild(t);
  setTimeout(() => t.remove(), 7000);

  // Haptics — strongest for high severity.
  if (navigator.vibrate) {
    navigator.vibrate(msg.severity === 'high' ? [200, 80, 200] : msg.severity === 'medium' ? [150] : [80]);
  }
  beep(msg.severity);
}

function beep(severity) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = severity === 'high' ? 880 : severity === 'medium' ? 660 : 440;
    gain.gain.setValueAtTime(0.12, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.start(); osc.stop(ctx.currentTime + 0.4);
  } catch {}
}

// ── Data loading ────────────────────────────────────────────────────────────
async function loadAlerts() {
  try {
    const sev = filterSev ? `&severity=${encodeURIComponent(filterSev)}` : '';
    const res = await fetch(`${API}/api/alerts/?status=${encodeURIComponent(filterStatus)}${sev}&per_page=50`);
    const data = await res.json();
    alerts = data.alerts || [];
    renderList();
  } catch {
    $('list').innerHTML = '<div class="empty">Can’t reach the server. Check your connection.</div>';
  }
}

async function loadStats() {
  try {
    const res = await fetch(`${API}/api/alerts/stats`);
    const d = await res.json();
    $('stat-active').textContent = d.active_alerts ?? '—';
    $('stat-high').textContent   = d.high ?? '—';
    $('stat-total').textContent  = (d.active_alerts || 0) + (d.resolved_alerts || 0);
  } catch {}
}

// ── List rendering ──────────────────────────────────────────────────────────
function renderList() {
  const list = $('list');
  let data = [...alerts];
  if (filterStatus === 'active')   data = data.filter((a) => a.status === 'active');
  if (filterStatus === 'resolved') data = data.filter((a) => a.status === 'resolved');
  if (filterSev)                   data = data.filter((a) => a.severity === filterSev);

  if (!data.length) { list.innerHTML = '<div class="empty">No alerts match this filter.</div>'; return; }

  list.innerHTML = data.map((a) => `
    <div class="card ${escHtml(a.severity)}" data-id="${escHtml(a.alert_id)}">
      <div class="card-top">
        <span class="sev-badge ${escHtml(a.severity)}">${escHtml(a.severity)}</span>
        <span class="card-loc">${escHtml(a.location_name || 'Unknown location')}</span>
        <span class="card-time">${fmtTime(a.created_at)}</span>
      </div>
      <div class="card-transcript">${escHtml(a.transcript || 'No transcript')}</div>
      <div class="card-status ${escHtml(a.status)}">${escHtml(a.status)}</div>
    </div>`).join('');

  list.querySelectorAll('.card').forEach((el) =>
    el.addEventListener('click', () => selectAlert(el.dataset.id)));
}

// ── Detail overlay ──────────────────────────────────────────────────────────
async function selectAlert(alertId) {
  selectedId = alertId;
  const cached = alerts.find((a) => a.alert_id === alertId);
  if (cached) renderDetail(cached);
  openDetail();
  try {
    const res = await fetch(`${API}/api/alerts/${encodeURIComponent(alertId)}`);
    if (res.ok) renderDetail(await res.json());
  } catch {}
}

function openDetail() { $('detail').classList.add('open'); }
function closeDetail() {
  $('detail').classList.remove('open');
  if (currentAudio && !currentAudio.paused) currentAudio.pause();
}

function renderDetail(a) {
  const sev   = a.severity || 'low';
  const score = a.threat_score ?? 0;
  const scoreW = Math.round(score * 100);
  const isRes = a.status === 'resolved';
  const fmt = (v, unit, dp = 1) => (v === null || v === undefined) ? '—' : Number(v).toFixed(dp) + unit;
  const edgeConf = (a.edge_confidence === null || a.edge_confidence === undefined)
    ? '—' : Math.round(a.edge_confidence * 100) + '%';

  $('detail-title').textContent = a.location_name || 'Incident';
  $('detail-body').innerHTML = `
    <div class="meta-row">
      <span class="sev-badge ${escHtml(sev)}">${escHtml(sev)}</span>
      <span class="pill">${escHtml(a.classification || '—')}</span>
      <span class="pill">${fmtDateTime(a.created_at)}</span>
      ${isRes ? '<span class="pill teal">✓ resolved</span>' : ''}
    </div>

    <div class="label">Threat score</div>
    <div class="score-track"><div class="score-fill ${escHtml(sev)}" style="width:${scoreW}%"></div></div>

    <div class="grid">
      <div class="field-box"><div class="k">Score</div><div class="v">${(score * 100).toFixed(1)}%</div></div>
      <div class="field-box"><div class="k">Edge scream conf</div><div class="v">${edgeConf}</div></div>
      <div class="field-box"><div class="k">Intensity</div><div class="v">${fmt(a.intensity, ' dB', 1)}</div></div>
      <div class="field-box"><div class="k">Pitch</div><div class="v">${fmt(a.pitch, ' Hz', 0)}</div></div>
    </div>

    <div class="label">Transcript</div>
    <div class="box"><div class="transcript-text">${highlight(a.transcript || 'No speech detected in this clip.')}</div></div>

    ${a.audio_url ? `
    <div class="label">Audio</div>
    <div class="box audio-player">
      <button class="play-btn" id="play-btn" aria-label="Play audio">
        <svg width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M11.6 8.7L4.6 12.5A.5.5 0 0 1 4 12V4a.5.5 0 0 1 .6-.5l7 3.8a.5.5 0 0 1 0 .9z"/></svg>
      </button>
      <span class="audio-time" id="audio-time">0:00</span>
    </div>` : ''}

    <div class="box resolve ${isRes ? 'resolved' : ''}">
      ${isRes ? `
        <div class="resolved-msg">
          <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>
          Resolved${a.resolution_notes ? ' — ' + escHtml(a.resolution_notes) : ''}
        </div>` : `
        <div class="label">Respond &amp; resolve</div>
        <textarea id="resolve-notes" placeholder="Describe what you found and the action taken…"></textarea>
        <button class="btn-primary" id="resolve-btn">Mark resolved</button>`}
    </div>`;

  if (a.audio_url) {
    $('play-btn').addEventListener('click', (e) => toggleAudio(`${API}${a.audio_url}`, e.currentTarget));
  }
  if (!isRes) {
    $('resolve-btn').addEventListener('click', () => resolveAlert(a.alert_id));
  }
}

// ── Audio playback ──────────────────────────────────────────────────────────
const ICON_PLAY  = '<svg width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M11.6 8.7L4.6 12.5A.5.5 0 0 1 4 12V4a.5.5 0 0 1 .6-.5l7 3.8a.5.5 0 0 1 0 .9z"/></svg>';
const ICON_PAUSE = '<svg width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M5.5 3.5A1.5 1.5 0 0 1 7 5v6a1.5 1.5 0 0 1-3 0V5a1.5 1.5 0 0 1 1.5-1.5m5 0A1.5 1.5 0 0 1 12 5v6a1.5 1.5 0 0 1-3 0V5a1.5 1.5 0 0 1 1.5-1.5"/></svg>';

async function toggleAudio(url, btn) {
  if (currentAudio && !currentAudio.paused && currentAudio.src.endsWith(url.replace(API, ''))) {
    currentAudio.pause(); btn.innerHTML = ICON_PLAY; return;
  }
  if (!currentAudio || !currentAudio.src.endsWith(url.replace(API, ''))) {
    currentAudio = new Audio(url);
    currentAudio.ontimeupdate = () => {
      const t = currentAudio.currentTime;
      const el = $('audio-time');
      if (el) el.textContent = `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
    };
    currentAudio.onended = () => { btn.innerHTML = ICON_PLAY; const el = $('audio-time'); if (el) el.textContent = '0:00'; };
  }
  try { await currentAudio.play(); btn.innerHTML = ICON_PAUSE; } catch {}
}

// ── Resolve ─────────────────────────────────────────────────────────────────
async function resolveAlert(alertId) {
  const ta = $('resolve-notes');
  const notes = (ta?.value || '').trim();
  if (!notes) { ta?.focus(); return; }
  const btn = $('resolve-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const res = await fetch(`${API}/api/alerts/${encodeURIComponent(alertId)}/resolve`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resolution_notes: notes }),
    });
    if (res.ok) {
      const a = alerts.find((x) => x.alert_id === alertId);
      if (a) { a.status = 'resolved'; a.resolution_notes = notes; }
      await loadStats();
      renderList();
      selectAlert(alertId);
    } else {
      btn.disabled = false; btn.textContent = 'Mark resolved';
    }
  } catch {
    btn.disabled = false; btn.textContent = 'Mark resolved';
  }
}

// ── Filters ─────────────────────────────────────────────────────────────────
function setFilter(val, el) {
  document.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
  el.classList.add('active');
  if (['active', 'resolved', 'all'].includes(val)) { filterStatus = val; filterSev = null; }
  else { filterSev = val; filterStatus = 'all'; }
  loadAlerts();
}

// ── Formatting ──────────────────────────────────────────────────────────────
const KEYWORDS = ['bodoh', 'babi', 'sial', 'mati', 'pergi mampus', 'bangang', 'stupid', 'loser', 'shut up', 'kill', 'hurt', 'go die'];
function highlight(text) {
  let t = escHtml(text);
  KEYWORDS.forEach((k) => { t = t.replace(new RegExp(`(${k})`, 'gi'), '<span class="keyword-hit">$1</span>'); });
  return t;
}
function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}
function fmtDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// ── Service worker registration (enables install + offline shell) ───────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('service-worker.js').catch(() => {});
  });
}

document.addEventListener('DOMContentLoaded', init);
