// DeviceManager service worker.
// Static assets are cache-first (fast loads, offline shell); everything else
// (navigations, API, terminal) goes straight to the network so device data is
// never served stale. Bump CACHE when static files change.
const CACHE = "dm-static-v2";
// Only unversioned assets are precached. style.css / app.js carry a ?v=<hash>
// query, so they cache per-version on first request and bust automatically when
// the hash changes — precaching them (or matching with ignoreSearch) would pin
// a stale copy.
const ASSETS = [
  "/static/favicon.svg",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/vendor/xterm.js",
  "/static/vendor/xterm-addon-fit.js",
  "/static/vendor/xterm.css",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // Cache-first for static assets, keyed by full URL so ?v=<hash> busts on change.
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(e.request).then(
        (hit) => hit || fetch(e.request).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return res;
        })
      )
    );
  }
  // Everything else: default network behaviour (no caching of live data).
});
