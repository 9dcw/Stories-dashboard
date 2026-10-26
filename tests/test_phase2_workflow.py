"""Behavioral tests of persistence, isolation, recovery and the research workflow."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jsonschema import ValidationError

from phase2.doc_policy import SPECS
from phase2.context import build_context
from phase2.orchestrator import Worker
from phase2.registry import snapshot, validate_output
from phase2.store import Conflict, LostLease, WorkflowStore
from phase2.telegram import dispatch, parse, signature


@pytest.fixture
def workflow(api_db):
    path,old,item,_=api_db
    story=old.request_phase2(item,'Research story')
    store=WorkflowStore(path)
    store.initialize()
    return store,story


def sample(key,url='https://news.example.test/first'):
    value=dict(summary='Evidence limits remain explicit',sections=[dict(heading='Research',text='Bounded findings')],sources=[dict(id='article',title='Original',url=url,publisher='Example',published_at='',accessed_at='2026-10-21',locator='article')],claims=[],gaps=['Primary record not obtained'])
    if key=='angles':
        angle=dict(title='A hypothesis',hook='Question',interpretation='Provisional',people_and_forces=[],stakes='Consequences',surprises='Unknown',implications='Context',disconfirming_evidence='Records')
        value.update(angles=[angle,dict(angle,title='Alternative')],recommended_thesis='Provisional direction')
    elif key=='plan':
        value['investigations']=[dict(question='What do records establish?',priority='high',source_targets=['Court'],narrative_purpose='Test hypothesis',visual_requirements=[])]
    elif key=='evidence':
        value.update(contradictions=[],timeline=[])
    elif key=='visuals':
        value['visuals']=[]
    elif key=='treatment':
        value['scenes']=[dict(beat='cold_open',purpose='Ask a question',viewer_question='What happened?',outline='Clearly identify missing evidence',claim_ids=[],visuals=[])]
    elif key=='review':
        value.update(findings=[],readiness='needs_research',policy_checks={})
    topics=SPECS[key][3] or ['Assessment']
    value.update(editorial_rows=[dict(topic=t,short='Evidence remains provisional.',detail='Primary records must be obtained before making a factual finding.') for t in topics],policy_exception='')
    return value


class FakeGoogle:
    def __init__(self):
        self.writes=[]
        self.conflict=False
        self.proposals=[]

    def read(self,doc_id):
        return {'revisionId':'revision-'+doc_id.removeprefix('doc-')}

    def review_snapshot(self, artifact, key):
        return dict(doc_id=artifact['doc_id'],revision='revision-'+key,text='Current document',managed_matches=True,checks=dict(policy_version='tables.v1',total_words=100,managed_words=90,cap=SPECS[key][0],issues=[],outcome='pass'))

    def provision(self,store,job,telegram):
        store.checkpoint(job,resources={'folder':'folder','images':'images'},topic=('-1003984316484','12','13'))
        for key in job['manifest']['artifacts']:
            store.document(job,key,'doc-'+key)

    def header(self,*args):
        return 'Navigation\n'

    def write(self,artifact,text,marker):
        if self.conflict and artifact['artifact_key']!='hub':
            raise Conflict('Human edit')
        self.writes.append((artifact['artifact_key'],marker,text))
        return 'baseline-'+marker,'revision'

    def hub(self,*args):
        return 'Hub\n'

    def proposal(self,store,job,version,text):
        self.proposals.append(version)
        return 'proposal'

    def append_revision(self,artifact,text,marker):
        self.writes.append(('append',marker,text))
        return 'appended','revision'


class FakeResearch:
    def __init__(self):
        self.calls=[]

    def run(self,job,inputs,heartbeat):
        self.calls.append((job['step_key'],inputs))
        output=sample(job['step_key'])
        if job['step_key']=='review':
            output['editorial_rows'][0]['detail']='; '.join(key+' '+v['revision']+' tables.v1 total '+str(v['checks']['total_words'])+' managed '+str(v['checks']['managed_words'])+' pass' for key,v in inputs['current_docs'].items())
        return dict(text=json.dumps(output),usage={'total_tokens':100},provenance={'model':'test'})


class FakeTelegram:
    chat_id='-1003984316484'
    def __init__(self):
        self.messages=[]
    def send(self,thread,text):
        self.messages.append((thread,text))


def worker(store,tmp_path):
    return Worker(store,FakeResearch(),FakeGoogle(),FakeTelegram(),tmp_path/'locks')


def drain(w,limit=30):
    for _ in range(limit):
        if not w.tick():
            return
    pytest.fail('Workflow did not settle')


def test_complete_pipeline_and_targeted_revision(workflow,tmp_path):
    store,story=workflow
    run=store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    drain(w)
    assert store.status(story)['state']=='completed'
    assert [k for k,_ in w.research.calls]==['angles','plan','evidence','visuals','treatment','review']
    with store.connect() as db:
        project=store.story(db,story)
        assert project['status']=='Phase 2'
        assert project['google_doc_url'].endswith('/doc-hub/edit')
        assert db.execute('select count(*) from artifact_versions').fetchone()[0]==6
    revision=store.enqueue(story,'revision','revise_artifact','angles','Follow consequences','editor')
    drain(w)
    artifacts=store.artifacts(project['workspace_id'])
    assert not artifacts['angles']['stale']
    assert all(artifacts[k]['stale'] for k in ['plan','evidence','visuals','treatment','review'])
    context=w.research.calls[-1][1]
    assert context['instruction']=='Follow consequences'
    assert context['upstream']=={}
    assert context['current_output']
    assert 'conversation' not in context


def test_duplicate_and_conflicting_request_ids(workflow):
    store,story=workflow
    run=store.enqueue(story,'same')
    assert store.enqueue(story,'same')==run
    assert store.enqueue(story,'different-initial')==run
    with pytest.raises(Conflict):
        store.enqueue(story,'same','revise_artifact','angles','different')


def test_lease_recovery_fences_dead_worker(workflow):
    store,story=workflow
    store.enqueue(story,'initial')
    first=store.claim()
    with store.connect(write=True) as db:
        db.execute('update step_executions set lease_until=? where step_id=?',(time.time()-1,first['step_id']))
    recovered=WorkflowStore(store.path).claim()
    assert recovered['step_id']==first['step_id']
    assert recovered['fence']!=first['fence']
    with pytest.raises(LostLease):
        store.finish(first)


def test_concurrent_claim_only_dispatches_provision_once(workflow):
    store,story=workflow
    store.enqueue(story,'initial')
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims=list(pool.map(lambda _:store.claim(),range(4)))
    assert sum(c is not None for c in claims)==1


def test_independent_steps_can_run_together(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    for _ in range(3):
        w.tick()
    first=store.claim()
    second=store.claim()
    assert {first['step_key'],second['step_key']}=={'evidence','visuals'}
    assert store.claim() is None


def test_human_conflict_retains_candidate_and_explicit_append(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    w.tick()
    w.google.conflict=True
    w.tick()
    assert store.status(story)['state']=='blocked'
    candidate=w.google.proposals[0]
    status=store.status(story)
    assert status['requires_resolution'] is True
    recovery=status['recovery'][0]
    assert recovery['version_id']==candidate
    assert recovery['artifact']=='angles'
    assert recovery['append_command']=='/story append '+candidate
    assert recovery['cancel_command']=='/story cancel '+status['run_id']
    with pytest.raises(Conflict):
        store.retry(story)
    store.enqueue_resolution(story,'append-request','append',candidate,'editor')
    w.tick()
    assert store.version(candidate)['state']=='published'
    assert any(k=='append' for k,_,_ in w.google.writes)
    assert not store.status(story)['requires_resolution']


def test_changed_editorial_direction_rejects_old_candidate(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    w.tick()
    job=store.claim()
    inputs=store.save_inputs(job,build_context(store,job))
    store.decide(job['workspace_id'],'decision','approve_direction','Follow policyholder consequences','editor')
    with pytest.raises(Conflict,match='direction changed'):
        store.candidate(job,sample('angles'),inputs,{}, {})


def test_cancel_prevents_late_results(workflow):
    store,story=workflow
    run=store.enqueue(story,'initial')
    job=store.claim()
    store.cancel(job['workspace_id'],run)
    with pytest.raises(LostLease):
        store.checkpoint(job,resources={'folder':'wrong'})
    assert store.claim() is None


def test_schema_claim_support_and_treatment_traceability():
    manifest=snapshot()
    for key in manifest['steps']:
        validate_output(manifest,key,sample(key))
    value=sample('evidence')
    value['claims']=[dict(id='c1',text='An allegation',label='Alleged',source_ids=['missing'],disposition='complaint',limitations='not established')]
    with pytest.raises(ValueError,match='unknown source'):
        validate_output(manifest,'evidence',value)
    value['claims'][0]['source_ids']=[]
    with pytest.raises(ValueError,match='source support'):
        validate_output(manifest,'evidence',value)
    bad=sample('treatment')
    bad['claims']=value['claims']
    with pytest.raises(ValueError,match='evidence ledger'):
        Worker.check_evidence({'step_key':'treatment'},{'upstream':{'evidence':sample('evidence')}},bad)


def test_ordinary_chat_does_not_parse_as_an_operation():
    with pytest.raises(ValueError):
        parse('Please revise the treatment')
    assert parse('/story@mehyanbot revise angles Follow consequences')['target']=='angles'


def test_topic_and_user_authorization(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    w.tick()
    event=dict(user_id='42',chat_id='-1003984316484',thread_id='12',message_id='9',text='/story decision approve Focus on consequences')
    result=dispatch(store,event,{'42'},event['chat_id'])
    assert dispatch(store,event,{'42'},event['chat_id'])==result
    with pytest.raises(PermissionError):
        dispatch(store,event,{'99'},event['chat_id'])
    with pytest.raises(Conflict):
        dispatch(store,dict(event,thread_id='unknown'),{'42'},event['chat_id'])


def test_repository_api_service_auth_and_legacy_isolation(runtime_config,api_db,monkeypatch):
    from api.main import create_app
    from tests.conftest import login
    from api import auth
    config=replace(runtime_config,phase2_engine='repository',phase2_service_secret='k'*48,phase2_allowed_users='42')
    monkeypatch.setattr(auth.id_token,'verify_oauth2_token',lambda *_a,**_kw:dict(aud=config.google_client_id,iss='https://accounts.google.com',sub=config.google_allowed_sub,email=config.google_allowed_email,email_verified=True))
    with TestClient(create_app(config),base_url='https://localhost') as client:
        csrf=login(client)
        response=client.post(f'/api/v1/items/{api_db[2]}/phase2',headers={'Origin':config.public_origin,'X-CSRF-Token':csrf})
        assert response.status_code==202
        assert response.json()['phase2_job']['state']=='queued'
        app=client.app
        app.state.phase2_jobs.store.claim()
        with app.state.store._connect() as db:
            assert db.execute('select count(*) from phase2_jobs').fetchone()[0]==0
        url='/api/v1/internal/phase2/dispatch'
        assert client.post(url,json={}).status_code==403
        assert client.post(url,content=b'x'*17000).status_code==413
        body=json.dumps(dict(chat_id=config.phase2_chat_id,thread_id='bad',user_id='42',message_id='9',text='/story status')).encode()
        stamp=str(int(time.time()))
        headers={'X-Stories-Timestamp':stamp,'X-Stories-Signature':signature(config.phase2_service_secret,stamp,body)}
        assert client.post(url,content=body,headers=headers).status_code==409
        headers['X-Stories-Timestamp']='0'
        assert client.post(url,content=body,headers=headers).status_code==403


def test_existing_phase2_requires_identity_migration(api_db):
    path,old,item,_=api_db
    story=old.promote_item(item,'Legacy','Phase 2')
    store=WorkflowStore(path)
    store.initialize()
    with pytest.raises(Conflict,match='identity migration'):
        store.enqueue(story,'initial')


def test_recover_interrupted_dashboard_promotion(workflow):
    store,story=workflow
    store.recover_pending()
    assert store.status(story)['state']=='queued'
    store.recover_pending()
    with store.connect() as db:
        assert db.execute('select count(*) from workflow_runs').fetchone()[0]==1


def test_budget_exhaustion_is_durable(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path);w.tick()
    job=store.claim()
    assert not store.record_usage(job,{'total_tokens':999999})
    assert store.status(story)['state']=='blocked'
    assert store.status(story)['tokens']==999999
    assert not w.research.calls


def test_targeted_review_only_needs_its_target(workflow,tmp_path):
    store,story=workflow
    original=store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    w.tick();w.tick()
    with store.connect() as db:
        wid=store.story(db,story)['workspace_id']
    store.cancel(wid,original)
    store.enqueue(story,'review','review_artifact','angles')
    drain(w)
    assert store.status(story)['state']=='completed'
    context=w.research.calls[-1][1]
    assert list(context['upstream'])==['angles']


def test_superseded_direction_is_preserved_but_not_reinjected(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path);w.tick()
    with store.connect() as db:
        wid=store.story(db,story)['workspace_id']
    old=store.decide(wid,'old-direction','approve_direction','Original direction','editor')
    new=store.decide(wid,'new-direction','approve_direction','Revised direction','editor',supersedes=old)
    job=store.claim()
    assert job['step_key']=='angles'
    inputs=build_context(store,job)
    assert [d['decision_id'] for d in inputs['decisions']]==[new]
    assert inputs['decision_ids']==[old,new]


def test_legacy_callbacks_cannot_replace_repository_artifacts(workflow,api_db,runtime_config):
    from api.phase2 import Phase2Jobs
    from api.models import Phase2Callback
    store,story=workflow
    legacy=Phase2Jobs(api_db[1],runtime_config)
    legacy.enqueue(story)
    with store.connect(write=True) as db:
        db.execute("UPDATE phase2_jobs SET state='waiting',attempts=1 WHERE story_id=?",(story,))
        run=db.execute('SELECT run_id FROM phase2_jobs WHERE story_id=?',(story,)).fetchone()[0]
    store.enqueue(story,'initial')
    with pytest.raises(ValueError,match='repository-owned'):
        legacy.callback(story,Phase2Callback(completion_token=legacy.token(run),state='waiting',step='topic'))


@pytest.mark.parametrize('failure',['schema','evidence'])
def test_treatment_repair_receives_contract_feedback(workflow,tmp_path,failure):
    store,story=workflow
    store.enqueue(story,'repair-contract')
    w=worker(store,tmp_path)
    calls=[]
    class NeedsRepair(FakeResearch):
        def run(self,job,inputs,heartbeat):
            result=super().run(job,inputs,heartbeat)
            if job['step_key']=='treatment':
                calls.append(inputs)
                if len(calls)==1:
                    output=json.loads(result['text'])
                    if failure=='schema':
                        output['scenes'][0]['beat']='unregistered_beat'
                    else:
                        output['claims']=[dict(id='angle-only',text='An unsupported upstream assertion',label='Unverified',source_ids=[],disposition='',limitations='')]
                    result['text']=json.dumps(output)
            return result
    w.research=NeedsRepair()
    drain(w)
    assert store.status(story)['state']=='completed'
    assert len(calls)==2
    feedback=calls[1]['validation_repair']
    assert ('scenes.0.beat (enum)' if failure=='schema' else 'Treatment claims must match the evidence ledger') in feedback
    assert 'unregistered_beat' not in feedback


def test_validation_feedback_does_not_echo_invalid_json():
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads('untrusted source content')
    assert Worker.validation_feedback(caught.value)=='Response must be valid JSON'
