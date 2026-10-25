const ASSET_VERSION='20261026-phase2-delete';
const CACHE=`stories-dashboard-${ASSET_VERSION}`;
const FILES=['./','./index.html',`./styles.css?v=${ASSET_VERSION}`,`./app.js?v=${ASSET_VERSION}`,'./manifest.json'];
const APP_SHELL_PATH=/\/(?:index\.html|styles\.css|app\.js|manifest\.json)$|\/$/;

self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(FILES)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));

self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET') return;
  const url=new URL(event.request.url);
  // API traffic stays network-only; offline cache must never mask current status.
  if(url.pathname.startsWith('/api/')){
    event.respondWith(fetch(event.request));
    return;
  }
  const isAppShell=url.origin===self.location.origin&&APP_SHELL_PATH.test(url.pathname);
  event.respondWith(
    isAppShell
      ? fetch(event.request,{cache:'no-cache'}).then(response=>{const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));return response}).catch(()=>caches.match(event.request))
      : caches.match(event.request).then(cached=>cached||fetch(event.request).then(response=>{const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));return response}).catch(()=>caches.match('./')))
  );
});