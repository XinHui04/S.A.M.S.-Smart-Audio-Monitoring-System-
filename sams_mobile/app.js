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
     PUT  /api/alerts/{id}/acknowledge               → acknowledge (FR17)
     PUT  /api/alerts/{id}/resolve                   → resolve w/ notes
     WS   /ws/dashboard                              → live alert push

   AUTH (FR23): real JWT login against POST /api/auth/login. The returned
   access token + user profile are kept in localStorage; every /api call sends
   `Authorization: Bearer <token>` (via authFetch), the WebSocket appends
   `?token=`, and audio URLs get a `?token=` query fallback. A 401 anywhere
   (or WS close 4401) signs the user out. No password is ever stored.
   ────────────────────────────────────────────────────────────────────────── */

'use strict';

// ── Config (same origin as the backend that serves this PWA) ────────────────
const API    = '';  // relative → same host:port as the page
const WS_BASE = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/dashboard`;
const SESSION_KEY = 'sams.teacher.session';

// ── State ───────────────────────────────────────────────────────────────────
let alerts       = [];
let selectedId   = null;
let filterStatus = 'open';     // open (active+acknowledged) | active | acknowledged | resolved | all
let filterSev    = null;       // null | high | medium | low
let ws           = null;
let currentAudio = null;
let session      = null;
let pushState    = 'off';      // 'off' | 'on' | 'unavailable' — mirrors the bell button
let checkin          = null;   // {location_id, location_name, checked_in_at, expired} | null (FR30)
let checkinLocations = [];     // [{location_id, location_name}, ...]

// ── DOM helpers ─────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Session / JWT auth ──────────────────────────────────────────────────────
function loadSession() {
  try { session = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null'); }
  catch { session = null; }
  // Reject the pre-auth {name, role} shape (or anything else without a JWT).
  if (!session?.token || !session?.user) session = null;
}

function showLoginError(text) {
  const el = $('login-error');
  el.textContent = text;   // textContent — never inject server text as HTML
  el.hidden = !text;
}

async function login(ev) {
  ev.preventDefault();
  const email    = $('login-email').value.trim();
  const password = $('login-password').value;
  const btn = $('login-submit');
  showLoginError('');
  btn.disabled = true; btn.textContent = 'Signing in…';
  try {
    const res = await fetch(`${API}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    if (!res.ok) {
      let detail = 'Invalid email or password';
      try { detail = (await res.json()).detail || detail; } catch {}
      showLoginError(res.status === 401 ? detail : 'Sign-in failed. Please try again.');
      return;
    }
    const data = await res.json();
    // Store the token + user profile only — the password is never persisted.
    session = { token: data.access_token, user: data.user, since: new Date().toISOString() };
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
    $('login-password').value = '';
    startApp();
  } catch {
    showLoginError('Can’t reach the server. Check your connection.');
  } finally {
    btn.disabled = false; btn.textContent = 'Sign in';
  }
}

function logout() {
  unsubscribePushBestEffort();            // best-effort — never block sign-out on it
  localStorage.removeItem(SESSION_KEY);   // drops the JWT along with the profile
  session = null;
  if (ws) { try { ws.onclose = null; ws.close(); } catch {} ws = null; }
  if (currentAudio) { try { currentAudio.pause(); } catch {} currentAudio = null; }
  // The check-in itself persists server-side (TTL handles staleness) — only
  // the local card resets, so the next login re-fetches the true state
  // instead of showing whatever was last rendered.
  checkin = null; checkinLocations = [];
  const card = $('checkin-card'); if (card) card.innerHTML = '';
  $('app').hidden = true;
  $('login-view').hidden = false;
}

// ── Authenticated fetch ─────────────────────────────────────────────────────
// Adds the Bearer token to every API call; a 401 means the token is expired
// or revoked, so we sign out back to the login view.
async function authFetch(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (session?.token) headers['Authorization'] = `Bearer ${session.token}`;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401) {
    logout();
    throw new Error('unauthorized');
  }
  return res;
}

