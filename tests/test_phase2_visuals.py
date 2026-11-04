import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from phase2.context import build_context
from phase2.doc_policy import NARRATIVE_HEADERS
from phase2.google_docs import GoogleDocs, units
from phase2.orchestrator import Worker
from phase2.registry import digest
from phase2.store import Conflict, WorkflowStore
from phase2.telegram import parse
from phase2.visuals import RECEIPT, identify, instructions, ledger, receipt, rows, update_receipts
from tests.test_phase2_google import Result, paragraph


class LiveDocs:
    def __init__(self,grid):
        self.grid=grid; self.revision=1; self.batches=[]; self.named=False

    def documents(self): return self

    def get(self,**_):
        at=2; table=[]
        for values in self.grid:
            cells=[]
            for value in values:
                text=value+'\n'
                cells.append(dict(content=[paragraph(at,text)])); at+=units(text)+2
            table.append(dict(tableCells=cells))
        document=dict(revisionId=str(self.revision),body=dict(content=[dict(startIndex=1,endIndex=at+1,table=dict(tableRows=table))]))
        if self.named:
            document['namedRanges']={'managed':dict(namedRanges=[dict(name='stories-managed-v1',ranges=[dict(startIndex=1,endIndex=at+1)])])}
        return Result(document)

    def batchUpdate(self,documentId,body):
        assert body['writeControl']['requiredRevisionId']==str(self.revision)
        self.batches.append(copy.deepcopy(body))
        for request in body['requests']:
            if 'updateTextStyle' in request: continue
            deletion=request.get('deleteContentRange')
            start=deletion['range']['startIndex'] if deletion else request['insertText']['location']['index']
            document=self.get().execute()
            for ri,row in enumerate(document['body']['content'][0]['table']['tableRows']):
                for ci,cell in enumerate(row['tableCells']):
                    p=cell['content'][0]
                    if p['startIndex']<=start<p['endIndex']:
                        value=self.grid[ri][ci].encode('utf-16-le')
                        a=(start-p['startIndex'])*2
                        if deletion:
                            b=(deletion['range']['endIndex']-p['startIndex'])*2
                            value=value[:a]+value[b:]
                        else:
                            value=value[:a]+request['insertText']['text'].encode('utf-16-le')+value[a:]
                        self.grid[ri][ci]=value.decode('utf-16-le')
                        break
                else: continue
                break
            else: pytest.fail('Invalid Docs insertion index')
        self.revision+=1
        return Result({})


class Files:
    def __init__(self): self.saved={}; self.uploads=0
    def list(self,q,**_):
        return Result(dict(files=[dict(id=k,parents=v['parents']) for k,v in self.saved.items() if v['appProperties']['storiesVisual'] in q]))
    def create(self,body,**_):
        self.uploads+=1; key='file-'+str(self.uploads)
        self.saved[key]=dict(body,id=key)
        return Result(dict(id=key))
    def get(self,fileId,**_): return Result(self.saved[fileId])


class Drive:
    def __init__(self): self.assets=Files(); self.open_comments=False
    def files(self): return self.assets
    def comments(self): return self
    def list(self,**_): return Result(dict(comments=[dict(id='c')] if self.open_comments else []))


class Research:
    def __init__(self): self.calls=[]; self.kind='generate'
    def run(self,job,inputs,heartbeat):
        self.calls.append(inputs)
        return dict(text=json.dumps(dict(kind=self.kind,prompt=inputs['instructions']['visual'],url='',reason='Original explanatory illustration.',aspect_ratio='landscape')),usage=dict(total_tokens=20))


class Renderer:
    def __init__(self,tmp): self.tmp=tmp; self.calls=[]; self.failure=False
    def cached(self,marker): return (self.tmp/marker).exists()
    def check(self): return True
    def render(self,marker,plan,heartbeat):
        if self.failure: raise RuntimeError('private-provider-error')
        self.calls.append(plan)
        path=self.tmp/marker; path.write_bytes(b'image')
        return str(path),'image/png'


