const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadWorker() {
  const listeners = {};
  const calls = { fetch: 0, cacheMatch: 0 };
  const context = vm.createContext({
    self: {
      location: { origin: 'https://stories.example' },
      clients: { claim: async () => undefined },
      skipWaiting: async () => undefined,
      addEventListener: (name, callback) => { listeners[name] = callback; },
    },
    caches: {
      open: async () => ({ addAll: async () => undefined, put: async () => undefined }),
      keys: async () => [],
      delete: async () => true,
      match: async () => { calls.cacheMatch += 1; return 'cached'; },
    },
    fetch: async () => { calls.fetch += 1; return 'network'; },
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
