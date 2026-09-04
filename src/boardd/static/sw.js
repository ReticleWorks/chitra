// boardd service worker: shell-only cache, no offline data.
//
// Caches exactly the static shell (the board page, the manifest, the
// icons) so a repeat visit paints instantly on a slow connection. It never
// caches /world, /escalations or /events — board data is always live,
// never served from a stale cache pretending to be current.
const CACHE = "boardd-shell-v2";
const SHELL = ["/", "/static/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png"];

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