@pytest.fixture
def visual_worker(api_db,tmp_path):
    path,old,item,_=api_db
    story=old.request_phase2(item,'A visual story')
    store=WorkflowStore(path); store.initialize()
    store.enqueue(story,'initial')
    job=store.claim(); store.checkpoint(job,resources=dict(folder='folder',images='images'),topic=('-1003984316484','12','13'))
    store.document(job,'narrative','doc'); store.finish(job)
    job=store.claim(); inputs=store.save_inputs(job,build_context(store,job))
    version=store.candidate(job,dict(narrative_rows=[]),inputs,{},{}); store.publish(job,version,'','1'); store.finish(job)
    docs=LiveDocs([NARRATIVE_HEADERS[:],['An original diagram 😀','Talk track','Illustrative only','Create an original diagram.'],['Another diagram','More narration','Illustrative only','Create this diagram too.']])
    google=GoogleDocs(Drive(),docs,'parent'); google.asset_media=lambda body,mime:body
    worker=Worker(store,Research(),google,SimpleNamespace(send=lambda *_:1),tmp_path/'locks')
    worker.visual_renderer=Renderer(tmp_path)
    return worker,story,docs


def run(worker,story,key):
    rid=worker.store.enqueue(story,key,'build_visuals','narrative')
    job=worker.store.claim(); assert job['operation']=='build_visuals'
    worker.execute(job)
    return worker.store.status(story),job


def test_command_and_invalid_syntax():
    assert parse('/story@bot visuals build')==dict(action='operate',operation='build_visuals',target='narrative',instruction='')
    with pytest.raises(ValueError): parse('/story visuals build all')


def test_first_build_reuse_reorder_and_talk_track_edit(visual_worker):
    w,story,docs=visual_worker
    original=copy.deepcopy(docs.grid)
    status,job=run(w,story,'first')
    assert status['state']=='completed'
    assert len(w.visual_renderer.calls)==2
    assert w.google.drive.assets.uploads==2
    assert all(r['state']=='ready' for r in ledger(w.store,job['workspace_id']))
    assert all(a[:3]==b[:3] and instructions(a)==instructions(b) for a,b in zip(docs.grid[1:],original[1:]))
    assert all('https://drive.google.com/file/' in r[3] for r in docs.grid[1:])
    written=len(docs.batches)
    docs.grid[1][1]='Producer revised narration'; docs.grid[1:]=reversed(docs.grid[1:])
    status,_=run(w,story,'second')
    assert status['state']=='completed'
    assert len(w.visual_renderer.calls)==2 and len(w.research.calls)==2
    assert w.google.drive.assets.uploads==2
    assert len(docs.batches)==written


def test_only_changed_instructions_rebuild_and_reverting_reuses(visual_worker):
    w,story,docs=visual_worker
    run(w,story,'first')
    docs.grid[1][0]+=' Use blue arrows.'
    status,job=run(w,story,'changed')
    assert status['state']=='completed'
    assert len(w.visual_renderer.calls)==3 and w.google.drive.assets.uploads==3
    saved=ledger(w.store,job['workspace_id'])
    assert len(saved)==3 and len({r['visual_id'] for r in saved})==2
    docs.grid[1][0]=docs.grid[1][0].removesuffix(' Use blue arrows.')
    run(w,story,'reverted')
    assert len(w.visual_renderer.calls)==3 and w.google.drive.assets.uploads==3


def test_missing_inputs_are_visible_without_fake_assets(visual_worker):
    w,story,docs=visual_worker; w.research.kind='skip'
    status,_=run(w,story,'skip')
    assert status['state']=='completed'
    assert not w.visual_renderer.calls and not w.google.drive.assets.uploads
    assert all('Needs production input:' in r[3] for r in docs.grid[1:])


def test_failed_generation_is_not_silently_billed_twice(visual_worker):
    w,story,docs=visual_worker; w.visual_renderer.failure=True
    status,job=run(w,story,'failure')
    assert status['state']=='blocked'
    assert all('private-provider-error' not in r[3] for r in docs.grid[1:])
    assert all(r['state']=='generating' for r in ledger(w.store,job['workspace_id']))
    w.visual_renderer.failure=False
    status,_=run(w,story,'retry')
    assert status['state']=='blocked' and not w.visual_renderer.calls
    assert all('uncertain outcome' in r[3] for r in docs.grid[1:])


def test_ambiguous_drive_upload_reconciles_without_regeneration(visual_worker,monkeypatch):
    w,story,docs=visual_worker
    original=w.google.drive.assets.create
    def interrupted(**kwargs):
        original(**kwargs)
        raise ConnectionError('Response lost')
    monkeypatch.setattr(w.google.drive.assets,'create',interrupted)
    status,job=run(w,story,'interrupted')
    assert status['state']=='blocked' and w.google.drive.assets.uploads==2
    monkeypatch.setattr(w.google.drive.assets,'create',original)
    status,_=run(w,story,'resume')
    assert status['state']=='completed' and w.google.drive.assets.uploads==2
    assert len(w.visual_renderer.calls)==2


