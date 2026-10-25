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
  const documentListeners = {};
  const document = {
    documentElement: { dataset: {} },
    querySelector: selector => getElement(selector),
    getElementById: id => getElement(`#${id}`),
    addEventListener(event,callback) { documentListeners[event]=callback; },
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
  return { context, getElement, fetchCalls, documentListeners };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function candidateSetup(context) {
  vm.runInContext(`
    state.candidates=[{item_id:7,headline:'First candidate',status:'NEW',summary_status:'COMPLETE'},
      {item_id:8,headline:'Second candidate',status:'NEW',summary_status:'COMPLETE'}];
    showSnackbar=message=>{document.querySelector('#snackbar').innerHTML=message;};
    refreshAfterMutation=async()=>{throw new Error('Must not reload the dashboard');};
    render();
  `,context);
}
function dismissalResponse(item, feedback) {
  return {ok:true,status:201,json:async()=>({item:{item_id:item,status:'DISMISSED'},feedback:{feedback_id:feedback},active_dismissal:{feedback_id:feedback}})};
}

test('one click hides immediately, saves not_relevant, prevents duplicate clicks, and supports Undo', async () => {
  const {context,getElement,documentListeners}=loadApp();
  candidateSetup(context);
  const request=deferred();
  const calls=[];
  context.saveDismissal=(_path,init)=>{calls.push(JSON.parse(init.body));return request.promise;};
  vm.runInContext('API.fetch=saveDismissal;const actualDismiss=dismissItem;dismissItem=id=>(window.dismissal=actualDismiss(id));',context);
  const button={dataset:{item:'7'}};
  documentListeners.click({target:{closest:selector=>selector==='.btn-dismiss-interactive'?button:null}});
  const pendingDismissal=context.window.dismissal;
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.match(getElement('#candidateGrid').innerHTML,/Second candidate/);
  assert.match(getElement('#visibleCount').textContent,/1 shown/);
  assert.doesNotMatch(getElement('#snackbar').innerHTML,/snackbarUndo/);
  await vm.runInContext('dismissItem(7)',context);
  assert.equal(calls.length,1);
  assert.equal(calls[0].reason_code,'not_relevant');
  assert.equal(calls[0].note,'');
  request.resolve(dismissalResponse(7,31));
  await pendingDismissal;
  assert.match(getElement('#snackbar').innerHTML,/snackbarUndo.*data-feedback="31"/);
  vm.runInContext("state.filters.dismissed='only';render()",context);
  assert.match(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.match(getElement('#candidateGrid').innerHTML,/data-feedback="31"/);
  const restoreCalls=[];
  context.restoreDismissal=async(_path,init)=>{restoreCalls.push(JSON.parse(init.body));return {ok:true,status:201,json:async()=>({item:{item_id:7,status:'NEW'},active_dismissal:null})};};
  vm.runInContext("state.filters.dismissed='hide';API.fetch=restoreDismissal",context);
  await vm.runInContext('undoLastDismiss(31)',context);
  assert.equal(restoreCalls[0].dismissal_feedback_id,31);
  assert.match(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.equal(vm.runInContext('state.dismissedQueue.length',context),0);
});

test('failed dismissal restores the card and retains its idempotency key for ambiguous retries', async () => {
  const {context,getElement}=loadApp();candidateSetup(context);
  const bodies=[];
  context.failDismissal=async(_path,init)=>{bodies.push(JSON.parse(init.body));throw new Error('Network down');};
  vm.runInContext('API.fetch=failDismissal',context);
  await vm.runInContext('dismissItem(7)',context);
  await vm.runInContext('dismissItem(7)',context);
  assert.match(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.match(getElement('#snackbar').innerHTML,/item restored.*Network down/);
  assert.equal(bodies.length,2);
  assert.equal(bodies[0].request_id,bodies[1].request_id);
  assert.equal(vm.runInContext('state.dismissedQueue.length',context),0);
  assert.equal(vm.runInContext('!!state.pending[7]',context),false);
});

test('simultaneous dismissals roll back only the failed item without changing current filters', async () => {
  const {context,getElement}=loadApp();candidateSetup(context);
  const first=deferred(),second=deferred();
  context.dismissRequests=url=>url.includes('/7/')?first.promise:second.promise;
  vm.runInContext('API.fetch=dismissRequests;window.first=dismissItem(7);window.second=dismissItem(8);state.query="First";',context);
  second.resolve(dismissalResponse(8,32));await context.window.second;
  first.resolve({ok:false,status:503,json:async()=>({detail:'Database unavailable'})});await context.window.first;
  assert.match(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/Second candidate/);
  assert.equal(vm.runInContext('state.candidates[1].status',context),'DISMISSED');
  assert.equal(vm.runInContext('state.query',context),'First');
  assert.equal(vm.runInContext('state.dismissedQueue[0].item_id',context),8);
  assert.equal(vm.runInContext("!!state.mutationRequestIds['dismiss:7']",context),true);
});

test('background dashboard responses cannot undo pending or confirmed local changes', async () => {
  const {context,getElement}=loadApp();candidateSetup(context);
  const save=deferred(),during=deferred(),beforeUndo=deferred();
  context.save=()=>save.promise;
  vm.runInContext('API.fetch=save;window.dismissal=dismissItem(7)',context);
  context.during=()=>during.promise;
  vm.runInContext('API.fetch=during;window.refresh=loadDashboard()',context);
  during.resolve({ok:true,json:async()=>({recent_candidates:[{item_id:7,headline:'First candidate',status:'NEW',summary_status:'COMPLETE'}]})});
  await context.window.refresh;
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/First candidate/);
  save.resolve(dismissalResponse(7,31));await context.window.dismissal;
  context.beforeUndo=()=>beforeUndo.promise;
  vm.runInContext('API.fetch=beforeUndo;window.oldRefresh=loadDashboard()',context);
  vm.runInContext("API.fetch=async()=>({ok:true,json:async()=>({item:{status:'NEW'}})})",context);
  await vm.runInContext('undoLastDismiss(31)',context);
  beforeUndo.resolve({ok:true,json:async()=>({recent_candidates:[{item_id:7,headline:'First candidate',status:'DISMISSED',summary_status:'COMPLETE'}]})});
  await context.window.oldRefresh;
  assert.match(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.equal(vm.runInContext('state.candidates[0].status',context),'NEW');
  assert.equal(vm.runInContext('state.candidates[0].can_promote_to_phase2',context),true);
});

test('an older dashboard request cannot resurrect a dismissal after a fresh request confirms it', async () => {
  const {context,getElement}=loadApp();candidateSetup(context);
  const stale=deferred();context.stale=()=>stale.promise;
  vm.runInContext('API.fetch=stale;window.stale=loadDashboard()',context);
  context.success=async()=>dismissalResponse(7,31);
  vm.runInContext('API.fetch=success',context);await vm.runInContext('dismissItem(7)',context);
  vm.runInContext("API.fetch=async()=>({ok:true,json:async()=>({recent_candidates:[{item_id:7,headline:'First candidate',status:'DISMISSED',summary_status:'COMPLETE'}]})})",context);
  await vm.runInContext('loadDashboard()',context);
  stale.resolve({ok:true,json:async()=>({recent_candidates:[{item_id:7,headline:'First candidate',status:'NEW',summary_status:'COMPLETE'}]})});
  await context.window.stale;
  assert.doesNotMatch(getElement('#candidateGrid').innerHTML,/First candidate/);
  assert.equal(vm.runInContext('state.candidates[0].status',context),'DISMISSED');
});

test('Undo restores its displayed item even when another dismissal has completed', async () => {
  const {context}=loadApp();candidateSetup(context);
  context.dismissSuccess=async url=>dismissalResponse(url.includes('/7/')?7:8,url.includes('/7/')?31:32);
  vm.runInContext('API.fetch=dismissSuccess',context);
  await vm.runInContext('dismissItem(7)',context);
  await vm.runInContext('dismissItem(8)',context);
  const restore=deferred(),calls=[];
  context.restoreTarget=(url,init)=>{calls.push({url,body:JSON.parse(init.body)});return restore.promise;};
  vm.runInContext('API.fetch=restoreTarget;window.undo=undoLastDismiss(31)',context);
  await vm.runInContext('undoLastDismiss(31)',context);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/v1/items/7/restore');
  assert.equal(calls[0].body.dismissal_feedback_id,31);
  restore.resolve({ok:true,json:async()=>({item:{status:'NEW'}})});
  await context.window.undo;
  assert.equal(vm.runInContext('state.candidates[0].status',context),'NEW');
  assert.equal(vm.runInContext('state.candidates[1].status',context),'DISMISSED');
  assert.equal(vm.runInContext('state.dismissedQueue[0].feedback_id',context),32);
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


test('promotion dialog waits for guidance and retains text on failure', async () => {
  const {context,getElement,documentListeners}=loadApp();
  candidateSetup(context);
  const dialog=getElement('#promotionDialog');
  dialog.showModal=()=>{dialog.open=true;};
  dialog.close=()=>{dialog.open=false;};
  getElement('#researchBrief').focus=()=>{};
  let calls=0;
  context.promote=async(_url,init)=>{
    calls++;
    assert.equal(JSON.parse(init.body).research_brief,'Follow the refunds');
    return {ok:false,status:503,json:async()=>({detail:'Try again'})};
  };
  vm.runInContext('API.fetch=promote',context);
  const button={dataset:{phase2Item:'7'}};
  documentListeners.click({target:{closest:s=>s==='[data-phase2-item]'?button:null}});
  assert.equal(dialog.open,true);
  assert.equal(calls,0);
  getElement('#researchBrief').value='Follow the refunds';
  await vm.runInContext("requestPhase2(7,document.querySelector('#researchBrief').value)",context);
  assert.equal(calls,1);
  assert.equal(dialog.open,true);
  assert.equal(getElement('#researchBrief').value,'Follow the refunds');
  assert.match(getElement('#promotionError').textContent,/Try again/);
});

test('Phase 2 deletion confirms scope, prevents duplicates, and survives stale dashboard responses', async () => {
  const {context,getElement}=loadApp();
  await new Promise(resolve=>setImmediate(resolve));
  vm.runInContext(`
    state.stories=[{story_id:7,title:'Remove me',status:'Phase 2'}];
    window.confirm=message=>{window.deleteMessage=message;return true;};
    window.requests=[];
    API.fetch=async(path,init)=>{window.requests.push({path,init});return {ok:true,json:async()=>({deleted:true})};};
    window.firstDelete=deletePhase2Story('7');
    window.secondDelete=deletePhase2Story('7');
  `,context);
  await vm.runInContext('Promise.all([window.firstDelete,window.secondDelete])',context);
  assert.equal(vm.runInContext('window.requests.length',context),1);
  assert.equal(vm.runInContext('window.requests[0].init.method',context),'DELETE');
  assert.match(vm.runInContext('window.deleteMessage',context),/Google Docs.*kept/);
  assert.equal(vm.runInContext('state.stories.length',context),0);
  await vm.runInContext(`API.fetch=async()=>({ok:true,json:async()=>({stories:[{story_id:7,status:'Phase 2'}]})});loadDashboard()`,context);
  assert.equal(vm.runInContext('state.stories.length',context),0);
});

test('Phase 2 delete cancellation and server errors preserve the card', async () => {
  const {context}=loadApp();
  await new Promise(resolve=>setImmediate(resolve));
  await vm.runInContext(`
    state.stories=[{story_id:7,title:'Keep me',status:'Phase 2'}];
    window.confirm=()=>false;
    API.fetch=()=>{throw Error('should not send');};
    deletePhase2Story('7');
  `,context);
  assert.equal(vm.runInContext('state.stories.length',context),1);
  await vm.runInContext(`window.confirm=()=>true;API.fetch=async()=>({ok:false,status:500,json:async()=>({detail:'Server unavailable'})});deletePhase2Story('7')`,context);
  assert.equal(vm.runInContext('state.stories.length',context),1);
  assert.equal(vm.runInContext('state.deletingStories.size',context),0);
  assert.match(vm.runInContext("card(state.stories[0])",context),/data-story-delete="7"/);
  assert.doesNotMatch(vm.runInContext("card({story_id:8,status:'Phase 3'})",context),/data-story-delete/);
});
