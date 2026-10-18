/* ================================================================
   Stories Dashboard — authenticated live application
   Google Sign-In → live API, dismiss, restore, Phase 2 handoff.
   ================================================================ */

// ── compact query helper ──────────────────────────────────────────
const $=s=>document.querySelector(s);
const ESCAPES={'&':'&amp;','<':'&lt;','>':'&gt;','\\':'&bsol;','"':'&quot;'};
const esc=s=>String(s??'').replace(/[&<>\\"]/g,c=>ESCAPES[c]);

// ── shared state ──────────────────────────────────────────────────
const state={
  mode:'interactive',
  csrf:'',
  csrfExpires:0,
  googleClientId:'',
  stories:[],candidates:[],sources:[],proposals:[],
  query:'', filters:{kind:'candidate',phase:'Candidate',lane:'B',source:'',dismissed:'hide',summary:'COMPLETE',activity:'',sourceStatus:''},
  favorites:new Set(JSON.parse(localStorage.getItem('stories-favorites')||'[]')),
  recent:JSON.parse(localStorage.getItem('stories-recent')||'[]'),
  pending:{},               // item_id → true during API call
  mutationRequestIds:{},     // operation key → UUID retained across ambiguous retries
  snackbarTimer:0,
  dismissedQueue:[]          // recently dismissed for undo
};
function save(){
  localStorage.setItem('stories-favorites',JSON.stringify([...state.favorites]));
  localStorage.setItem('stories-recent',JSON.stringify(state.recent.slice(0,8)));
}

// ── API helpers ───────────────────────────────────────────────────
const API={};
API.origin=window.location.origin;
API.headers=()=>{
  const h={'Content-Type':'application/json','Origin':API.origin};
  if(state.csrf)h['X-CSRF-Token']=state.csrf;
  return h;
};
API.fetch=async(path,init)=>{
  const r=await fetch(path,init);
  if(r.status===401){API.forceLogin();throw new Error('Authentication required');}
  return r;
};
API.json=async response=>{
  let body={};
  try{body=await response.json();}catch(_){}
  if(!response.ok){
    const error=new Error(body.detail||`Request failed (${response.status})`);
    error.status=response.status;
    throw error;
  }
  return body;
};
API.forceLogin=()=>{state.csrf='';state.csrfExpires=0;renderLogin();};
API.requestId=()=>crypto.randomUUID?crypto.randomUUID():'r-'+Date.now()+'-'+Math.random().toString(36).slice(2);
API.mutationRequestId=key=>state.mutationRequestIds[key]||(state.mutationRequestIds[key]=API.requestId());
API.finishMutation=key=>{delete state.mutationRequestIds[key];};

// ── Google Sign-In ────────────────────────────────────────────────
function initGoogleSignIn(){
  if(!window.google||!state.googleClientId)return;
  google.accounts.id.initialize({
    client_id:state.googleClientId,callback:async resp=>{
      try{
        const r=await fetch('/api/v1/auth/google',{
          method:'POST',headers:{'Content-Type':'application/json','Origin':API.origin},
          body:JSON.stringify({credential:resp.credential})
        });
        if(!r.ok){showLoginError('Sign-in failed. Verify your account is authorized.');return;}
        const d=await r.json();
        state.csrf=d.csrf_token;
        state.csrfExpires=Date.now()+50*60*1000;
        hideLoginError();
        hideLoginGate();
        await loadDashboard();
      }catch(e){showLoginError(e.message||'Sign-in failed');}
    }
  });
  const btn=document.getElementById('loginButtonContainer');
  if(btn)google.accounts.id.renderButton(btn,{theme:'filled_blue',size:'large',text:'signin_with',shape:'rectangular'});
}

async function checkSession(){
  try{
    const r=await fetch('/api/v1/auth/session');
    if(r.ok){
      const d=await r.json();
      state.csrf=d.csrf_token;
      state.csrfExpires=Date.now()+50*60*1000;
      hideLoginGate();
      await loadDashboard();
      return true;
    }
  }catch(_){}
  return false;
}

async function logout(){
  try{
    await fetch('/api/v1/auth/logout',{method:'POST',headers:API.headers()});
  }catch(_){}
  state.csrf='';
  state.csrfExpires=0;
  renderLogin();
}

function showLoginError(msg){
  const el=$('#loginError');
  if(el){el.textContent=msg;el.hidden=false;}
}
function hideLoginError(){
  const el=$('#loginError');
  if(el)el.hidden=true;
}
function showLoginGate(){
  const gate=$('#loginGate');
  if(gate){gate.hidden=false;initGoogleSignIn();}
}
function hideLoginGate(){
  const gate=$('#loginGate');
  if(gate){gate.hidden=true;}
  const badge=$('#authBadge');
  if(badge){badge.innerHTML='<button class="btn-logout" id="logoutBtn">Sign out</button>';badge.hidden=false;}
}
function renderLogin(){
  showLoginGate();
  const badge=$('#authBadge');
  if(badge)badge.hidden=true;
}

// ── Dashboard loading ─────────────────────────────────────────────
async function loadDashboard(){
  try{
    const r=await API.fetch('/api/v1/dashboard');
    if(!r.ok)throw new Error('Dashboard unavailable');
    const d=await r.json();
    state.stories=d.stories||[];
    state.candidates=(d.recent_candidates||[]).filter(c=>c.status!=='IGNORED');
    state.sources=d.sources||[];
    state.proposals=d.source_proposals||[];
    const gen=d.generated_at||new Date().toISOString();
    $('#updatedAt').textContent=`Live dashboard · ${gen.slice(0,16).replace('T',' ')}`;
    renderFilters();
    render();
  }catch(e){
    $('#storyCount').textContent='Could not load dashboard';
    $('#updatedAt').textContent=e.message||'Check connection';
  }
}

// ── Render helpers (preserving original card layouts) ─────────────
const laneLabels={A:'Enforcement & regulatory actions',B:'Courts & legal opinions',C:'Public agencies & public-interest news',D:'Insurance industry & risk news'};
function storyStage(s){return s.stage||s.status||'Phase 2'}
const defaultFilters={kind:'candidate',phase:'Candidate',lane:'B',source:'',dismissed:'hide',summary:'COMPLETE',activity:'',sourceStatus:''};
function filterSelect(key,label,options){return `<label>${label}<select data-global-filter="${key}">${options.map(([value,text])=>`<option value="${esc(value)}"${state.filters[key]===value?' selected':''}>${esc(text)}</option>`).join('')}</select></label>`}
function renderFilters(){
  const names=[...new Set([...state.sources.map(s=>s.name),...state.candidates.map(s=>s.source),...state.stories.map(s=>s.source),...state.proposals.map(s=>s.name)].filter(Boolean))].sort();
  $('#filters').innerHTML=[
    filterSelect('kind','Items',[['','All items'],['candidate','Candidates'],['story','Promoted stories'],['discovery','Discovery items'],['source','Sources']]),
    filterSelect('phase','Phase',[['','All phases'],['Candidate','Candidates'],['Phase 2 pending','Phase 2 pending'],['Phase 2','Phase 2'],['Phase 3','Phase 3']]),
    filterSelect('lane','Content area',[['','All content areas'],...Object.entries(laneLabels)]),
    filterSelect('source','Source',[['','All sources'],...names.map(n=>[n,n])]),
    filterSelect('dismissed','Dismissed',[['hide','Hide dismissed'],['include','Include dismissed'],['only','Dismissed only']]),
    filterSelect('summary','Summary',[['','All summary states'],['COMPLETE','Complete'],['PENDING','Pending'],['FAILED','Failed'],['SKIPPED','Skipped']]),
    filterSelect('activity','Activity',[['','All activity'],['recent','Recently opened'],['favorites','Favorites']]),
    filterSelect('sourceStatus','Source status',[['','All source statuses'],['active','Active'],['disabled','Disabled']]),
    '<button class="text-button" id="resetFilters">Reset filters</button>'
  ].join('');
}
// Every card goes through this predicate; incompatible facets yield no matches.
function matchesRecord(record,kind){
  const f=state.filters;
  const source=kind==='source'||kind==='discovery'?record.name:record.source;
  const registry=state.sources.find(s=>s.name===source);
  const lane=record.lane||registry?.lane;
  const phase=kind==='story'?storyStage(record):kind==='candidate'?'Candidate':'';
  const id=record.id||String(record.story_id);
  const dismissed=record.status==='DISMISSED';
  if(f.kind&&f.kind!==kind)return false;
  if(f.phase&&f.phase!==phase)return false;
  if(f.lane&&f.lane!==lane)return false;
  if(f.source&&f.source!==source)return false;
  if(f.dismissed==='hide'&&dismissed||f.dismissed==='only'&&!dismissed)return false;
  if(f.summary&&(kind!=='candidate'||(record.summary_status||'PENDING')!==f.summary))return false;
  if(f.activity&&(kind!=='story'||!(f.activity==='recent'?state.recent.includes(id):state.favorites.has(id))))return false;
  if(f.sourceStatus&&(kind!=='source'||(record.active?'active':'disabled')!==f.sourceStatus))return false;
  const hay=[...Object.values(record).filter(v=>typeof v==='string'),source,laneLabels[lane]].join(' ').toLowerCase();
  return hay.includes(state.query.trim().toLowerCase());
}
function statusClass(s){const stage=storyStage(s);return stage==='Phase 3'?'phase3':stage==='Phase 2'||stage==='Phase 2 pending'?'phase2':'stub'}
const stageOrder={'Phase 3':0,'Phase 2':1,'Phase 2 pending':2};
function storyDate(s){const explicit=s.dateAdded||s.addedAt||s.createdAt||s.updated_at||s.created_at;if(explicit)return Date.parse(explicit)||0;const match=String(s.eventKey||'').match(/(20\d{2}-\d{2}-\d{2})/);return match?Date.parse(match[1]):0}
function storySort(a,b){const stageDiff=(stageOrder[storyStage(a)]??99)-(stageOrder[storyStage(b)]??99);if(stageDiff!==0)return stageDiff;return storyDate(b)-storyDate(a)}
function action(url,label,kind=''){return url?`<a class="action ${kind}" href="${esc(url)}" target="_blank" rel="noreferrer">${label} ↗</a>`:''}
function card(s){const id=s.id||String(s.story_id);const fav=state.favorites.has(id);const stage=s.stage||s.status||'Promoted';return `<article class="story-card"><div class="card-top"><span class="stage ${statusClass({...s,stage})}">${esc(stage)}</span><button class="favorite ${fav?'active':''}" data-favorite="${esc(id)}" aria-label="${fav?'Remove from':'Add to'} favorites">${fav?'★':'☆'}</button></div><h3>${esc(s.title)}</h3><p class="hook">${esc(s.hook||s.whatHappened||`Origin: ${s.source||'Unknown source'}`)}</p><div class="card-meta"><span>${esc(s.source||'Unknown source')}</span>${s.origin_item_id?`<span>· item ${esc(s.origin_item_id)}</span>`:''}</div><div class="actions">${action(s.doc||s.google_doc_url,'Doc','primary')}${action(s.telegramTopicLink||s.telegram_thread_url,'Telegram','telegram')}${action(s.phase2Folder||s.research_folder_url,'Research')}${action(s.phase3Doc,'Visual script')}${action(s.source||s.origin_url,'Source')}</div></article>`}

function candidateCard(c){
  const gist=c.gist?`<p class="candidate-gist">${esc(c.gist)}</p>`:'';
  const display=c.display_headline||c.headline;
  const summaryStatus=c.summary_status||'PENDING';
  const itemStatus=c.status||'NEW';
  let actions='';
  if(itemStatus==='DISMISSED'){
    actions=`<span class="candidate-dismissed-label">Dismissed</span>`;
    if(c.active_dismissal){
      actions+=`<button class="btn-restore" data-item="${esc(c.item_id)}" data-feedback="${esc(c.active_dismissal.feedback_id)}"${state.pending[c.item_id]?' disabled':''}>Restore</button>`;
    }
  }else{
    actions=`<button class="btn-dismiss-interactive" data-item="${esc(c.item_id)}" data-headline="${esc(display)}"${state.pending[c.item_id]?' disabled':''}>Dismiss</button>`;
    actions+=`${action(c.source_link,'Source','primary')}`;
    if(c.can_promote_to_phase2!==false)actions+=`<button class="action telegram phase2-action btn-phase2" data-phase2-item="${esc(c.item_id)}"${state.pending[c.item_id]?' disabled':''}>${state.pending[c.item_id]?'Starting Phase 2…':'Promote to Phase 2'}</button>`;
  }
  return `<article class="candidate-card"><div class="candidate-top"><span class="candidate-summary-status ${summaryStatus.toLowerCase()}">${esc(summaryStatus)}</span><span class="candidate-lane">${esc(laneLabels[c.lane]||'Other content')}</span></div><h3 class="candidate-display-headline">${esc(display)}</h3><details class="candidate-original"><summary>Original headline</summary><p>${esc(c.headline)}</p></details><p class="candidate-meta"><strong>${esc(c.source)}</strong> · ${esc(c.date||'Undated')} · item ${esc(c.item_id)}</p>${gist}<div class="actions">${actions}</div></article>`;
}

function render(){
  const stories=state.stories.filter(s=>matchesRecord(s,'story')).sort(storySort);
  const candidates=state.candidates.filter(c=>matchesRecord(c,'candidate')).sort((a,b)=>String(b.date).localeCompare(String(a.date)));
  const proposals=state.proposals.filter(p=>matchesRecord(p,'discovery'));
  const sources=state.sources.filter(s=>matchesRecord(s,'source'));
  $('#storyGrid').innerHTML=stories.map(card).join('');
  $('#resultsCount').textContent=`${stories.length} stories`;
  $('#resultsHeading').textContent=state.filters.phase||'Promoted stories';
  $('#promotedSection').hidden=!stories.length;
  $('#candidateGrid').innerHTML=candidates.map(candidateCard).join('');
  $('#candidateCount').textContent=`${candidates.length} candidates`;
  $('#candidateSection').hidden=!candidates.length;
  renderSources({source_proposals:proposals});
  $('#sourceReviewSection').hidden=!proposals.length;
  renderSourceManagement(sources);
  $('#sourceManagementSection').hidden=!sources.length;
  const total=stories.length+candidates.length+proposals.length+sources.length;
  $('#storyCount').textContent=`${total} matching items`;
  $('#visibleCount').textContent=`${candidates.length} candidates · ${stories.length} stories · ${proposals.length} discovery items · ${sources.length} sources`;
  $('#emptyState').hidden=total>0;
}

function renderSources(d){const proposals=d.source_proposals||[];const c=d.coverage||{};$('#sourceProposalCount').textContent=`${proposals.length} proposals · approval remains CLI-only`;const laneCounts=(c.active_by_lane||[]).map(x=>`<span><b>Lane ${esc(x.lane)}</b> ${x.count} active</span>`).join('');$('#coverageGrid').innerHTML=laneCounts||'<span>No coverage statistics yet</span>';$('#sourceProposalGrid').innerHTML=proposals.map(p=>`<article class="source-proposal"><h3>${esc(p.name)}</h3><p>${esc(p.jurisdiction)} · Lane ${esc(p.lane)} · ${esc(p.publication_type||p.collector_type||'publication source')}</p><p class="muted">${esc(p.validation_status)} · ${esc(p.status)}</p>${p.source_url?action(p.source_url,'Open source','primary'):''}<p>${esc(p.reason)}</p></article>`).join('')}

function renderSourceManagement(shown){$('#sourceManagementCount').textContent=`${shown.length} of ${state.sources.length} sources`;$('#sourceManagementGrid').innerHTML=shown.map(s=>`<article class="source-management-card"><div class="source-management-top"><span class="source-status ${s.active?'active':'disabled'}">${s.active?'Active':'Disabled'}</span><span class="muted">ID ${esc(s.source_id)}</span></div><h3>${esc(s.name)}</h3><p>${esc(s.jurisdiction)} · ${esc(laneLabels[s.lane]||'Other content')} · ${esc(s.collector_type||s.source_type)}</p><p><a href="${esc(s.publication_url||s.source_url)}" target="_blank" rel="noreferrer">${esc(s.publication_url||s.source_url)} ↗</a></p><p>Last successful poll: ${esc(s.last_success_at||'Never')}</p><p>Publications in last 30 days: <strong>${esc(s.publications_last_30_days)}</strong></p>${s.last_error?`<p class="source-error">Latest error: ${esc(s.last_error)}</p>`:''}<div class="actions"><a class="action telegram" href="${esc(s.telegram_action_url)}" target="_blank" rel="noreferrer">${esc(s.telegram_action_label)} ↗</a></div></article>`).join('')||'<p class="muted">No matching sources.</p>'}

// ── Dismiss panel ─────────────────────────────────────────────────
let dismissTarget=null;
function openDismissPanel(itemId,headline){
  dismissTarget={item_id:parseInt(itemId,10),headline};
  $('#dismissItemLabel').textContent=`#${dismissTarget.item_id} — ${dismissTarget.headline.slice(0,120)}`;
  const reasonsEl=$('#dismissReasons');
  reasonsEl.innerHTML=['not_relevant','duplicate','too_old','weak_source','other'].map(r=>`<button class="reason-chip" data-reason="${r}">${r.replace(/_/g,' ')}</button>`).join('');
  $('#dismissConfirm').disabled=true;
  $('#dismissConfirm').dataset.reason='';
  $('#dismissNote').value='';
  $('#dismissPanel').hidden=false;
}
function closeDismissPanel(){
  dismissTarget=null;
  $('#dismissPanel').hidden=true;
}
async function submitDismiss(){
  if(!dismissTarget)return;
  const reason=$('#dismissConfirm').dataset.reason;
  if(!reason)return;
  const note=$('#dismissNote').value.slice(0,1000);
  const itemId=dismissTarget.item_id;
  const mutationKey=`dismiss:${itemId}`;
  const requestId=API.mutationRequestId(mutationKey);
  state.pending[itemId]=true;
  closeDismissPanel();
  try{
    const r=await API.fetch(`/api/v1/items/${itemId}/dismiss`,{
      method:'POST',headers:API.headers(),
      body:JSON.stringify({reason_code:reason,note,request_id:requestId})
    });
    const d=await API.json(r);
    API.finishMutation(mutationKey);
    const fb=d.feedback||d;
    state.dismissedQueue.push({item_id:itemId,feedback_id:fb.feedback_id});
    showSnackbar(`Dismissed. <button class="snackbar-undo" id="snackbarUndo">Undo</button>`,8000);
    await refreshAfterMutation();
  }catch(e){
    if(e.status)API.finishMutation(mutationKey);
    showSnackbar(`Error: ${e.message}`,4000);
  }finally{delete state.pending[itemId];render();}
}
async function undoLastDismiss(){
  const entry=state.dismissedQueue.pop();
  if(!entry){showSnackbar('Nothing to undo',3000);return;}
  const mutationKey=`restore:${entry.item_id}:${entry.feedback_id}`;
  const requestId=API.mutationRequestId(mutationKey);
  state.pending[entry.item_id]=true;
  try{
    const r=await API.fetch(`/api/v1/items/${entry.item_id}/restore`,{
      method:'POST',headers:API.headers(),
      body:JSON.stringify({dismissal_feedback_id:entry.feedback_id,request_id:requestId})
    });
    await API.json(r);
    API.finishMutation(mutationKey);
    showSnackbar('Restored',3000);
    await refreshAfterMutation();
  }catch(e){
    if(e.status)API.finishMutation(mutationKey);
    showSnackbar(`Restore failed: ${e.message}`,4000);
    state.dismissedQueue.push(entry);
  }finally{delete state.pending[entry.item_id];render();}
}
async function restoreItem(itemId,feedbackId){
  const parsedFeedbackId=parseInt(feedbackId,10);
  const mutationKey=`restore:${itemId}:${parsedFeedbackId}`;
  const requestId=API.mutationRequestId(mutationKey);
  state.pending[itemId]=true;
  render();
  try{
    const r=await API.fetch(`/api/v1/items/${itemId}/restore`,{
      method:'POST',headers:API.headers(),
      body:JSON.stringify({dismissal_feedback_id:parsedFeedbackId,request_id:requestId})
    });
    await API.json(r);
    API.finishMutation(mutationKey);
    showSnackbar('Restored',3000);
    await refreshAfterMutation();
  }catch(e){
    if(e.status)API.finishMutation(mutationKey);
    showSnackbar(`Restore failed: ${e.message}`,4000);
  }finally{delete state.pending[itemId];render();}
}
async function requestPhase2(itemId){
  const parsedItemId=parseInt(itemId,10);
  state.pending[parsedItemId]=true;
  render();
  try{
    const r=await API.fetch(`/api/v1/items/${parsedItemId}/phase2`,{
      method:'POST',headers:API.headers()
    });
    const d=await API.json(r);
    await refreshAfterMutation();
    showSnackbar('Moved to Promoted as Phase 2 pending',3000);
    if(d.telegram_handoff_url)window.location.assign(d.telegram_handoff_url);
  }catch(e){
    showSnackbar(`Promotion failed: ${e.message}`,4000);
  }finally{delete state.pending[parsedItemId];render();}
}
async function refreshAfterMutation(){
  try{await loadDashboard();}catch(_){}
}
function showSnackbar(html,ms){
  const el=$('#snackbar');
  el.innerHTML=html;el.hidden=false;
  clearTimeout(state.snackbarTimer);
  if(ms)state.snackbarTimer=setTimeout(()=>{el.hidden=true;},ms);
}

// ── Event delegation ─────────────────────────────────────────────────
document.addEventListener('click',e=>{
  if(e.target.closest('#resetFilters')){state.filters={...defaultFilters};state.query='';$('#searchInput').value='';$('#clearSearch').style.display='none';renderFilters();render();return}
  const fav=e.target.closest('[data-favorite]');
  if(fav){const id=fav.dataset.favorite;state.favorites.has(id)?state.favorites.delete(id):state.favorites.add(id);save();render();return}
  const phase2Btn=e.target.closest('[data-phase2-item]');
  if(phase2Btn){requestPhase2(phase2Btn.dataset.phase2Item);return}
  const link=e.target.closest('a.action');
  if(link){const c=link.closest('.story-card');const s=state.stories.find(x=>c&&c.querySelector('[data-favorite]')?.dataset.favorite===(x.id||String(x.story_id)));if(s){state.recent=[s.id||String(s.story_id),...state.recent.filter(x=>x!==(s.id||String(s.story_id)))];save()}}
  // interactive dismiss button
  const dimBtn=e.target.closest('.btn-dismiss-interactive');
  if(dimBtn){openDismissPanel(dimBtn.dataset.item,dimBtn.dataset.headline);return}
  // restore button
  const rstBtn=e.target.closest('.btn-restore');
  if(rstBtn){restoreItem(rstBtn.dataset.item,rstBtn.dataset.feedback);return}
  // dismiss panel reason chips
  const chip=e.target.closest('.reason-chip');
  if(chip){
    const panel=$('#dismissReasons');
    panel.querySelectorAll('.reason-chip').forEach(b=>b.classList.remove('active'));
    chip.classList.add('active');
    $('#dismissConfirm').disabled=false;
    $('#dismissConfirm').dataset.reason=chip.dataset.reason;
    return;
  }
  // dismiss panel actions
  if(e.target.id==='dismissCancel'){closeDismissPanel();return}
  if(e.target.id==='dismissConfirm'){submitDismiss();return}
  // snackbar undo
  if(e.target.closest('#snackbarUndo')){undoLastDismiss();return}
  // logout
  if(e.target.closest('#logoutBtn')){logout();return}
});

// bind existing controls
$('#searchInput').addEventListener('input',e=>{state.query=e.target.value;$('#clearSearch').style.display=state.query?'block':'none';render()});
$('#clearSearch').addEventListener('click',()=>{$('#searchInput').value='';state.query='';$('#clearSearch').style.display='none';render()});
$('#themeToggle').addEventListener('click',()=>{const dark=document.documentElement.dataset.theme==='dark';document.documentElement.dataset.theme=dark?'':'dark';localStorage.setItem('stories-theme',dark?'light':'dark');$('#themeToggle').textContent=dark?'☼':'☾'});
document.addEventListener('change',e=>{const key=e.target.dataset.globalFilter;if(key&&Object.hasOwn(defaultFilters,key)){state.filters[key]=e.target.value;render();}});

// ── Init ──────────────────────────────────────────────────────────────
async function init(){
  if(localStorage.getItem('stories-theme')==='dark'){
    document.documentElement.dataset.theme='dark';
    $('#themeToggle').textContent='☾';
  }
  if('serviceWorker'in navigator)navigator.serviceWorker.register('./sw.js').catch(()=>{});

  let configResponse;
  try{
    configResponse=await fetch('/api/v1/config',{headers:{Origin:API.origin}});
  }catch(_){
    state.mode='unavailable';
    $('#storyCount').textContent='Interactive dashboard unavailable';
    $('#updatedAt').textContent='Could not reach the interactive service. Try again when connected.';
    return;
  }

  if(configResponse.status===404){
    state.mode='unavailable';
    $('#storyCount').textContent='Interactive dashboard unavailable';
    $('#updatedAt').textContent='The interactive service is not available on this deployment.';
    return;
  }
  if(!configResponse.ok){
    state.mode='unavailable';
    $('#storyCount').textContent='Interactive dashboard unavailable';
    $('#updatedAt').textContent=`Interactive service returned ${configResponse.status}. Try again later.`;
    return;
  }

  try{
    const config=await configResponse.json();
    if(!config.google_client_id)throw new Error('Missing Google client configuration');
    state.mode='interactive';
    state.googleClientId=config.google_client_id;
  }catch(_){
    state.mode='unavailable';
    $('#storyCount').textContent='Interactive dashboard unavailable';
    $('#updatedAt').textContent='Interactive service configuration is invalid.';
    return;
  }

  const loggedIn=await checkSession();
  if(!loggedIn)showLoginGate();
}

init();

// ── Inline tests — run in console: appTests() ──────────────────
window.appTests=function(){
  const results=[];
  function ok(cond,msg){results.push(cond?'PASS '+msg:'FAIL '+msg);}
  ok(state.mode==='interactive'||state.mode==='unavailable','only the interactive app is supported');
  ok(API.requestId().length>0,'requestId generates string');
  ok(esc('<scrpt>').indexOf('<')===-1,'esc escapes angle');
  ok(typeof dismissTarget==='object'||dismissTarget===null,'dismissTarget is object or null');
  console.log(results.join('\n'));
  return results;
};