def test_live_edits_during_generation_preserved(visual_worker):
    w,story,docs=visual_worker
    original=w.visual_renderer.render
    def changed(*args):
        result=original(*args)
        docs.grid[1][0]='A producer changed this visual'
        return result
    w.visual_renderer.render=changed
    status,_=run(w,story,'editing')
    assert status['state']=='blocked'
    assert docs.grid[1][0]=='A producer changed this visual'
    assert w.google.drive.assets.uploads==2


def test_comments_prevent_any_doc_writes_or_generation(visual_worker):
    w,story,docs=visual_worker; w.google.drive.open_comments=True
    status,_=run(w,story,'comments')
    assert status['state']=='blocked' and not docs.batches and not w.research.calls


def test_copied_receipts_are_rejected(visual_worker):
    w,story,docs=visual_worker; run(w,story,'first')
    docs.grid[2][3]=docs.grid[1][3]
    status,_=run(w,story,'copy')
    assert status['state']=='blocked' and len(w.visual_renderer.calls)==2


def test_receipts_preserve_human_text_after_them_and_utf16(visual_worker):
    w,story,docs=visual_worker; run(w,story,'first')
    docs.grid[1][3]+='\nProducer note 😀: retain this.'
    _,values=rows(w.google.read('doc'))
    items=identify(values,[])
    before=instructions(values[0])
    updated=update_receipts(w.google,dict(doc_id='doc'),w.google.read('doc'),items,[receipt(i,'Updated receipt') for i in items])
    _,after=rows(updated)
    assert instructions(after[0])==before and 'Producer note 😀: retain this.' in after[0][3]


def test_build_requires_completed_narrative(api_db):
    path,old,item,_=api_db; story=old.request_phase2(item,'A story')
    store=WorkflowStore(path); store.initialize()
    with pytest.raises(Conflict,match='Complete the narrative'):
        store.enqueue(story,'premature','build_visuals','narrative')


def test_unavailable_provider_can_be_configured_and_retried(visual_worker):
    w,story,docs=visual_worker
    w.visual_renderer.check=lambda:False
    status,job=run(w,story,'unavailable')
    assert status['state']=='blocked' and not w.visual_renderer.calls
    assert all(r['state']=='planned' for r in ledger(w.store,job['workspace_id']))
    w.visual_renderer.check=lambda:True
    status,_=run(w,story,'configured')
    assert status['state']=='completed' and len(w.visual_renderer.calls)==2
    assert len(w.research.calls)==2


def test_local_result_recovers_generation_checkpoint(visual_worker):
    w,story,docs=visual_worker
    original=w.visual_renderer.render
    def interrupted(*args):
        original(*args)
        raise ConnectionError('Crashed after writing image')
    w.visual_renderer.render=interrupted
    status,_=run(w,story,'interrupted-local')
    assert status['state']=='blocked'
    def cached(marker,plan,heartbeat):
        assert w.visual_renderer.cached(marker)
        return str(w.visual_renderer.tmp/marker),'image/png'
    w.visual_renderer.render=cached
    status,_=run(w,story,'recover-local')
    assert status['state']=='completed' and len(w.visual_renderer.calls)==2


def test_managed_baseline_advances_for_receipts_but_not_human_edits(visual_worker):
    w,story,docs=visual_worker; docs.named=True
    with w.store.connect(write=True) as db:
        artifact=db.execute("SELECT * FROM artifacts WHERE artifact_key='narrative'").fetchone()
        content=w.google.body(w.google.read('doc'))[1]
        baseline=w.google.fingerprint(content,1,content[0]['endIndex'])
        db.execute('UPDATE artifacts SET baseline=? WHERE artifact_id=?',(baseline,artifact['artifact_id']))
        wid=artifact['workspace_id']
    status,_=run(w,story,'with-baseline')
    assert status['state']=='completed'
    artifact=w.store.artifacts(wid)['narrative']
    assert artifact['baseline']!=baseline
    assert w.google.review_snapshot(artifact,'narrative')['managed_matches']
    old_baseline=artifact['baseline']; docs.grid[1][1]='A human edit that must stay protected'
    run(w,story,'human-edited')
    assert w.store.artifacts(wid)['narrative']['baseline']==old_baseline
    assert not w.google.review_snapshot(w.store.artifacts(wid)['narrative'],'narrative')['managed_matches']
