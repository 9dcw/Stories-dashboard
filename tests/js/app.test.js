const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() {
    this.hidden = false;
    this.textContent = '';
    this.innerHTML = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.style = {};
    this.dataset = {};
    this.classList = { add() {}, remove() {} };
  }
  addEventListener() {}
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
}

function loadApp({ configResponse, pageHref = 'https://stories.example/', deploymentMode = 'interactive' } = {}) {
  const elements = new Map();
  const getElement = key => {
    if (!elements.has(key)) elements.set(key, new Element());
    return elements.get(key);
  };
  const document = {
    documentElement: { dataset: {} },
    querySelector: selector => getElement(selector),
    getElementById: id => getElement(`#${id}`),
    addEventListener() {},
  };
  const storage = new Map();
  let requestCounter = 0;
  const context = vm.createContext({
    document,
    window: { location: { origin: new URL(pageHref).origin, href: pageHref } },
    navigator: {},
    localStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
    },
    fetch: async request => {
      const url = new URL(String(request), pageHref);
      if (url.pathname.endsWith('/deployment-mode.json')) {
        const expectedBase = new URL(pageHref).pathname.startsWith('/Stories-dashboard/')
          ? '/Stories-dashboard/' : '/';
        if (!url.pathname.startsWith(expectedBase)) {
          return { ok: false, status: 404, json: async () => ({}) };
        }
        return { ok: true, status: 200, json: async () => ({ mode: deploymentMode }) };
      }
      if (String(request).includes('/api/v1/config')) {
        return configResponse || { ok: false, status: 404, json: async () => ({}) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ stories: [], recent_candidates: [], sources: [] }),
      };
    },
    crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(++requestCounter).padStart(12, '0')}` },
    URL,
    console,
    setTimeout,
    clearTimeout,
  });
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app.js'), 'utf8');
  vm.runInContext(source, context, { filename: 'app.js' });
  return { context, getElement };
}

test('dismiss retains the candidate and reports an HTTP mutation failure', async () => {
  const { context, getElement } = loadApp();
  getElement('#dismissConfirm').dataset.reason = 'other';
  getElement('#dismissNote').value = 'keep this card';
  const result = await vm.runInContext(`(async () => {
    state.mode = 'interactive';
    dismissTarget = {item_id: 7, headline: 'Candidate'};
    const messages = [];
    const requestIds = [];
    API.fetch = async (_path, init) => {
      requestIds.push(JSON.parse(init.body).request_id);
      throw new Error('Network down');
    };
    showSnackbar = message => messages.push(message);
    refreshAfterMutation = async () => { throw new Error('must not refresh'); };
    render = () => {};
    await submitDismiss();
    dismissTarget = {item_id: 7, headline: 'Candidate'};
    document.querySelector('#dismissConfirm').dataset.reason = 'other';
    await submitDismiss();
    return {messages, requestIds, queueLength: state.dismissedQueue.length};
  })()`, context);

  assert.equal(result.queueLength, 0);
  assert.match(result.messages.at(-1), /Error: Network down/);
  assert.equal(result.requestIds.length, 2);
  assert.equal(result.requestIds[0], result.requestIds[1]);
});

test('restore reports an HTTP mutation failure without refreshing', async () => {
  const { context } = loadApp();
  const result = await vm.runInContext(`(async () => {
    state.mode = 'interactive';
    const messages = [];
    let refreshes = 0;
    API.fetch = async () => ({ok:false,status:409,json:async()=>({detail:'Dismissal is no longer active'})});
    showSnackbar = message => messages.push(message);
    refreshAfterMutation = async () => { refreshes += 1; };
    render = () => {};
    await restoreItem(7, 2);
    return {messages, refreshes};
  })()`, context);

  assert.equal(result.refreshes, 0);
  assert.match(result.messages.at(-1), /Restore failed: Dismissal is no longer active/);
});

test('missing interactive API does not silently downgrade to static mode', async () => {
  const { context, getElement } = loadApp({
    configResponse: { ok: false, status: 404, json: async () => ({}) },
  });
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.equal(vm.runInContext('state.mode', context), 'unavailable');
  assert.match(getElement('#updatedAt').textContent, /interactive service/i);
});

test('static dashboard resolves deployment mode from its GitHub Pages subpath', async () => {
  const { context } = loadApp({
    pageHref: 'https://stories.example/Stories-dashboard/',
    deploymentMode: 'static',
  });
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.equal(vm.runInContext('state.mode', context), 'static');
});


test('HTML escaping handles backslashes without emitting undefined', () => {
  const { context } = loadApp();
  assert.equal(vm.runInContext("esc('a\\\\b')", context), 'a&bsol;b');
});
