// Paid2Match service worker (Phase 2).
//
// Cache the shell and the branding, never the social data. This is a
// small server-rendered Flask app, not a SPA: the precache is the
// static assets every page loads, plus the offline fallback page.
// Navigations are network-first (pages are server-rendered and mostly
// login-walled — never serve a stale cached copy of someone's feed,
// messages, or account pages) and fall back to /offline. /static/* is
// cache-first from the precache, refreshed by bumping CACHE_VERSION on
// deploy. /auth/*, /api/*, all POSTs, and every other request are
// network-only. NEVER cached: feed data, API responses, user profiles,
// messages, search results, or any HTML other than /offline.

const CACHE_VERSION = 'p2m-v5';
const CACHE_NAME = CACHE_VERSION;
const OFFLINE_URL = '/offline';

// ~860 KB total — well under 1 MB. icons.svg is precached too: the
// logo mark lives in the sprite now (logo_mark macro), so every page
// needs it, including offline.
const PRECACHE_URLS = [
    OFFLINE_URL,
    '/static/css/bootstrap.min.css',
    '/static/css/font-awesome.min.css',
    '/static/css/google-fonts.css',
    '/static/css/main.css',
    '/static/js/bootstrap.bundle.min.js',
    '/static/js/main.js',
    '/static/img/logo-mark.svg',
    '/static/img/icons.svg',
    '/static/img/favicon.svg',
    '/static/img/icon-192.png',
    '/static/img/icon-512.png',
    '/static/webfonts/fa-brands-400.woff2',
    '/static/webfonts/fa-regular-400.woff2',
    '/static/webfonts/fa-solid-900.woff2',
];

self.addEventListener('install', function (event) {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(function (cache) { return cache.addAll(PRECACHE_URLS); })
            .then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener('activate', function (event) {
    event.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(keys.map(function (key) {
                if (key !== CACHE_NAME) return caches.delete(key);
            }));
        }).then(function () { return self.clients.claim(); })
    );
});

self.addEventListener('fetch', function (event) {
    const request = event.request;
    const url = new URL(request.url);

    // Only same-origin GETs are ever handled. POSTs, /auth/*, /api/*,
    // and cross-origin requests (e.g. Google Fonts) go to the network
    // untouched — the page renders fine with fallback fonts offline.
    if (request.method !== 'GET' || url.origin !== self.location.origin) {
        return;
    }
    if (url.pathname.startsWith('/auth/') || url.pathname.startsWith('/api/')) {
        return;
    }

    // Navigations: network-first, fall back to the cached offline page.
    if (request.mode === 'navigate') {
        event.respondWith(
            fetch(request).catch(function () {
                return caches.match(OFFLINE_URL);
            })
        );
        return;
    }

    // Static assets: cache-first from the precache (refreshed by
    // bumping CACHE_VERSION on deploy). Anything not precached falls
    // through to the network and is NOT added to the cache.
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(request).then(function (cached) {
                return cached || fetch(request);
            })
        );
        return;
    }

    // Everything else (feed data, profiles, messages, search, any
    // other HTML): network-only, never cached.
});
