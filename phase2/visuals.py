"""Build live narrative visuals without rewriting producer instructions.

The durable ledger is keyed by a visible stable ID and exact instruction hash.
Only application-owned receipt text is edited in Google Docs.
"""
from __future__ import annotations

import copy
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from jsonschema import Draft202012Validator

from phase2.doc_policy import NARRATIVE_HEADERS, SPECS, words
from phase2.google_docs import GoogleDocs, units
from phase2.registry import canonical, digest
from phase2.store import Conflict

RECEIPT = re.compile(r'\n?\[Visual asset: ([a-f0-9]{32})\]\n.*?\[/Visual asset\]', re.S)
PLAN_SCHEMA = {
    'type':'object', 'additionalProperties':False,
    'properties':{
        'kind':{'enum':['generate','retrieve','skip']},
        'prompt':{'type':'string','maxLength':12000},
        'url':{'type':'string','maxLength':4000},
        'aspect_ratio':{'enum':['landscape','portrait','square']},
        'reason':{'type':'string','minLength':1,'maxLength':1000}},
    'required':['kind','prompt','url','reason','aspect_ratio']}
PLAN_PROMPT = '''Prepare exactly one visual asset from the producer's instructions.
Return kind=generate only for an original illustration, explanatory graphic or
explicitly requested illustrative reconstruction. Put the complete image prompt
in prompt, preserving exact required text and labeling reconstructions illustrative.
Never generate substitute evidence, real incident footage, authentic photographs,
court records, screenshots of sources, or fabricated factual charts. For an
existing source image/PDF return kind=retrieve with a verified direct public URL.
Use search and source extraction only as needed. Availability does not grant rights.
Choose aspect_ratio from the instructions; default to landscape for video visuals.
If instructions need reference-image editing, video, exact data not provided,
restricted assets, or are ambiguous, return kind=skip and explain the missing input.
Never invent retrieval success. Empty unused prompt/url fields. No narration changes.'''


def rows(document):
    _,content,_=GoogleDocs.body(document)
    tables=[p['table'] for p in content if 'table' in p]
    if len(tables)!=1:
        raise Conflict('Visual builds require exactly one narrative table')
    table=tables[0]['tableRows']
    values=[[GoogleDocs.text(c.get('content',[])).removesuffix('\n') for c in r['tableCells']] for r in table]
    if not values or values[0]!=NARRATIVE_HEADERS or any(len(r)!=4 for r in values):
        raise Conflict('Visual builds require the four-column narrative table')
    if not 1<=len(values)-1<=20:
        raise Conflict('Visual builds require 1–20 narrative rows')
    if 'suggested' in canonical(content).lower():
        raise Conflict('Resolve narrative suggestions before building visuals')
    return table[1:],values[1:]


def instructions(row):
    # The talk track and application-owned receipts do not invalidate an image.
    return dict(visual=row[0],production_notes=row[2],collateral=RECEIPT.sub('',row[3]))


def ledger(store,wid):
    with store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM visual_builds WHERE workspace_id=? ORDER BY created_at',(wid,))]


def identify(values,saved):
    result=[]; seen=set()
    for row in values:
        spec=instructions(row); hashed=digest(spec)
        markers=RECEIPT.findall(row[3])
        if len(markers)>1:
            raise Conflict('Multiple visual IDs in one row; retain exactly one receipt')
        visual_id=markers[0] if markers else None
        if not visual_id:
            matches={r['visual_id'] for r in saved if r['instruction_hash']==hashed}
            if len(matches)>1:
                raise Conflict('Ambiguous visual identity; restore the row receipt')
            visual_id=next(iter(matches),uuid.uuid4().hex)
        if visual_id in seen:
            raise Conflict('Duplicate visual identity; give copied rows their own instructions and remove the copied receipt')
        seen.add(visual_id)
        result.append(dict(visual_id=visual_id,instruction_hash=hashed,instructions=spec))
    return result


