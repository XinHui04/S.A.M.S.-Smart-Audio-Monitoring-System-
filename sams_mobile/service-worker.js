/* ──────────────────────────────────────────────────────────────────────────
   service-worker.js — S.A.M.S. Teacher PWA
   ─────────────────────────────────────────────────────────────────────────
   Makes the teacher app installable and usable offline by caching the app
   shell (HTML/CSS/JS/icons). It deliberately does NOT cache API or WebSocket
   traffic — alerts must always come from the live backend, never from a stale
   cache. When the network is unreachable, navigations fall back to the cached
   shell so the app still opens (showing the last-known UI / offline notice).
   Scope is limited to /m/ because the worker is served from /m/.
   ────────────────────────────────────────────────────────────────────────── */

const CACHE = 'sams-teacher-v4';   // bumped: Web Push notifications (FR9/FR12)

// App-shell assets to precache. Paths are absolute under the /m/ scope.
const SHELL = [
  '/m/',
  '/m/index.html',
  '/m/styles.css',
  '/m/app.js',
  '/m/manifest.webmanifest',
  '/m/icons/icon.svg',
  '/m/icons/icon-192.png',
  '/m/icons/icon-512.png',
];

// Install: precache the shell, then activate immediately.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// Activate: drop any old cache versions, take control of open clients.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle our own origin; never touch anything cross-origin (fonts, etc.).
  if (url.origin !== self.location.origin) return;

  // Never intercept live data: API calls, audio, and WebSocket upgrades go
  // straight to the network so alerts are always fresh.
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) return;

  // App-shell navigations: try network first (to pick up updates), fall back
  // to the cached shell when offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match('/m/index.html'))
    );
    return;
  }

  // Static assets under our scope: cache-first for speed/offline.
  if (url.pathname.startsWith('/m/')) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req).then((res) => {
        // Runtime-cache successful same-scope GETs for next time.
        if (req.method === 'GET' && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => cached))
    );
  }
});

// ── Web Push (FR9/FR12) — deliver alerts while the app is closed ────────────
// The server sends a minimal payload (no transcript, no PII beyond location):
//   {"type":"ALERT","alert_id","severity","location_name","timestamp"}
// Anything else (or malformed JSON) is defensively ignored — a push handler
// that throws would crash the worker's event, so every step is guarded.
self.addEventListener('push', (event) => {
  let msg = null;
  try { msg = event.data ? event.data.json() : null; } catch { msg = null; }
  if (!msg || msg.type !== 'ALERT' || !msg.alert_id) return;

  const severity = (msg.severity || 'low').toLowerCase();
  const title = `🚨 SAMS ALERT — ${severity.toUpperCase()}`;
  const time = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '';
  const body = `${msg.location_name || 'Unknown location'}${time ? ' · ' + time : ''}`;
  const isHigh = severity === 'high';

  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: '/m/icons/icon-192.png',
      badge: '/m/icons/icon-192.png',
      tag: msg.alert_id,               // dedupe repeat pushes for the same alert
      data: { alert_id: msg.alert_id },
      vibrate: isHigh ? [200, 80, 200] : [150],
      requireInteraction: isHigh,
    })
  );
});

// Tapping a notification: focus an already-open tab and hand it the alert id
// via postMessage, or open a fresh tab pointed at it via the URL hash.
self.addEventListener('notificationclick', (event) => {
  const alertId = event.notification?.data?.alert_id;
  event.notification.close();
  if (!alertId) return;

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      const existing = clientList.find((c) => c.url.includes('/m/'));
      if (existing) {
        existing.focus();
        existing.postMessage({ type: 'OPEN_ALERT', alert_id: alertId });
        return;
      }
      return self.clients.openWindow('/m/#alert=' + encodeURIComponent(alertId));
    })
  );
});
