const CACHE_VERSION = 'planner-shell-v47';
const CORE_ASSETS = [
  './',
  './index.html',
  'https://telegram.org/js/telegram-web-app.js',
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js',
  'https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700;800&family=Unbounded:wght@500;600;700&display=swap'
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_VERSION);
    await Promise.allSettled(CORE_ASSETS.map(url => cache.add(url)));
    await self.skipWaiting();
  })());
});

self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(name => name !== CACHE_VERSION).map(name => caches.delete(name)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.hostname.includes('planner-backend') || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    // Сеть первой, но не дольше 3 секунд: при плохой связи открываем
    // приложение из кеша, а свежую версию докачиваем в фоне.
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_VERSION);
      const network = fetch(request).then(response => {
        if (response && response.ok) cache.put('./index.html', response.clone());
        return response;
      });
      const cached = await caches.match('./index.html');
      if (!cached) return network.catch(() => Response.error());
      event.waitUntil(network.catch(() => null));
      const timeout = new Promise(resolve => setTimeout(() => resolve(null), 3000));
      try {
        const winner = await Promise.race([network, timeout]);
        return winner || cached;
      } catch (_) {
        return cached;
      }
    })());
    return;
  }

  event.respondWith((async () => {
    const cached = await caches.match(request);
    if (cached) return cached;
    try {
      const response = await fetch(request);
      if (response && (response.ok || response.type === 'opaque')) {
        const cache = await caches.open(CACHE_VERSION);
        cache.put(request, response.clone());
      }
      return response;
    } catch (_) {
      return Response.error();
    }
  })());
});
