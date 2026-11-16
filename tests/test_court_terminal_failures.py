import json
import pytest
from court_pipeline import run_pipeline, RULE_VERSION, terminal_failure, save_screen
from story_store import StoryStore


def setup(store,n=1):
    sid=store.add_source('Court','B','US','https://www.ca1.uscourts.gov',collector_type='html_list',poll_url='https://www.ca1.uscourts.gov/opinions',collector_config=json.dumps({'opinion_url_pattern':r'/opnfiles/.+\.pdf'}))
    return [store.add_item(sid,'Opinion '+str(i),f'https://www.ca1.uscourts.gov/opnfiles/{i}.pdf').item_id for i in range(n)]


def test_extraction_failure_is_terminal_and_visible_without_retry(tmp_path):
    store=StoryStore(tmp_path/'db');ids=setup(store);calls=[]
    def fetch(url,**kw):calls.append(url);raise OSError('Opinion no longer exists')
    first=run_pipeline(store,lambda _:pytest.fail('No document'),poll=False,fetcher=fetch)
    second=run_pipeline(store,lambda _:pytest.fail('Terminal failure retried'),poll=False,fetcher=fetch)
    assert first['errors']==1 and first['pending']==0
    assert second['processed']==0 and second['pending']==0 and len(calls)==1
    with store._connect() as c:
        row=c.execute('select * from court_screenings').fetchone();note=c.execute('select * from candidate_notes').fetchone()
    result=json.loads(row['result_json'])
    assert row['decision']=='ERROR' and result['terminal_failure'] and result['retry_policy']=='manual_only'
    assert 'no longer exists' in result['error'] and note['summary_status']=='FAILED'


def test_terminal_failure_does_not_requeue_on_policy_change(tmp_path):
    store=StoryStore(tmp_path/'db');ids=setup(store)
    save_screen(store,ids[0],terminal_failure({'decision':'ERROR','reason':'blocked'}))
    with store._connect() as c:c.execute("update court_screenings set rule_version='old-rule'")
    r=run_pipeline(store,lambda _:pytest.fail('No automatic retry'),poll=False,fetcher=lambda *a,**k:pytest.fail('No automatic retry'))
    assert r['processed']==0 and r['pending']==0


def test_uncertain_screening_becomes_failed_preserving_evidence(tmp_path,monkeypatch):
    import court_pipeline
    store=StoryStore(tmp_path/'db');ids=setup(store)
    doc={'resolved_url':'https://www.ca1.uscourts.gov/opnfiles/0.pdf','pages':['An opinion with enough text to identify the actual dispute.'],'content_hash':'hash','method':'pdf_full_text'}
    monkeypatch.setattr(court_pipeline,'extract_document',lambda *a,**k:doc)
    evidence=[{'page':1,'quote':'An opinion with enough text'}]
    monkeypatch.setattr(court_pipeline,'screen_document',lambda *a:dict(decision='EXCLUDED',reason_code='SCREENING_UNCERTAIN',needs_review=True,evidence=evidence,gist='Partial factual summary',reason='Conflicting subject classification'))
    r=run_pipeline(store,lambda _:pytest.fail('Mock screening'),poll=False)
    assert r['errors']==1 and r['pending']==0
    with store._connect() as c:
        result=json.loads(c.execute('select result_json from court_screenings').fetchone()[0])
        assert c.execute('select summary_status from candidate_notes').fetchone()[0]=='FAILED'
    assert result['evidence']==evidence and result['original_decision']=='EXCLUDED'
    assert result['gist']=='Partial factual summary'


def test_configured_cap_records_unprocessed_as_failed_not_pending(tmp_path):
    store=StoryStore(tmp_path/'db');ids=setup(store,2)
    r=run_pipeline(store,lambda _:pytest.fail('No screening under zero cap'),poll=False,limit=0)
    assert r['pending']==0 and r['errors']==2 and r['processed']==0
    with store._connect() as c:
        assert [r[0] for r in c.execute('select reason_code from court_screenings')]==['RUN_LIMIT_REACHED']*2
    assert run_pipeline(store,lambda _:pytest.fail('No automatic retry'),poll=False)['processed']==0


def test_existing_uncertainty_finalization_is_audited_idempotent_and_preserves_text(tmp_path):
    from court_failures import finalize_existing
    store=StoryStore(tmp_path/'db');ids=setup(store,2)
    original={'decision':'EXCLUDED','reason_code':'INSURANCE_COMPANY_UNCERTAIN','needs_review':True,'evidence':[{'quote':'Original evidence','page':1}],'reason':'No verified company evidence'}
    save_screen(store,ids[0],original)
    store.save_summary(ids[0],gist='Original summary',display_headline='Original headline')
    save_screen(store,ids[1],dict(decision='EXCLUDED',reason_code='NONPRECEDENTIAL',needs_review=False))
    assert finalize_existing(store)['failed_existing_unresolved']==1
    with store._connect() as c:assert c.execute('select decision from court_screenings where item_id=?',(ids[0],)).fetchone()[0]=='EXCLUDED'
    r=finalize_existing(store,apply=True)
    assert r['failed_existing_unresolved']==1 and r['closed_excluded_summaries']==1
    with store._connect() as c:
        saved=json.loads(c.execute('select result_json from court_screenings where item_id=?',(ids[0],)).fetchone()[0])
        assert saved['evidence']==original['evidence']
        assert c.execute('select gist,summary_status from candidate_notes where item_id=?',(ids[0],)).fetchone()[:]==('Original summary','FAILED')
        assert c.execute('select summary_status from candidate_notes where item_id=?',(ids[1],)).fetchone()[0]=='SKIPPED'
        assert c.execute('select count(*) from court_failure_audit').fetchone()[0]==1
    assert finalize_existing(store,apply=True)['failed_existing_unresolved']==0
    assert run_pipeline(store,lambda _:pytest.fail('No retry'),poll=False)['pending']==0