// Appends the JWT as a query param — for URLs the browser fetches itself
// (Audio element, WebSocket) where we can’t set an Authorization header.
function withToken(url) {
  if (!session?.token) return url;
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(session.token)}`;
}

// ── Boot ────────────────────────────────────────────────────────────────────
function init() {
  loadSession();
  $('login-form').addEventListener('submit', login);
  $('btn-logout').addEventListener('click', logout);
  $('btn-refresh').addEventListener('click', () => { loadAlerts(); loadStats(); });
  $('btn-push').addEventListener('click', togglePush);
  $('detail-back').addEventListener('click', closeDetail);
  document.querySelectorAll('.chip').forEach((c) =>
    c.addEventListener('click', () => setFilter(c.dataset.filter, c)));

  if (session) startApp();
  else { $('login-view').hidden = false; $('app').hidden = true; }
}

function startApp() {
  $('login-view').hidden = true;
  $('app').hidden = false;
  $('who').textContent = `${session.user.name} · ${session.user.role}`;
  connectWS();
  loadAlerts().then(consumeAlertHash);
  loadStats();
  loadCheckin();  // FR30 — zone check-in card
  // Gentle background refresh as a safety net behind the live socket.
  clearInterval(window._alertPoll); clearInterval(window._statPoll);
  window._alertPoll = setInterval(loadAlerts, 30000);
  window._statPoll  = setInterval(loadStats, 15000);
  initPush();   // silent reconciliation — never prompts (FR9/FR12)
}

// ── WebSocket live feed ─────────────────────────────────────────────────────
function connectWS() {
  if (!session?.token) return;
  ws = new WebSocket(withToken(WS_BASE));
  ws.onopen = () => {
    setWs('connected', 'Live');
    setInterval(() => ws.readyState === 1 && ws.send('ping'), 25000);
  };
  ws.onmessage = (e) => {
    let msg; try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === 'ALERT') handleIncomingAlert(msg);
  };
  ws.onclose = (e) => {
    // 4401 = server rejected the token — sign out instead of reconnecting
    // in a loop with dead credentials.
    if (e.code === 4401) { logout(); return; }
    setWs('error', 'Reconnecting…'); setTimeout(connectWS, 3000);
  };
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
    nearest_staff: msg.nearest_staff || null,   // FR30 — who's closest to respond
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
    const res = await authFetch(`${API}/api/alerts/?status=${encodeURIComponent(filterStatus)}${sev}&per_page=50`);
    const data = await res.json();
    alerts = data.alerts || [];
    renderList();
  } catch {
    $('list').innerHTML = '<div class="empty">Can’t reach the server. Check your connection.</div>';
  }
}

async function loadStats() {
  try {
    const res = await authFetch(`${API}/api/alerts/stats`);
    const d = await res.json();
    $('stat-active').textContent = d.active_alerts ?? '—';
    $('stat-high').textContent   = d.high ?? '—';
    $('stat-total').textContent  = (d.active_alerts || 0) + (d.acknowledged_alerts || 0) + (d.resolved_alerts || 0);
  } catch {}
}

// ── List rendering ──────────────────────────────────────────────────────────
function renderList() {
  const list = $('list');
  let data = [...alerts];
  if (filterStatus === 'open')         data = data.filter((a) => a.status === 'active' || a.status === 'acknowledged');
  if (filterStatus === 'active')       data = data.filter((a) => a.status === 'active');
  if (filterStatus === 'acknowledged') data = data.filter((a) => a.status === 'acknowledged');
  if (filterStatus === 'resolved')     data = data.filter((a) => a.status === 'resolved');
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
    const res = await authFetch(`${API}/api/alerts/${encodeURIComponent(alertId)}`);
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
  const isAck = a.status === 'acknowledged';
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
      ${isAck ? '<span class="pill amber">acknowledged</span>' : ''}
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

    ${Array.isArray(a.nearest_staff) && a.nearest_staff.length ? `
    <div class="label">Nearest staff</div>
    <div class="meta-row">
      ${a.nearest_staff.map((s) => `<span class="pill">${escHtml(s.name || 'Staff')} · ${
        s.assigned_here ? 'assigned here' : escHtml(fmt(s.distance, ' away', 1))
      }${s.checked_in ? ' · checked in' : ''}</span>`).join('')}
    </div>` : ''}

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
        ${isAck ? `
        <div class="ack-note">
          <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>
          Acknowledged — this incident is being handled.
        </div>` : `
        <button class="btn-ack" id="ack-btn">Acknowledge — I’m on it</button>`}
        <div class="label">Respond &amp; resolve</div>
        <textarea id="resolve-notes" placeholder="Describe what you found and the action taken…"></textarea>
        <button class="btn-primary" id="resolve-btn">Mark resolved</button>`}
    </div>`;

  if (a.audio_url) {
    // Audio is fetched by the <audio> element itself (no headers), so the JWT
    // rides along as the backend’s ?token= query fallback.
    $('play-btn').addEventListener('click', (e) => toggleAudio(withToken(`${API}${a.audio_url}`), e.currentTarget));
  }
  if (!isRes) {
    $('resolve-btn').addEventListener('click', () => resolveAlert(a.alert_id));
    if (a.status === 'active') {
      $('ack-btn').addEventListener('click', () => acknowledgeAlert(a.alert_id));
    }
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

// ── Acknowledge (FR17) ──────────────────────────────────────────────────────
// Marks an active alert as "acknowledged" (a teacher is on the way). The alert
// stays open — Resolve remains available until it is closed with notes.
async function acknowledgeAlert(alertId) {
  const btn = $('ack-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Acknowledging…'; }
  try {
    const res = await authFetch(`${API}/api/alerts/${encodeURIComponent(alertId)}/acknowledge`, {
      method: 'PUT',
    });
    if (res.ok) {
      const a = alerts.find((x) => x.alert_id === alertId);
      if (a) a.status = 'acknowledged';
      await loadStats();
      renderList();
      selectAlert(alertId);
    } else if (res.status === 409) {
      // Someone else already acknowledged/resolved it — refetch the truth.
      selectAlert(alertId);
      loadAlerts();
    } else if (btn) {
      btn.disabled = false; btn.textContent = 'Acknowledge — I’m on it';
    }
  } catch {
    if (btn) { btn.disabled = false; btn.textContent = 'Acknowledge — I’m on it'; }
  }
}

// ── Resolve ─────────────────────────────────────────────────────────────────
async function resolveAlert(alertId) {
  const ta = $('resolve-notes');
  const notes = (ta?.value || '').trim();
  if (!notes) { ta?.focus(); return; }
  const btn = $('resolve-btn');
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    const res = await authFetch(`${API}/api/alerts/${encodeURIComponent(alertId)}/resolve`, {
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

// ── Staff zone check-in (FR30) ──────────────────────────────────────────────
// Lets a teacher mark which zone they're physically in, so alert detail's
// "Nearest staff" list can show a live "checked in" hint alongside "assigned
// here"/distance. Server contract:
//   GET    /api/staff/checkin → {checkin, locations, ttl_seconds}
//   PUT    /api/staff/checkin {location_id} → checkin object
//   DELETE /api/staff/checkin → {status:"checked_out"} (404 if none)
// The check-in persists across logout intentionally (server TTL handles
// staleness) — logout() only clears the local card so the next login
// re-fetches the true state instead of showing stale data.
async function loadCheckin() {
  try {
    const res = await authFetch(`${API}/api/staff/checkin`);
    if (!res.ok) { console.warn('checkin: load failed', res.status); return; }
    const data = await res.json();
    checkin = data.checkin || null;
    checkinLocations = data.locations || [];
    renderCheckinCard();
  } catch (err) {
    console.warn('checkin: load failed', err);
  }
}

async function doCheckIn() {
  const sel = $('checkin-select');
  const locationId = sel?.value;
  if (!locationId) return;
  const btn = $('checkin-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Checking in…'; }
  try {
    const res = await authFetch(`${API}/api/staff/checkin`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ location_id: locationId }),
    });
    if (res.ok) {
      checkin = await res.json();
      renderCheckinCard();
    } else {
      console.warn('checkin: check-in failed', res.status);
      if (btn) { btn.disabled = false; btn.textContent = 'Check in'; }
    }
  } catch (err) {
    console.warn('checkin: check-in failed', err);
    if (btn) { btn.disabled = false; btn.textContent = 'Check in'; }
  }
}

async function doCheckOut() {
  const btn = $('checkout-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Checking out…'; }
  try {
    const res = await authFetch(`${API}/api/staff/checkin`, { method: 'DELETE' });
    if (res.ok || res.status === 404) {
      checkin = null;
      renderCheckinCard();
    } else {
      console.warn('checkin: check-out failed', res.status);
      if (btn) { btn.disabled = false; btn.textContent = 'Check out'; }
    }
  } catch (err) {
    console.warn('checkin: check-out failed', err);
    if (btn) { btn.disabled = false; btn.textContent = 'Check out'; }
  }
}

// Coarse relative time for the "since" label — no need for second-level
// precision here, the TTL/expiry flag already conveys freshness.
function fmtRelative(iso) {
  if (!iso) return '—';
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1)  return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function checkinOptionsHtml(selectedId) {
  return checkinLocations.map((l) => `<option value="${escHtml(l.location_id)}"${
    l.location_id === selectedId ? ' selected' : ''
  }>${escHtml(l.location_name)}</option>`).join('');
}

function renderCheckinCard() {
  const el = $('checkin-card');
  if (!el) return;

  if (checkin && !checkin.expired) {
    el.className = 'checkin-card checked-in';
    el.innerHTML = `
      <span class="pill teal">You're at: ${escHtml(checkin.location_name)} · since ${escHtml(fmtRelative(checkin.checked_in_at))}</span>
      <button class="btn-checkout" id="checkout-btn">Check out</button>`;
    $('checkout-btn').addEventListener('click', doCheckOut);
    return;
  }

  if (checkin && checkin.expired) {
    el.className = 'checkin-card expired';
    el.innerHTML = `
      <span class="pill">${escHtml(checkin.location_name)} · since ${escHtml(fmtRelative(checkin.checked_in_at))} (expired)</span>
      <div class="checkin-row">
        <select id="checkin-select">${checkinOptionsHtml(checkin.location_id)}</select>
        <button class="btn-checkin" id="checkin-btn">Check in</button>
      </div>`;
    $('checkin-btn').addEventListener('click', doCheckIn);
    return;
  }

  el.className = 'checkin-card';
  if (!checkinLocations.length) { el.innerHTML = ''; return; }   // nothing to offer — stays collapsed
  el.innerHTML = `
    <div class="checkin-row">
      <span class="checkin-label">Check in to a zone</span>
      <select id="checkin-select">${checkinOptionsHtml(null)}</select>
      <button class="btn-checkin" id="checkin-btn">Check in</button>
    </div>`;
  $('checkin-btn').addEventListener('click', doCheckIn);
}

// ── Filters ─────────────────────────────────────────────────────────────────
function setFilter(val, el) {
  document.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
  el.classList.add('active');
  if (['open', 'active', 'acknowledged', 'resolved', 'all'].includes(val)) { filterStatus = val; filterSev = null; }
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

// ── Web Push (FR9/FR12) — alerts while the app is closed ────────────────────
// Server contract:
//   GET    /api/push/public-key → {"public_key": <base64url VAPID key|"">, "enabled": bool}
//   POST   /api/push/subscribe  {endpoint, keys:{p256dh, auth}} → upserts (idempotent)
//   DELETE /api/push/subscribe  {endpoint}
// We only ever store the browser's PushSubscription (endpoint + keys) — never
// more. Every step degrades silently (console.warn) so a push failure can
// never break login, the alert feed, or the WebSocket.
let pushPublicKey = null;   // cached VAPID key, fetched once
let pushEnabled   = false;  // backend flag from /api/push/public-key

// Converts a base64url VAPID key (as the backend/W3C Push API expect) into the
// Uint8Array applicationServerKey subscribe() needs. Standard boilerplate.
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return arr;
}

function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && window.isSecureContext;
}

function setBell(state) {
  pushState = state;
  const btn = $('btn-push');
  if (!btn) return;
  btn.classList.remove('on', 'unavailable');
  if (state === 'on') { btn.classList.add('on'); btn.title = 'Push notifications on — tap to turn off'; }
  else if (state === 'unavailable') {
    btn.classList.add('unavailable');
    btn.title = 'Push unavailable on this origin (needs HTTPS / localhost — see README)';
  } else {
    btn.title = 'Push notifications off — tap to turn on';
  }
}

// Fetches the VAPID key + enabled flag once per session. Not behind authFetch
// since it's harmless config, but the endpoint lives under /api regardless.
async function loadPushConfig() {
  if (pushPublicKey !== null) return true;   // already fetched this session
  try {
    const res = await authFetch(`${API}/api/push/public-key`);
    if (!res.ok) return false;
    const data = await res.json();
    pushPublicKey = data.public_key || '';
    pushEnabled = !!data.enabled;
    return !!(pushEnabled && pushPublicKey);
  } catch (err) {
    console.warn('push: could not load config', err);
    return false;
  }
}

async function sendSubscription(sub) {
  const json = sub.toJSON();
  await authFetch(`${API}/api/push/subscribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
  });
}

