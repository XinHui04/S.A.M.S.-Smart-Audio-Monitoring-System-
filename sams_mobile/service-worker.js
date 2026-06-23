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

const CACHE = 'sams-teacher-v1';

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
