const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() {
    this.listeners = {};
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
  addEventListener(event, callback) { this.listeners[event] = callback; }
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

test('category switching clears incompatible filters and includes every Phase 2 state', () => {
  const {context,getElement}=loadApp();
  vm.runInContext(`
    state.stories=[{story_id:1,title:'Ready story',status:'Phase 2'},
      {story_id:2,title:'Pending story',status:'Phase 2 pending'},
      {story_id:3,title:'Failed story',status:'Phase 2 pending',phase2_job:{state:'failed'}},
      {story_id:4,title:'Final story',status:'Phase 3'}];
    state.filters.source='Missing';state.filters.lane='B';state.query='no match';
    selectView('phase2');
  `,context);
  assert.match(getElement('#storyGrid').innerHTML,/Ready story/);
  assert.match(getElement('#storyGrid').innerHTML,/Pending story/);
  assert.match(getElement('#storyGrid').innerHTML,/Failed story/);
  assert.doesNotMatch(getElement('#storyGrid').innerHTML,/Final story/);
  assert.equal(vm.runInContext('state.query',context),'');
  assert.equal(vm.runInContext('state.filters.source',context),'');
  vm.runInContext("state.filters.setup='failed';render()",context);
  assert.match(getElement('#storyGrid').innerHTML,/Failed story/);
  assert.doesNotMatch(getElement('#storyGrid').innerHTML,/Ready story|Pending story/);
  vm.runInContext("selectView('phase3')",context);
  assert.match(getElement('#storyGrid').innerHTML,/Final story/);
  assert.doesNotMatch(getElement('#storyGrid').innerHTML,/Failed story/);
});

test('new candidates include all content areas and pending summaries, with opt-in dismissal and exclusions', () => {
  const {context,getElement}=loadApp();
  vm.runInContext(`
    state.candidates=[
      {item_id:1,headline:'Agency candidate',source:'Agency',lane:'A',status:'NEW',summary_status:'COMPLETE'},
      {item_id:2,headline:'Court pending',source:'Court',lane:'B',status:'NEW',summary_status:'PENDING'},
      {item_id:3,headline:'Dismissed item',status:'DISMISSED',summary_status:'FAILED'},
      {item_id:4,headline:'Promoted item',status:'PROMOTED'},
      {item_id:5,headline:'Failed item',summary_status:'FAILED'},
      {item_id:6,headline:'Excluded item',screening:{decision:'EXCLUDED'}},
      {item_id:7,headline:'Already a story',status:'NEW'}];
    state.stories=[{story_id:9,origin_item_id:7,status:'Phase 2'}];renderFilters();render();
  `,context);
  assert.match(getElement('#candidateGrid').innerHTML,/Agency candidate/);
  assert.match(getElement('#candidateGrid').innerHTML,/Court pending/);
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/Dismissed item|Promoted item|Failed item|Excluded item|Already a story/);
  assert.match(getElement('#filters').innerHTML,/All sources/);
  vm.runInContext("state.filters.source='Court';state.query='pending';render()",context);
  assert.match(getElement('#candidateGrid').innerHTML,/Court pending/);
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/Agency candidate/);
  assert.equal(vm.runInContext("categoryCount('candidates')",context),2);
  vm.runInContext("clearFilters();state.filters.dismissed='only';render()",context);
  assert.match(getElement('#candidateGrid').innerHTML,/Dismissed item/);
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/Agency candidate/);
  vm.runInContext("clearFilters();state.filters.screening='EXCLUDED';render()",context);
  assert.match(getElement('#candidateGrid').innerHTML,/Excluded item/);
});

