// boardd service worker: shell-only cache, no offline data.
//
// Caches exactly the static shell (this page, its css/js, the manifest) so
// a repeat visit paints instantly on a slow connection. It never caches
// /api/state or /events — board data is always live or explicitly stale,
// never served from a stale cache pretending to be current.
const CACHE = "boardd-shell-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || !SHELL.includes(url.pathname)) return;
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
