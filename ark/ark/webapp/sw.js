/* ARK companion — minimal service worker: precache the app shell so it installs
   and opens offline. Uploads (/ingest) always go to the network. */
const CACHE = 'ark-shell-v1';
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // never cache API calls; network only
  if (e.request.method !== 'GET' || url.pathname === '/status' || url.pathname === '/ingest') return;
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