test('Problems combines failures and review, counts each record once and preserves Phase 2 overlap', () => {
  const {context,getElement}=loadApp();
  vm.runInContext(`
    state.candidates=[{item_id:1,headline:'Needs attention',summary_status:'FAILED',screening:{needs_review:true}},
      {item_id:2,headline:'Uncertain opinion',screening:{decision:'PASS',needs_review:true}},
      {item_id:3,headline:'Dismissed failure',status:'DISMISSED',summary_status:'FAILED'},
      {item_id:4,headline:'Promoted failure',status:'PROMOTED',summary_status:'FAILED'}];
    state.sources=[{name:'Broken source',last_error:'Timeout',active:true},{name:'Healthy source',last_error:''}];
    state.stories=[{story_id:7,title:'Setup failure',status:'Phase 2 pending',phase2_job:{state:'failed',step:'drive',attempts:1}}];
    selectView('problems');
  `,context);
  assert.equal(vm.runInContext("categoryCount('problems')",context),4);
  assert.match(getElement('#candidateGrid').innerHTML,/Needs attention|Uncertain opinion/);
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/Dismissed failure|Promoted failure/);
  assert.match(getElement('#storyGrid').innerHTML,/data-phase2-retry="7"/);
  assert.match(getElement('#sourceManagementGrid').innerHTML,/Broken source/);
  assert.doesNotMatch(getElement('#sourceManagementGrid').innerHTML,/Healthy source/);
  vm.runInContext("state.filters.problem='review';render()",context);
  assert.equal(getElement('#sourceManagementSection').hidden,true);
  assert.equal(getElement('#promotedSection').hidden,true);
  assert.equal(getElement('#candidateSection').hidden,false);
  vm.runInContext("selectView('phase2')",context);
  assert.match(getElement('#storyGrid').innerHTML,/Setup failure/);
  vm.runInContext("selectView('candidates')",context);
  assert.match(getElement('#candidateGrid').innerHTML,/Uncertain opinion/);
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/Needs attention/);
});

test('empty categories remain visible and clearing filters keeps the selected category', () => {
  const {context,getElement}=loadApp();
  vm.runInContext("selectView('phase2');state.filters.setup='failed';state.query='absent';render()",context);
  assert.equal(getElement('#emptyState').hidden,false);
  assert.equal(getElement('#promotedSection').hidden,false);
  assert.equal(getElement('#viewHeading').textContent,'Phase 2');
  vm.runInContext('clearFilters()',context);
  assert.equal(vm.runInContext('state.view',context),'phase2');
  assert.equal(vm.runInContext('state.query',context),'');
  assert.equal(vm.runInContext('state.filters.setup',context),'');
});

test('source management filters remain isolated from work categories', () => {
  const {context,getElement}=loadApp();
  vm.runInContext(`state.sources=[{name:'Disabled',active:false},{name:'Active',active:true}];
    selectView('sources');state.filters.sourceStatus='disabled';render()`,context);
  assert.match(getElement('#sourceManagementGrid').innerHTML,/Disabled/);
  assert.doesNotMatch(getElement('#sourceManagementGrid').innerHTML,/>Active</);
  assert.equal(getElement('#courtPipelineStatus').hidden,false);
  vm.runInContext("selectView('candidates')",context);
  assert.equal(getElement('#sourceManagementSection').hidden,true);
  assert.equal(getElement('#courtPipelineStatus').hidden,true);
  assert.equal(vm.runInContext('state.filters.sourceStatus',context),'');
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


test('late Google SDK load renders sign-in after the session check', () => {
  const { context, getElement } = loadApp();
  vm.runInContext("state.googleClientId='test-client';showLoginGate()",context);
  assert.match(getElement('#loginButtonContainer').textContent,/Loading Google/);
  let rendered=0;
  const google={accounts:{id:{initialize(){},renderButton(){rendered++;}}}};
  context.google=google;
  context.window.google=google;
  getElement('script[src="https://accounts.google.com/gsi/client"]').listeners.load();
  assert.equal(rendered,1);
  assert.equal(getElement('#loginButtonContainer').textContent,'');
});

test('blocked Google SDK reports an actionable sign-in error', () => {
  const { getElement } = loadApp();
  getElement('script[src="https://accounts.google.com/gsi/client"]').listeners.error();
  assert.equal(getElement('#loginError').hidden,false);
  assert.match(getElement('#loginError').textContent,/could not load/);
});


test('opinion screening retains uncertain and failed items and exposes exclusions on demand', () => {
  const {context}=loadApp();
  const result=vm.runInContext(`(() => {
    const base={item_id:1,lane:'B',status:'NEW'};
    const pass=matchesRecord({...base,screening:{decision:'PASS',needs_review:true}},'candidate');
    const error=matchesRecord({...base,screening:{decision:'ERROR',needs_review:true}},'candidate');
    const excluded=matchesRecord({...base,screening:{decision:'EXCLUDED'}},'candidate');
    state.filters.screening='EXCLUDED';
    const audit=matchesRecord({...base,screening:{decision:'EXCLUDED'}},'candidate');
    return {pass,error,excluded,audit};
  })()`,context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)),{pass:true,error:true,excluded:false,audit:true});
});
