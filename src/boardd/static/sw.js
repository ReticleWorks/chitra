// boardd service worker: shell-only cache, no offline data.
//
// Caches exactly the static shell (the board page, the manifest, the
// icons) so a repeat visit paints on a slow connection. It never caches
// /world, /escalations or /events — board data is always live, never served
// from a stale cache pretending to be current. The board page itself is
// network-first for the same reason.
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
  // The board page changes with every boardd release, so it is network-first:
  // cache-first kept an installed viewer on the old board until someone
  // hand-bumped CACHE. The cached copy is the offline fallback only. Matched
  // and stored under "/" because the page is also requested as "/?monitor=x".
  if (url.pathname === "/") {
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put("/", copy));
          return res;
        })
        .catch(() => caches.match("/").then((cached) => cached || Response.error())),
    );
    return;
  }
  // The icons and the manifest are immutable per release: cache-first.
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