// Runs once after login/startApp. Never prompts for permission on its own —
// only reconciles an *existing* grant/subscription so the bell reflects
// reality and the server's endpoint mapping stays current (idempotent POST).
async function initPush() {
  if (!pushSupported()) { setBell('unavailable'); return; }
  const ok = await loadPushConfig();
  if (!ok) { setBell('unavailable'); return; }

  if (Notification.permission !== 'granted') { setBell('off'); return; }
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await sendSubscription(sub);   // upsert — keeps server mapping fresh
      setBell('on');
    } else {
      setBell('off');
    }
  } catch (err) {
    console.warn('push: reconcile failed', err);
    setBell('off');
  }
}

// Bell click handler — the ONLY place we ask for Notification permission,
// always in direct response to this user gesture (browsers require it).
async function togglePush() {
  if (pushState === 'unavailable') return;

  if (pushState === 'on') {
    await unsubscribePushBestEffort();
    setBell('off');
    return;
  }

  const ok = await loadPushConfig();
  if (!ok) { setBell('unavailable'); return; }

  try {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') { setBell('off'); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(pushPublicKey),
    });
    await sendSubscription(sub);
    setBell('on');
  } catch (err) {
    console.warn('push: subscribe failed', err);
    setBell('off');
  }
}

// Best-effort unsubscribe — used both by the bell toggle and on logout.
// Never throws: a failed unsubscribe must not block sign-out or the UI.
async function unsubscribePushBestEffort() {
  if (!session?.token) return;   // nothing we can authenticate the DELETE with
  try {
    if (!('serviceWorker' in navigator)) return;
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    const endpoint = sub.endpoint;
    await sub.unsubscribe();
    await authFetch(`${API}/api/push/subscribe`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint }),
    });
  } catch (err) {
    console.warn('push: unsubscribe failed', err);
  }
}

// ── Opening an alert from a push notification tap ───────────────────────────
// Fired either by service-worker "message" (app already open) or by the
// '#alert=' URL hash the worker's notificationclick uses to open a new tab.
async function handleOpenAlert(alertId) {
  if (!alertId) return;
  if (!alerts.length) { try { await loadAlerts(); } catch {} }
  selectAlert(alertId);
}

function consumeAlertHash() {
  const m = /#alert=([^&]+)/.exec(location.hash);
  if (!m) return;
  const alertId = decodeURIComponent(m[1]);
  history.replaceState(null, '', location.pathname + location.search);   // clear the hash
  handleOpenAlert(alertId);
}

// ── Service worker registration (enables install + offline shell) ───────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('service-worker.js').catch(() => {});
  });
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data?.type === 'OPEN_ALERT') handleOpenAlert(event.data.alert_id);
  });
}

document.addEventListener('DOMContentLoaded', init);
