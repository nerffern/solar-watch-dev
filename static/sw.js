/**
 * SolarWatch — sw.js  (Service Worker v2)
 *
 * Caching strategy — carefully matched to how this app actually behaves:
 *
 *  ┌─────────────────────────────┬──────────────────────────────────────────┐
 *  │ Request type                │ Strategy                                 │
 *  ├─────────────────────────────┼──────────────────────────────────────────┤
 *  │ /api/*  /health             │ Network Only — live data, never stale    │
 *  │ /manifest.json  /sw.js      │ Network Only — must always be fresh      │
 *  │ /  (app shell)              │ Cache First → background revalidate      │
 *  │ /static/icons/*             │ Cache First → Network fallback           │
 *  │ jsdelivr CDN (chart.js)     │ Cache First (versioned URL = safe)       │
 *  │ Google Fonts CSS            │ Network First → Cache fallback           │
 *  │ Google Fonts woff2          │ Cache First → Network                    │
 *  └─────────────────────────────┴──────────────────────────────────────────┘
 *
 *  PRECACHE: only same-origin assets we fully control — NO CDN URLs.
 *  If a CDN is unreachable during SW install it does NOT break the PWA.
 *  CDN assets are cached opportunistically on first successful load.
 *
 *  OFFLINE: API calls return a JSON error so the app handles them gracefully.
 *  The SW broadcasts OFFLINE/ONLINE messages to the app so it can show/hide
 *  an inline banner without reloading the page.
 */

const CACHE_NAME = 'solarwatch-v2';

// Paths that must NEVER be cached — always live
const NETWORK_ONLY_PREFIXES = ['/api/', '/health'];
const NETWORK_ONLY_EXACT    = ['/manifest.json', '/sw.js'];

// Same-origin assets to pre-cache on SW install.
// NO CDN URLs — a CDN failure would abort the entire installation.
const PRECACHE_URLS = [
  '/',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/icons/apple-touch-icon.png',
  '/static/icons/favicon-32x32.png',
  '/static/icons/favicon-16x16.png',
];

// ── OFFLINE PAGE ──────────────────────────────────────────────────────────────
const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SolarWatch — Offline</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:#0a0c10;color:#e8eaf2;
  font-family:'Barlow',system-ui,sans-serif;
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;height:100vh;gap:20px;padding:24px;text-align:center;
}
.sun{font-size:56px;animation:glow 2s ease-in-out infinite alternate}
@keyframes glow{from{filter:drop-shadow(0 0 8px rgba(245,166,35,.4))}to{filter:drop-shadow(0 0 22px rgba(245,166,35,.9))}}
h1{font-size:clamp(22px,5vw,34px);font-weight:800;letter-spacing:-.02em;color:#f5a623}
p{color:#8090b8;font-size:clamp(13px,2.5vw,17px);max-width:360px;line-height:1.55}
.hint{font-size:13px;color:#4a5070;margin-top:4px}
button{
  margin-top:4px;padding:11px 26px;border-radius:8px;border:none;cursor:pointer;
  background:#f5a623;color:#000;font-weight:700;font-size:15px;
  font-family:inherit;letter-spacing:.04em;transition:background .2s;
}
button:hover{background:#e09510}
</style>
</head>
<body>
  <div class="sun">☀️</div>
  <h1>SolarWatch</h1>
  <p>You're offline — the server isn't reachable right now.</p>
  <p class="hint">Live data will resume automatically when your connection is restored.</p>
  <button onclick="location.reload()">Try Again</button>
</body>
</html>`;

// ── INSTALL ───────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        // addAll is atomic per URL — wrap individually so one failure doesn't kill all
        return Promise.allSettled(
          PRECACHE_URLS.map(url => cache.add(url).catch(e =>
            console.warn('[SW] Pre-cache skipped:', url, e.message)
          ))
        );
      })
      .then(() => self.skipWaiting())
  );
});

// ── ACTIVATE ──────────────────────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => {
          console.log('[SW] Purging old cache:', k);
          return caches.delete(k);
        })
      ))
      .then(() => self.clients.claim())
  );
});

// ── FETCH ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;  // Only intercept GET

  const url = new URL(req.url);

  // ── 1. API + health — Network Only, return structured error offline ────────
  if (NETWORK_ONLY_PREFIXES.some(p => url.pathname.startsWith(p))) {
    event.respondWith(
      fetch(req).then(res => {
        notifyOnline();
        return res;
      }).catch(() => {
        notifyOffline();
        return new Response(
          JSON.stringify({ error: 'offline', stale: true }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        );
      })
    );
    return;
  }

  // ── 2. Manifest + SW — Network Only (always fresh) ────────────────────────
  if (NETWORK_ONLY_EXACT.includes(url.pathname)) {
    event.respondWith(fetch(req).catch(() =>
      new Response('', { status: 503 })
    ));
    return;
  }

  // ── 3. Google Fonts CSS — Network First, cache fallback ───────────────────
  // Font CSS is versioned by Google but we don't control it.
  // Network first ensures we get the latest; cache covers offline.
  if (url.hostname === 'fonts.googleapis.com') {
    event.respondWith(
      fetch(req).then(res => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then(c => c.put(req, clone));
        return res;
      }).catch(() => caches.match(req).then(cached => cached ||
        new Response('', { status: 503 })
      ))
    );
    return;
  }

  // ── 4. Google Font files (woff2) — Cache First, safe because URLs are stable
  if (url.hostname === 'fonts.gstatic.com') {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) return cached;
        return fetch(req).then(res => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
          return res;
        });
      })
    );
    return;
  }

  // ── 5. jsdelivr CDN (chart.js@4.4.0, adapter) — Cache First ─────────────
  // URLs contain exact version numbers — safe to cache forever.
  // If CDN unreachable offline and not yet cached, return a stub so the
  // page doesn't hard-error (charts just won't render — acceptable offline).
  if (url.hostname === 'cdn.jsdelivr.net') {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) return cached;
        return fetch(req).then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(req, clone));
          }
          return res;
        }).catch(() =>
          new Response('/* CDN unavailable offline */', {
            status: 200,
            headers: { 'Content-Type': 'application/javascript' }
          })
        );
      })
    );
    return;
  }

  // ── 6. Everything else (app shell, icons, static) ─────────────────────────
  // Stale-While-Revalidate: serve from cache instantly, update in background.
  // Navigation requests (full page loads) fall back to offline page if no cache.
  event.respondWith(
    caches.match(req).then(cached => {
      const networkFetch = fetch(req).then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
        }
        notifyOnline();
        return res;
      }).catch(() => {
        notifyOffline();
        if (req.mode === 'navigate') {
          return new Response(OFFLINE_HTML, {
            status: 200,
            headers: { 'Content-Type': 'text/html; charset=utf-8' }
          });
        }
        return new Response('', { status: 503 });
      });

      // Return cache immediately if available; otherwise wait for network
      return cached || networkFetch;
    })
  );
});

// ── ONLINE / OFFLINE BROADCAST ────────────────────────────────────────────────
// Sends messages to all open app windows so they can show/hide the offline
// banner without polling navigator.onLine (which is unreliable).

let _offlineState = false;

function notifyOffline() {
  if (_offlineState) return;
  _offlineState = true;
  broadcast({ type: 'SW_OFFLINE' });
}

function notifyOnline() {
  if (!_offlineState) return;
  _offlineState = false;
  broadcast({ type: 'SW_ONLINE' });
}

function broadcast(msg) {
  self.clients.matchAll({ includeUncontrolled: true, type: 'window' })
    .then(clients => clients.forEach(c => c.postMessage(msg)));
}
