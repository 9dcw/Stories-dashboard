"""The new default runs once and revises the same narrative without a package DAG."""
import json
import pytest
from phase2.store import WorkflowStore, Conflict
from phase2.context import build_context
from phase2.registry import snapshot, validate_output
from phase2.doc_policy import render_document, NARRATIVE_HEADERS, audit
from phase2.orchestrator import Worker
from phase2.telegram import parse, dispatch
from tests.test_phase2_workflow import FakeGoogle, FakeTelegram


def narrative():
    return dict(summary='A visual question',sources=[dict(id='article',title='Original',url='https://news.example.test/first',publisher='Example',published_at='',accessed_at='2026-10-28',locator='article')],claims=[],gaps=['Obtain primary footage'],narrative_rows=[dict(visual='Original footage',talk_track='• What does the scene establish?',production_notes='Unverified: obtain primary footage; https://news.example.test/first',collateral='Not in Drive. Request from the original uploader; rights uncleared.')],policy_exception='')


@pytest.fixture
def setup(api_db):
    path,old,item,_=api_db
    story=old.request_phase2(item,'Story',research_brief='Open with the image. Keep “my wording”.')
    store=WorkflowStore(path);store.initialize()
    return store,story


class Google(FakeGoogle):
    def review_snapshot(self,artifact,key):
        return dict(doc_id=artifact['doc_id'],revision='revision',checks=audit('narrative','test','test',[NARRATIVE_HEADERS,['image','talk','notes','asset']]))


class Research:
    def __init__(self):self.calls=[]
    def run(self,job,inputs,heartbeat):
        self.calls.append((job,inputs))
        return dict(text=json.dumps(narrative()),usage={'total_tokens':100},provenance={'model':'test'})


def run(store,tmp_path):
    research=Research();google=Google();worker=Worker(store,research,google,FakeTelegram(),tmp_path)
    for _ in range(4):
        job=store.claim()
        if not job:break
        worker.execute(job)
    return research,google


def test_default_is_one_stage_and_four_column_table():
    manifest=snapshot()
    assert manifest['steps']==['narrative']
    assert list(manifest['artifacts'])==['narrative']
    assert manifest['budgets']['iterations']==12
    validate_output(manifest,'narrative',narrative())
    table=render_document('Story','narrative',dict(narrative(),producer_guidance='Exact instruction.'))
    assert table.grid[0]==NARRATIVE_HEADERS
    assert all(len(r)==4 for r in table.grid)
    assert 'Exact instruction.' in table.prefix


def test_promotion_revision_guidance_prompt_isolation_and_readback(setup,tmp_path):
    store,story=setup
    store.enqueue(story,'promotion')
    research,google=run(store,tmp_path)
    assert len(research.calls)==1
    assert store.status(story)['state']=='completed'
    assert [x[0] for x in google.writes]==['narrative']
    wid=store.status(story)['artifacts'][0]
    doc=wid['doc_id']
    version=store.version(wid['current_version'])
    assert json.loads(version['provenance'])['document_checks']['policy_version']=='tables.v1'
    assert 'Keep “my wording”.' in google.writes[0][2].prefix
    prompt=store.update_narrative_prompt('meta','Prefer maps in future drafts.','producer')
    assert prompt['version']==1
    store.enqueue(story,'revision',operation='revise_artifact',target='narrative',instruction='Find the original photo.\nKeep the ending.')
    r,g=run(store,tmp_path)
    assert len(r.calls)==1
    job,inputs=r.calls[0]
    assert inputs['current_output']['narrative_rows']==narrative()['narrative_rows']
    assert not inputs['upstream']
    assert inputs['producer_guidance']=='Open with the image. Keep “my wording”. | Find the original photo.\nKeep the ending.'
    assert job['manifest']['prompt_version']==0
    assert 'Prefer maps' not in job['manifest']['prompts']['narrative']
    assert store.status(story)['artifacts'][0]['doc_id']==doc
    assert inputs['producer_guidance'] in g.writes[0][2].prefix
    assert 'producer_guidance' not in json.loads(store.version(store.status(story)['artifacts'][0]['current_version'])['output'])


def test_prompt_update_is_idempotent_and_pinned_on_new_promotion(setup):
    store,story=setup
    first=store.update_narrative_prompt('feedback','Use an image first.','me')
    assert store.update_narrative_prompt('feedback','Use an image first.','me')==first
    with pytest.raises(Conflict):store.update_narrative_prompt('feedback','Other feedback','me')
    store.enqueue(story,'promotion')
    with store.connect() as db:manifest=json.loads(db.execute('SELECT manifest FROM workflow_runs').fetchone()[0])
    assert manifest['prompt_version']==1
    assert 'Use an image first.' in manifest['prompts']['narrative']


