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
  const fetchCalls = [];
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
      fetchCalls.push(String(request));
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
    setInterval() {},
    clearTimeout,
  });
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'app.js'), 'utf8');
  vm.runInContext(source, context, { filename: 'app.js' });
  return { context, getElement, fetchCalls };
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
  const { context, getElement, fetchCalls } = loadApp({
    configResponse: { ok: false, status: 404, json: async () => ({}) },
  });
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.equal(vm.runInContext('state.mode', context), 'unavailable');
  assert.match(getElement('#updatedAt').textContent, /interactive service/i);
  assert.ok(fetchCalls.includes('/api/v1/config'));
  assert.ok(!fetchCalls.some(url => url.includes('deployment-mode.json')));
});

test('live dashboard initializes directly against the interactive API', async () => {
  const { context, fetchCalls } = loadApp({
    configResponse: { ok: true, status: 200, json: async () => ({ google_client_id: 'client-id' }) },
  });
  await new Promise(resolve => setTimeout(resolve, 10));

  assert.equal(vm.runInContext('state.mode', context), 'interactive');
  assert.ok(fetchCalls.includes('/api/v1/config'));
  assert.ok(!fetchCalls.some(url => url.includes('deployment-mode.json')));
});


test('HTML escaping handles backslashes without emitting undefined', () => {
  const { context } = loadApp();
  assert.equal(vm.runInContext("esc('a\\\\b')", context), 'a&bsol;b');
});

test('default filters show complete court/legal candidates and reset restores those defaults', () => {
  const { context, getElement } = loadApp();
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('defaultFilters', context))), {
    kind:'candidate',phase:'Candidate',lane:'B',source:'',dismissed:'hide',summary:'COMPLETE',activity:'',sourceStatus:''
  });
  vm.runInContext(`
    state.sources=[{name:'Regulator',lane:'A',source_id:1,active:true}];
    state.stories=[{story_id:1,title:'Phase two',status:'Phase 2',source:'Regulator'},{story_id:2,title:'Phase three',status:'Phase 3',source:'Regulator'}];
    state.candidates=[{item_id:3,headline:'Candidate',source:'Regulator',lane:'A',status:'NEW'}];
    state.proposals=[{name:'New source',lane:'B'}];
    state.filters={...defaultFilters,kind:'',phase:'Phase 2',lane:'',summary:''};render();
  `, context);
  assert.equal(getElement('#candidateSection').hidden, true);
  assert.equal(getElement('#sourceReviewSection').hidden, true);
  assert.equal(getElement('#sourceManagementSection').hidden, true);
  assert.equal(getElement('#promotedSection').hidden, false);
  assert.match(getElement('#storyGrid').innerHTML, /Phase two/);
  assert.doesNotMatch(getElement('#storyGrid').innerHTML, /Phase three/);
  vm.runInContext('state.filters={...defaultFilters};render()', context);
  assert.equal(getElement('#candidateSection').hidden, true);
  assert.equal(getElement('#promotedSection').hidden, true);
  assert.equal(getElement('#sourceReviewSection').hidden, true);
  assert.equal(getElement('#sourceManagementSection').hidden, true);
});

test('source, content area, dismissal, summary and activity facets compose consistently', () => {
  const { context, getElement } = loadApp();
  const result = vm.runInContext(`(() => {
    state.sources=[{name:'Regulator',lane:'A',active:true}];
    const story={story_id:1,source:'Regulator',status:'Phase 2',title:'Court case'};
    const candidate={item_id:2,source:'Regulator',lane:'A',status:'DISMISSED',summary_status:'FAILED'};
    state.filters={...defaultFilters,kind:'',phase:'',lane:'',source:'Regulator',summary:''};
    const sourceAndLane=matchesRecord(story,'story');
    state.filters.source='Other';const wrongSource=matchesRecord(story,'story');
    state.filters={...defaultFilters,kind:'',phase:'',lane:'',dismissed:'only',summary:'FAILED'};
    const dismissed=matchesRecord(candidate,'candidate');
    const unrelated=matchesRecord(story,'story');
    state.filters={...defaultFilters,kind:'',phase:'',lane:'',summary:'',activity:'recent'};state.recent=['1'];
    const recent=matchesRecord(story,'story');
    const recentCandidate=matchesRecord(candidate,'candidate');
    state.filters={...defaultFilters,kind:'',phase:'Phase 3',lane:'',summary:''};state.stories=[story];render();
    return {sourceAndLane,wrongSource,dismissed,unrelated,recent,recentCandidate};
  })()`,context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {sourceAndLane:true,wrongSource:false,dismissed:true,unrelated:false,recent:true,recentCandidate:false});
  assert.equal(getElement('#emptyState').hidden,false);
});


test('Phase 2 promotion stays in dashboard and setup failures expose a safe retry', async () => {
  const { context } = loadApp();
  const result = await vm.runInContext(`(async () => {
    let redirected=false;
    window.location.assign=()=>{redirected=true};
    API.fetch=async()=>({ok:true,status:202,json:async()=>({story:{status:'Phase 2 pending'},telegram_handoff_url:'https://t.me/old'})});
    refreshAfterMutation=async()=>{};
    showSnackbar=()=>{};
    await requestPhase2(1);
    const html=phase2Progress({story_id:7,status:'Phase 2 pending',phase2_job:{state:'failed',step:'drive',attempts:5,error:'<script>bad</script>'}});
    return {redirected,html};
  })()`, context);
  assert.equal(result.redirected, false);
  assert.match(result.html,/data-phase2-retry="7"/);
  assert.match(result.html,/&lt;script&gt;/);
  assert.doesNotMatch(result.html,/<script>/);
});
