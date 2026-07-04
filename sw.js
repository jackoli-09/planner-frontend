const CACHE = 'planner-v1';
const OFFLINE_QUEUE_KEY = 'planner_offline_queue';

const STATIC = [
  '/',
  '/index.html',
  'https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700;800&display=swap',
  'https://telegram.org/js/telegram-web-app.js',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC.map(u => new Request(u, {mode:'no-cors'}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API запросы — network first, фоллбек в офлайн очередь
  if (url.pathname.startsWith('/api/') || url.hostname.includes('railway.app')) {
    if (e.request.method !== 'GET') {
      // Мутирующие запросы — пробуем отправить, при ошибке сохраняем в очередь
      e.respondWith(
        fetch(e.request.clone()).catch(async () => {
          // Сохраняем в IndexedDB очередь
          const body = await e.request.clone().text().catch(() => '');
          await saveToQueue({
            url: e.request.url,
            method: e.request.method,
            headers: Object.fromEntries(e.request.headers.entries()),
            body,
            timestamp: Date.now()
          });
          return new Response(JSON.stringify({status:'queued',offline:true}), {
            headers:{'Content-Type':'application/json'}
          });
        })
      );
      return;
    }
    // GET к API — network first, нет кеша
    e.respondWith(fetch(e.request).catch(() =>
      new Response(JSON.stringify({error:'offline'}), {
        headers:{'Content-Type':'application/json'}
      })
    ));
    return;
  }

  // Статика — cache first
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(resp => {
        if (resp && resp.status === 200 && resp.type !== 'opaque') {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => caches.match('/index.html'));
    })
  );
});

// IndexedDB для очереди
function openDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open('planner_sw', 1);
    req.onupgradeneeded = e => e.target.result.createObjectStore('queue', {keyPath:'id',autoIncrement:true});
    req.onsuccess = e => res(e.target.result);
    req.onerror = () => rej(req.error);
  });
}

async function saveToQueue(item) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction('queue','readwrite');
    tx.objectStore('queue').add(item);
    tx.oncomplete = res;
    tx.onerror = () => rej(tx.error);
  });
}

async function getQueue() {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction('queue','readonly');
    const req = tx.objectStore('queue').getAll();
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}

async function clearQueue() {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction('queue','readwrite');
    tx.objectStore('queue').clear();
    tx.oncomplete = res;
    tx.onerror = () => rej(tx.error);
  });
}

// Синхронизация при восстановлении соединения
self.addEventListener('sync', e => {
  if (e.tag === 'planner-sync') {
    e.waitUntil(syncQueue());
  }
});

async function syncQueue() {
  const queue = await getQueue();
  if (!queue.length) return;
  
  let allOk = true;
  for (const item of queue) {
    try {
      await fetch(item.url, {
        method: item.method,
        headers: item.headers,
        body: item.body || undefined,
      });
    } catch {
      allOk = false;
      break;
    }
  }
  
  if (allOk) {
    await clearQueue();
    // Сообщаем клиентам что синхронизация завершена
    const clients = await self.clients.matchAll();
    clients.forEach(c => c.postMessage({type:'SYNC_DONE'}));
  }
}

// Принимаем команду на синхронизацию от клиента
self.addEventListener('message', e => {
  if (e.data?.type === 'SYNC_NOW') {
    syncQueue().then(() => {
      e.source?.postMessage({type:'SYNC_DONE'});
    });
  }
});