def test_revise_replaces_blocked_run_without_resetting_usage(setup,tmp_path):
    store,story=setup;old=store.enqueue(story,'promotion')
    job=store.claim();store.checkpoint(job,resources={'folder':'folder','images':'images'},topic=('-1003984316484','12','13'));store.document(job,'narrative','doc-narrative');store.finish(job)
    job=store.claim();store.record_usage(job,{'total_tokens':200000})
    assert store.status(story)['state']=='blocked'
    new=store.enqueue(story,'follow-up',operation='revise_artifact',target='narrative',instruction='Finish a focused draft.')
    assert old!=new
    r,g=run(store,tmp_path)
    assert len(r.calls)==1 and store.status(story)['state']=='completed'
    with store.connect() as db:
        oldrow=db.execute('SELECT state,tokens FROM workflow_runs WHERE run_id=?',(old,)).fetchone()
        assert tuple(oldrow)==('cancelled',200000)


def test_commands_preserve_guidance_and_reject_old_artifact_syntax():
    assert parse('/story revise Find photos.\nKeep the opener.')['instruction']=='Find photos.\nKeep the opener.'
    assert parse('/story prompt show')=={'action':'prompt_show'}
    assert parse('/story prompt update Prefer maps.')=={'action':'prompt_update','text':'Prefer maps.'}
    for text in ['/story investigate evidence question','/story review treatment','/story revise','/story prompt update']:
        with pytest.raises(ValueError):parse(text)


def test_asset_collection_uploads_once_and_preserves_receipt(setup,tmp_path,monkeypatch):
    from types import SimpleNamespace
    from phase2.google_docs import GoogleDocs
    from tests.test_phase2_google import Result
    from phase2 import source_reader
    store,story=setup;store.enqueue(story,'initial')
    job=store.claim();store.checkpoint(job,resources={'folder':'folder','images':'images'})
    class Files:
        def __init__(self):self.uploads=[]
        def files(self):return self
        def list(self,**kwargs):
            return Result({'files':[{'id':'asset','parents':['images']}] if self.uploads else []})
        def create(self,**kwargs):self.uploads.append(kwargs);return Result({'id':'asset'})
        def get(self,**kwargs):return Result({'id':'asset','parents':['images'],'trashed':False})
    drive=Files();google=GoogleDocs(drive,None,'parent')
    monkeypatch.setattr(google,'asset_media',lambda body,mime:dict(body=body,mime=mime))
    requests=[]
    monkeypatch.setattr(source_reader,'fetch_asset',lambda url:(requests.append(url) or b'bytes','image/png',url))
    value=narrative();value['asset_requests']=[dict(row_index=0,url='https://example.org/photo.png',title='Scene',owner='Photographer')]
    google.collect_assets(store,job,value)
    assert 'https://drive.google.com/file/d/asset/view' in value['narrative_rows'][0]['collateral']
    assert 'rights uncleared' in value['narrative_rows'][0]['collateral']
    google.collect_assets(store,job,value)
    assert len(drive.uploads)==1 and len(requests)==1
    assert drive.uploads[0]['body']['parents']==['images']


def test_asset_fetch_failure_is_an_explicit_gap(setup,monkeypatch):
    from phase2.google_docs import GoogleDocs
    from tests.test_phase2_google import Result
    from phase2 import source_reader
    store,story=setup;store.enqueue(story,'initial');job=store.claim()
    store.checkpoint(job,resources={'images':'images'})
    class Files:
        def files(self):return self
        def list(self,**kwargs):return Result({'files':[]})
    def blocked(url):raise ValueError('HTML login page')
    monkeypatch.setattr(source_reader,'fetch_asset',blocked)
    output=narrative();output['asset_requests']=[dict(row_index=0,url='https://example.org/login',title='Asset',owner='Unknown')]
    GoogleDocs(Files(),None,'parent').collect_assets(store,job,output)
    assert 'not verified in Drive' in output['narrative_rows'][0]['collateral']


def test_existing_package_is_synthesized_once_and_preserved(setup,tmp_path,monkeypatch):
    from tests.test_phase2_workflow import worker,drain
    store,story=setup
    monkeypatch.setattr('phase2.store.snapshot',lambda:snapshot(legacy=True))
    store.enqueue(story,'legacy-promotion')
    drain(worker(store,tmp_path))
    before={a['artifact_key']:a['current_version'] for a in store.status(story)['artifacts']}
    monkeypatch.setattr('phase2.store.snapshot',snapshot)
    store.enqueue(story,'convert',operation='revise_artifact',target='narrative',instruction='Build a visual narrative from the existing research.')
    research,google=run(store,tmp_path)
    assert len(research.calls)==1 and store.status(story)['state']=='completed'
    inputs=research.calls[0][1]
    assert 'evidence' in inputs['legacy_material'] and inputs['legacy_sources']
    assert 'sources' not in inputs['legacy_material']['evidence']
    for a in store.status(story)['artifacts']:
        if a['artifact_key'] in before:assert a['current_version']==before[a['artifact_key']]
    store.enqueue(story,'refine',operation='revise_artifact',target='narrative',instruction='Refine the opening.')
    research,_=run(store,tmp_path)
    assert 'legacy_material' not in research.calls[0][1]
