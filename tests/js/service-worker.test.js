const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadWorker() {
  const listeners = {};
  const calls = { fetch: 0, cacheMatch: 0, fetchOptions: [], precached: [] };
  const context = vm.createContext({
    self: {
      location: { origin: 'https://stories.example' },
      clients: { claim: async () => undefined },
      skipWaiting: async () => undefined,
      addEventListener: (name, callback) => { listeners[name] = callback; },
    },
    caches: {
      open: async () => ({ addAll: async files => { calls.precached = files; }, put: async () => undefined }),
      keys: async () => [],
      delete: async () => true,
      match: async () => { calls.cacheMatch += 1; return 'cached'; },
    },
    fetch: async (request,options) => { calls.fetch += 1; calls.fetchOptions.push(options); return request.url.includes('/api/')?'network':{clone:()=>({})}; },
    URL,
    Promise,
  });
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'sw.js'), 'utf8');
  vm.runInContext(source, context, { filename: 'sw.js' });
  return { listeners, calls };
}

test('GET API requests are network-only and never consult caches', async () => {
  const { listeners, calls } = loadWorker();
  let response;
  listeners.fetch({
    request: { method: 'GET', url: 'https://stories.example/api/v1/dashboard' },
    respondWith: promise => { response = promise; },
  });

  assert.equal(await response, 'network');
  assert.equal(calls.fetch, 1);
  assert.equal(calls.cacheMatch, 0);
});

test('non-GET requests are not intercepted by the service worker', () => {
  const { listeners, calls } = loadWorker();
  let intercepted = false;
  listeners.fetch({
    request: { method: 'POST', url: 'https://stories.example/api/v1/items/7/dismiss' },
    respondWith: () => { intercepted = true; },
  });

  assert.equal(intercepted, false);
  assert.equal(calls.fetch, 0);
  assert.equal(calls.cacheMatch, 0);
});


test('app shell requests revalidate the browser HTTP cache, including versioned assets', async () => {
  for (const path of ['/','/index.html','/app.js?v=one-click-dismiss-v2','/styles.css?v=one-click-dismiss-v2']) {
    const {listeners,calls}=loadWorker();
    let response;
    listeners.fetch({request:{method:'GET',url:'https://stories.example'+path},respondWith:promise=>{response=promise;}});
    await response;
    assert.equal(calls.fetchOptions[0].cache,'no-cache');
    assert.equal(calls.cacheMatch,0);
  }
});


test('precache includes the exact versioned script and stylesheet referenced by the page', async () => {
  const {listeners,calls}=loadWorker();
  let installed;
  listeners.install({waitUntil:promise=>{installed=promise;}});
  await installed;
  const html=fs.readFileSync(path.join(__dirname,'..','..','index.html'),'utf8');
  const assets=[...html.matchAll(/(?:src|href)="(\.\/(?:app\.js|styles\.css)\?[^"]+)"/g)].map(match=>match[1]);
  assert.equal(assets.length,2);
  for (const asset of assets) assert.ok(calls.precached.includes(asset),asset+' missing from precache');
});
