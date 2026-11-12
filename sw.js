// Offline cache for Study: the home page and every course.
//
// The page and data files are fetched network-first, so a deploy shows up on
// the next load, with the cached copy as the fallback when the network is
// gone, failing (5xx), or too slow: on a train between masts a request can
// hang for a minute rather than fail, so after 3 s the cached copy is served
// and the network answer, if it comes, only refreshes the cache. Lessons and fonts are cache-first (they rarely change) and refreshed
// in the background. /state is never cached: progress goes through the page's
// own localStorage fallback instead.
//
// Only plain 200 responses from this origin are stored, never redirects, so
// a login redirect from an auth proxy in front of the app never gets cached
// as the page.

const VERSION = "v4";
const CACHE = "study-plan-" + VERSION;
const CORE = ["/", "/courses.json", "/manifest.webmanifest",
  "/fonts/archivo.woff2", "/fonts/plexmono-400.woff2", "/fonts/plexmono-500.woff2",
  "/icons/icon.svg", "/icons/icon-192.png", "/icons/apple-touch-icon.png"];
const SLOW_MS = 3000;

function storable(res) {
  return res && res.ok && res.status === 200 && res.type === "basic" && !res.redirected;
}

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    for (const url of CORE) {
      try { const r = await fetch(url, { cache: "no-store" }); if (storable(r)) await cache.put(url, r); } catch (e) { /* offline install: fill later */ }
    }
    // Every course's page, questions, drills and lessons, so reading works
    // offline from the first visit. /courses.json lists them.
    try {
      const courses = await (await fetch("/courses.json", { cache: "no-store" })).json();
      const urls = [];
      for (const [c, lessons] of Object.entries(courses)) {
        urls.push("/" + c + "/", "/" + c + "/quizzes.json", "/" + c + "/drills.json");
        for (const id of lessons) urls.push("/" + c + "/lessons/" + id + ".html");
      }
      await Promise.all(urls.map(async (url) => {
        try { const r = await fetch(url, { cache: "no-store" }); if (storable(r)) await cache.put(url, r); } catch (e) { /* skip */ }
      }));
    } catch (e) { /* skip */ }
    self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Progress, the login and the mentor's pages (another person's view) are never cached.
  if (req.method !== "GET" || url.origin !== location.origin || /(^|\/)state$/.test(url.pathname) ||
      url.pathname.startsWith("/oauth2/") || url.pathname.startsWith("/mentor/")) return;

  const cacheFirst = url.pathname.includes("/lessons/") || ["/fonts/", "/icons/"].some((p) => url.pathname.startsWith(p));
  // A page is stored by its path without the hash: "/" or "/<course>/".
  const key = req.mode === "navigate" ? (url.pathname.endsWith("/") ? url.pathname : url.pathname + "/") : url.pathname;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    if (cacheFirst) {
      const hit = await cache.match(key);
      const refresh = fetch(req).then((r) => { if (storable(r)) cache.put(key, r.clone()); return r; }).catch(() => null);
      if (hit) { event.waitUntil(refresh); return hit; }
      return (await refresh) || Response.error();
    }
    const net = fetch(req).then((r) => { if (storable(r)) cache.put(key, r.clone()); return r; });
    const hit = await cache.match(key);
    if (!hit) return net.catch(() => Response.error());
    try {
      const r = await Promise.race([net, new Promise((res) => setTimeout(res, SLOW_MS, null))]);
      // Anything below 500 goes through, including a redirect to a login
      // page: when the network works and the session expired, signing in is
      // what's needed, and hiding it behind a cached page would only defer it.
      if (r && r.status < 500) return r;
    } catch (e) { /* offline */ }
    event.waitUntil(net.catch(() => null));   // let a slow answer still refresh the cache
    return hit;
  })());
});
