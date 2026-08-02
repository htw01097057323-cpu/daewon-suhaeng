/* 수행평가 알리미 — 서비스 워커
   - 알림 표시: 모바일 크롬은 new Notification()을 막아두어, 알림은 이 워커가 대신 띄웁니다.
   - 오프라인: 한 번 연 페이지는 인터넷이 끊겨도 열립니다(네트워크 우선, 실패 시 캐시).
   - 푸시: 나중에 푸시 서버를 붙이면 페이지를 닫아도 알림이 오도록 push 핸들러를 준비해 뒀습니다. */

const CACHE = 'ssh-cache-v1';

self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(['./']).catch(() => {})));
});

self.addEventListener('activate', e => {
  e.waitUntil(Promise.all([
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))),
    self.clients.claim()
  ]));
});

// 항상 최신 파일을 우선 받아오고, 실패하면 캐시로 대체합니다.
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  e.respondWith(
    fetch(req).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
      return res;
    }).catch(() => caches.match(req).then(m => m || caches.match('./')))
  );
});

// 페이지가 보낸 알림 요청을 받아 표시합니다.
self.addEventListener('message', e => {
  const d = e.data || {};
  if (d.type !== 'notify') return;
  e.waitUntil(self.registration.showNotification(d.title || '수행평가 알리미', {
    body: d.body || '',
    tag: d.tag || 'ssh-deadline',
    renotify: true,
    icon: './icon.svg',
    badge: './icon.svg',
    data: { url: d.url || './' }
  }));
});

// 알림을 누르면 이미 열린 탭으로 이동하고, 없으면 새로 엽니다.
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || './';
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) { if ('focus' in c) return c.focus(); }
    if (self.clients.openWindow) return self.clients.openWindow(url);
  }));
});

// 푸시 서버 연결 시 페이지가 닫혀 있어도 알림이 도착합니다.
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title || '수행평가 알리미', {
    body: d.body || '마감이 임박한 수행평가가 있어요.',
    icon: './icon.svg',
    badge: './icon.svg',
    tag: 'ssh-push',
    data: { url: d.url || './' }
  }));
});
