/* Network-first prototype shell: avoids serving stale UI after an update. */
const CACHE='pazlix-ui-v6-1';
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['./index.html','./styles.css','./app.js','./prototype-store.js','./icons.svg','./app-icon.svg'])));self.skipWaiting();});
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith('pazlix-ui-')&&k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET'||new URL(e.request.url).origin!==self.location.origin)return;
  e.respondWith(fetch(e.request).then(response=>{if(response.ok){const clone=response.clone();e.waitUntil(caches.open(CACHE).then(c=>c.put(e.request,clone)));}return response;}).catch(()=>caches.match(e.request).then(r=>r||new Response('Для первого открытия страницы подключитесь к интернету.',{status:503,headers:{'Content-Type':'text/plain;charset=utf-8'}}))));
});