def save(store,job,item,**fields):
    with store.connect(write=True) as db:
        store._owned(db,job)
        now=time.time()
        db.execute('INSERT OR IGNORE INTO visual_builds(workspace_id,visual_id,instruction_hash,instructions,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                   (job['workspace_id'],item['visual_id'],item['instruction_hash'],canonical(item['instructions']),now,now))
        if fields:
            allowed={'plan','file_id','state','error','output_path','mime_type'}
            if not fields.keys()<=allowed: raise ValueError('Unknown visual checkpoint field')
            db.execute('UPDATE visual_builds SET '+','.join(k+'=?' for k in fields)+',updated_at=? WHERE workspace_id=? AND visual_id=? AND instruction_hash=?',
                       (*fields.values(),now,job['workspace_id'],item['visual_id'],item['instruction_hash']))


def receipt(item,state,file_id=None,reason=''):
    text='\n[Visual asset: '+item['visual_id']+']\n'
    if file_id:
        text+='In Drive: https://drive.google.com/file/d/'+file_id+'/view\n'
    text+=state+('\n'+reason if reason else '')+'\n[/Visual asset]'
    return text


def update_receipts(google,artifact,document,items,receipts):
    """Append/replace only receipts; revision guards preserve all other content."""
    table,values=rows(document)
    _,content,tab_id=google.body(document)
    requests=[]
    for r,item,new_receipt in reversed(list(zip(table,items,receipts))):
        cell=r['tableCells'][3]
        cell_content=cell.get('content',[])
        if any('paragraph' not in p or any(set(e)-{'startIndex','endIndex','textRun'} for e in p['paragraph'].get('elements',[])) for p in cell_content):
            raise Conflict('Collateral contains non-text elements; preserve these and reconcile before building')
        value=google.text(cell_content).removesuffix('\n')
        matches=list(RECEIPT.finditer(value))
        if len(matches)>1: raise Conflict('Ambiguous visual receipt')
        start=cell_content[0]['startIndex']
        match=matches[0] if matches else None
        begin=start+units(value[:match.start()] if match else value)
        end=start+units(value[:match.end()] if match else value)
        if match and match.group()==new_receipt: continue
        location=dict(index=begin,**({'tabId':tab_id} if tab_id else {}))
        span=dict(startIndex=begin,endIndex=end,**({'tabId':tab_id} if tab_id else {}))
        if end>begin: requests.append({'deleteContentRange':{'range':span}})
        requests.append({'insertText':{'location':location,'text':new_receipt}})
        for link in re.finditer(r'https?://\S+',new_receipt):
            linked=dict(span,startIndex=begin+units(new_receipt[:link.start()]),endIndex=begin+units(new_receipt[:link.end()]))
            requests.append({'updateTextStyle':{'range':linked,'textStyle':{'link':{'url':link.group()}},'fields':'link'}})
    if not requests: return document
    google.docs.documents().batchUpdate(documentId=artifact['doc_id'],body={'writeControl':{'requiredRevisionId':document['revisionId']},'requests':requests}).execute()
    verified=google.read(artifact['doc_id']); _,after=rows(verified)
    # Receipts may be followed by producer text; compare instruction content too.
    if len(after)!=len(values) or any(instructions(a)!=instructions(b) or a[:3]!=b[:3] or RECEIPT.findall(a[3])!=[i['visual_id']] or new not in a[3] for a,b,i,new in zip(after,values,items,receipts)):
        raise Conflict('Visual receipt readback mismatch; preserve existing files and reconcile the Doc')
    return verified


def write_receipts(worker,job,artifact,document,items,receipts):
    google=worker.google
    _,values=rows(document)
    projected=[RECEIPT.sub('',row[3])+new for row,new in zip(values,receipts)]
    document_words=words(google.text(google.body(document)[1]))+sum(words(new)-words(row[3]) for row,new in zip(values,projected))
    excess=document_words>SPECS['narrative'][0]
    receipts=[new.replace('\n[/Visual asset]','\nException requires human review: Original instructions and asset provenance retained in full.\n[/Visual asset]') if excess or words(text)>150 else new for new,text in zip(receipts,projected)]
    if all((match:=RECEIPT.search(row[3])) and match.group()==new for row,new in zip(values,receipts)):
        return document
    tab,content,_=google.body(document)
    names=[n for g in tab.get('namedRanges',document.get('namedRanges',{})).values() for n in g.get('namedRanges',[]) if n.get('name','').startswith('stories-managed-')]
    spans=[r for n in names for r in n.get('ranges',[])]
    matched=len(spans)==1 and bool(artifact['baseline']) and google.fingerprint(content,spans[0]['startIndex'],spans[0]['endIndex'])==artifact['baseline']
    # Receipt ranges can carry comments too. Preserve them under the same rule
    # as narrative revisions, rather than silently deleting their anchors.
    comments=google.drive.comments().list(fileId=artifact['doc_id'],fields='nextPageToken,comments(id,resolved,deleted)',includeDeleted=False,pageSize=100).execute()
    if comments.get('nextPageToken') or any(not c.get('resolved') and not c.get('deleted') for c in comments.get('comments',[])):
        raise Conflict('Resolve narrative comments before updating visual receipts; annotations were preserved')
    verified=update_receipts(google,artifact,document,items,receipts)
    if matched:
        after_tab,after_content,_=google.body(verified)
        after_names=[n for g in after_tab.get('namedRanges',verified.get('namedRanges',{})).values() for n in g.get('namedRanges',[]) if n.get('name','').startswith('stories-managed-')]
        after_spans=[r for n in after_names for r in n.get('ranges',[])]
        if len(after_spans)!=1: raise Conflict('Managed narrative boundaries changed during visual receipts')
        span=after_spans[0]
        baseline=google.fingerprint(after_content,span['startIndex'],span['endIndex'])
        worker.store.rendered(job,'narrative',baseline,verified['revisionId'])
        artifact.update(baseline=baseline,revision_id=verified['revisionId'])
    return verified


def build_visuals(worker,job):
    store,google=worker.store,worker.google
    artifact=store.artifacts(job['workspace_id'])['narrative']
    with worker.render_lock(job):
        document=google.read(artifact['doc_id'])
        _,values=rows(document)
        items=identify(values,ledger(store,job['workspace_id']))
        # Save exact input cells before the first agent or external side effect.
        inputs=store.save_inputs(job,dict(doc_id=artifact['doc_id'],revision=document['revisionId'],items=items))
        if inputs['items']!=items:
            raise Conflict('Visual instructions changed during the build; run /story visuals build again')
        saved=ledger(store,job['workspace_id'])
        initial=[]
        for item,row in zip(items,values):
            previous=RECEIPT.search(row[3])
            ready=any(r['visual_id']==item['visual_id'] and r['instruction_hash']==item['instruction_hash'] and r['file_id'] for r in saved)
            initial.append(previous.group() if previous and ready else receipt(item,'Build queued; instructions retained above.'))
        document=write_receipts(worker,job,artifact,document,items,initial)
    from phase2.visual_renderer import Renderer
    renderer=getattr(worker,'visual_renderer',None) or Renderer(worker.research.python,worker.research.source,worker.lock_dir/'visual-assets',host_model=job['manifest']['engine']['model'] if job['manifest']['engine']['provider']=='openai-codex' else '')
    folder=json.loads(store.workspace(job['workspace_id'])['resources'])['images']
    narrative=json.loads(store.version(artifact['current_version'])['output'])
    source_registry=narrative.get('sources',[])
    source_context_hash=digest(source_registry)

    def build(item):
        marker=digest([job['workspace_id'],item['visual_id'],item['instruction_hash']])
        saved=next((r for r in ledger(store,job['workspace_id']) if r['visual_id']==item['visual_id'] and r['instruction_hash']==item['instruction_hash']),None)
        if saved and saved['file_id']:
            meta=google.metadata(saved['file_id'])
            if meta.get('trashed') or folder not in meta.get('parents',[]):
                raise Conflict('Saved visual was moved or deleted; restore its Drive file before rebuilding')
            label='Original AI illustration; human visual review required.' if json.loads(saved['plan'])['kind']=='generate' else 'Retrieved source; rights uncleared.'
            return receipt(item,label,saved['file_id'])
        if saved and saved['state']=='generating' and not getattr(renderer,'recoverable',renderer.cached)(marker):
            raise Conflict('A prior generation has an uncertain outcome; reconcile it before requesting another paid image')
        save(store,job,item)
        plan=json.loads(saved['plan']) if saved and saved['plan'] else None
        if plan and saved['state']=='skipped' and source_registry and plan.get('_source_context_hash')!=source_context_hash:
            plan=None  # No image exists; reassess gaps when source references become available.
        if plan is None:
            task=copy.deepcopy(job)
            task['manifest']['prompts']['narrative']=PLAN_PROMPT
            task['manifest']['schemas']['narrative']=PLAN_SCHEMA
            task['step_id']=job['step_id']+'-'+item['visual_id']
            result=worker.research.run(task,dict(instructions=item['instructions'],source_registry=source_registry),lambda:store.heartbeat(job))
            if not store.record_usage(job,result['usage']): raise Conflict('Visual planning token budget exhausted')
            plan=json.loads(result['text']); Draft202012Validator(PLAN_SCHEMA).validate(plan)
            if plan['kind']=='generate' and not plan['prompt'].strip(): raise Conflict('Visual agent returned an empty generation prompt')
            if plan['kind']=='retrieve' and not plan['url'].strip(): raise Conflict('Visual agent returned an empty source URL')
            plan['_source_context_hash']=source_context_hash
            save(store,job,item,plan=canonical(plan))
        if plan['kind']=='skip':
            save(store,job,item,state='skipped')
            return receipt(item,'Needs production input: '+plan['reason'])
        # Reconcile an upload acknowledged by Drive but not yet saved in SQLite.
        files=google.drive.files().list(q="trashed=false and appProperties has { key='storiesVisual' and value='"+marker+"' }",fields='files(id,parents)',pageSize=100).execute().get('files',[])
        if len(files)>1 or files and folder not in files[0].get('parents',[]): raise Conflict('Visual upload identity mismatch')
        if files:
            file_id=files[0]['id']
        else:
            if saved and saved['state']=='uploading': raise Conflict('Prior Drive upload is ambiguous; reconcile before retry')
            if saved and saved['output_path']:
                path,mime=saved['output_path'],saved['mime_type']
            else:
                # Persist the intent before generation: a crash never silently bills twice.
                if plan['kind']=='generate':
                    if not renderer.cached(marker) and not renderer.check():
                        raise Conflict('Hermes image generation is unavailable; configure its image provider and rerun /story visuals build')
                    save(store,job,item,state='generating')
                path,mime=renderer.render(marker,plan,lambda:store.heartbeat(job))
                save(store,job,item,state='generated',output_path=str(path),mime_type=mime)
            from pathlib import Path
            body=Path(path).read_bytes()
            save(store,job,item,state='uploading')
            file_id=google.drive.files().create(body=dict(name='Visual '+item['visual_id'][:8]+' — '+item['instruction_hash'][:8],parents=[folder],appProperties={'storiesVisual':marker},description='Instructions (verbatim):\n'+canonical(item['instructions'])+'\nBuild plan:\n'+canonical(plan)+'\n'+('Original AI illustration; not primary evidence. Human visual review required.' if plan['kind']=='generate' else 'Retrieved source; rights uncleared.')),
                                               media_body=google.asset_media(body,mime),fields='id').execute()['id']
        meta=google.metadata(file_id)
        if meta.get('trashed') or folder not in meta.get('parents',[]): raise Conflict('Visual upload verification failed')
        save(store,job,item,state='ready',file_id=file_id,error='')
        from phase2.asset_inventory import refresh_inventory
        refresh_inventory(store,google,job['workspace_id'])
        label='Original AI illustration; human visual review required.' if plan['kind']=='generate' else 'Retrieved source; rights uncleared.'
        return receipt(item,label,file_id)

    results=[]
    # Each agent gets only its visual's saved instruction cells; no shared chat.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(build,item) for item in items]
        for item,future in zip(items,futures):
            try: results.append(future.result())
            except Exception as exc:
                # Never persist arbitrary provider errors containing credentials.
                from phase2.visual_renderer import RenderFailure
                error=str(exc) if isinstance(exc,(Conflict,RenderFailure)) else 'Visual build failed ('+type(exc).__name__+'); inspect worker configuration and retry.'
                save(store,job,item,error=error,**({'state':'planned'} if getattr(exc,'retry_safe',False) else {}))
                results.append(receipt(item,'Build blocked: '+error))
    with worker.render_lock(job):
        current=google.read(artifact['doc_id']); _,current_values=rows(current)
        if identify(current_values,ledger(store,job['workspace_id']))!=items:
            raise Conflict('Narrative visual instructions changed; files retained. Run /story visuals build again')
        verified=write_receipts(worker,job,artifact,current,items,results)
        checks=google.review_snapshot(artifact,'narrative')
        with store.connect(write=True) as db:
            store._owned(db,job)
            db.execute('INSERT OR REPLACE INTO package_assessments(step_id,assessment) VALUES (?,?)',(job['step_id'],canonical(dict(action='visual_build',reason='Built or reused visuals; production gaps remain in collateral cells.',doc_id=artifact['doc_id'],revision=verified['revisionId'],checks=checks['checks'],visuals=items))))
    failures=[r for r in ledger(store,job['workspace_id']) if any(r['visual_id']==i['visual_id'] and r['instruction_hash']==i['instruction_hash'] for i in items) and r['state'] not in {'ready','skipped'}]
    if failures: raise Conflict('Some visual builds are blocked; see the narrative collateral cells. Re-run /story visuals build to reuse completed assets and resume.')
