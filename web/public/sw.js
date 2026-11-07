const CACHE = "cc-remote-shell-v5";
const SHELL = [
  "/", "/manifest.webmanifest", "/favicon.svg", "/apple-touch-icon.png",
  "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith("cc-remote-") && key !== CACHE)
      .map((key) => caches.delete(key)),
  )));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin
      || url.pathname.startsWith("/api/") || url.pathname === "/ws"
      || url.pathname === "/healthz"
      || url.pathname === "/cc-remote-build.json") return;
  if (url.pathname === "/html-preview-runner.html"
      || url.pathname === "/html-preview-runner.js"
      || url.pathname.startsWith("/__cc_viewer/")
      || url.pathname.startsWith("/ws/")
      || url.pathname === "/cc-remote-viewer-runner.js"
      || url.pathname === "/cc-remote-viewer-zoom.js") return;
  // Entry points and unversioned shell icons/manifests must revalidate online.
  // Only content-addressed assets can safely use the cache-first path below.
  if (request.mode === "navigate" || SHELL.includes(url.pathname)) {
    event.respondWith(fetch(request, { cache: "no-cache" }).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        void caches.open(CACHE).then((cache) => cache.put(
          request.mode === "navigate" ? "/" : request, copy));
      }
      return response;
    }).catch(() => caches.match(request.mode === "navigate" ? "/" : request).then((response) => response || Response.error())));
    return;
  }
  event.respondWith(caches.match(request).then((cached) => cached || fetch(request).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      void caches.open(CACHE).then((cache) => cache.put(request, copy));
    }
    return response;
  })));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  let target = "/";
  try {
    const candidate = new URL(event.notification.data?.url || "/", self.location.origin);
    if (candidate.origin === self.location.origin) {
      target = `${candidate.pathname}${candidate.search}${candidate.hash}`;
    }
  } catch { /* same-origin fallback */ }
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true })
    .then((clients) => {
      const existing = clients.find((client) => "focus" in client);
      if (!existing) return self.clients.openWindow(target);
      existing.postMessage({
        type: "cc-remote-notification",
        route: event.notification.data?.route,
      });
      return existing.focus();
    }));
});

self.addEventListener("push", (event) => {
  let payload = {};
  try { payload = event.data?.json() ?? {}; } catch { /* generic fallback */ }
  const title = typeof payload.title === "string" ? payload.title : "cc-remote";
  const body = typeof payload.body === "string" ? payload.body : "远程会话状态已更新";
  const tag = typeof payload.tag === "string" ? payload.tag : "cc-remote-turn";
  const url = typeof payload.url === "string" ? payload.url : "/";
  event.waitUntil(self.registration.showNotification(title, {
    body, tag, icon: "/icon-192.png", badge: "/favicon.svg",
    data: { url, route: payload.route },
  }));
});
