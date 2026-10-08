import json
from export_stories import export_json
from source_discovery import run_discovery, validate_source
from story_store import StoryStore

RSS='''<rss><channel><item><title>Current enforcement release</title><link>https://agency.gov/release/1</link><pubDate>Tue, 07 Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>'''
HTML='''<html><body><h2>Current order</h2><a href="/order/1">Current order</a><time datetime="2026-10-07">October 7</time></body></html>'''

def test_source_proposals_are_persistent_and_deduplicated(tmp_path):
    s=StoryStore(tmp_path/'db.sqlite3')
    cid,created=s.propose_source(name='Agency',organization='Agency',jurisdiction='NJ',lane='C',proposed_url='https://agency.gov/list/',collector_type='html_list')
    assert created and s.propose_source(name='Again',organization='Agency',jurisdiction='NJ',lane='C',proposed_url='https://agency.gov/list',collector_type='html_list') == (cid,False)
    assert len(s.list_source_candidates('PROPOSED'))==1

def test_validation_uses_existing_collector_and_records_publication_shape():
    candidate={'proposed_url':'https://agency.gov/list','poll_url':'https://agency.gov/list','collector_type':'html_list','collector_config':'{}'}
    result=validate_source(candidate,lambda _:HTML)
    assert result['status']=='VALID' and result['publications_extracted']==1 and result['accepted']==1

def test_rotation_and_discovery_run_are_small_and_persistent(tmp_path):
    s=StoryStore(tmp_path/'db.sqlite3')
    s.seed_discovery_targets([{'name':'A','lane':'A','jurisdiction':'US'},{'name':'B','lane':'B','jurisdiction':'NJ'}])
    result=run_discovery(s,[{'name':'Federal feed','organization':'Agency','jurisdiction':'US','lane':'A','proposed_url':'https://agency.gov/feed','poll_url':'https://agency.gov/feed','collector_type':'rss','publication_type':'RSS'}],fetcher=lambda _:RSS)
    assert result['proposals_created']==1 and s.list_source_candidates()[0]['validation_status']=='VALID'
    assert s.coverage_report()['proposal_status'][0]['count']==1

def test_validation_preserves_inaccessible_and_inactive_outcomes():
    from urllib.error import HTTPError
    candidate={'proposed_url':'https://agency.gov/list','poll_url':'https://agency.gov/list','collector_type':'html_list','collector_config':'{}'}
    def blocked(_): raise HTTPError('https://agency.gov/list', 403, 'blocked', {}, None)
    def missing(_): raise HTTPError('https://agency.gov/list', 404, 'missing', {}, None)
    assert validate_source(candidate, blocked)['status']=='INACCESSIBLE'
    assert validate_source(candidate, missing)['status']=='INACTIVE'

def test_approval_enrollment_is_idempotent_and_uses_existing_registry(tmp_path):
    s=StoryStore(tmp_path/'db.sqlite3')
    cid,_=s.propose_source(name='Agency',organization='Agency',jurisdiction='US',lane='A',proposed_url='https://agency.gov/feed',poll_url='https://agency.gov/feed',collector_type='rss')
    s.update_source_candidate(cid,status='APPROVED')
    first=s.enroll_source_candidate(cid); second=s.enroll_source_candidate(cid)
    assert first==second and len(s.list_sources())==1 and s.get_source_candidate(cid)['status']=='ENROLLED'

def test_export_includes_source_review_and_coverage(tmp_path):
    s=StoryStore(tmp_path/'db.sqlite3')
    s.propose_source(name='Agency',organization='Agency',jurisdiction='US',lane='D',proposed_url='https://agency.gov/news',reason='Public safety')
    out=tmp_path/'stories.json'; export_json(tmp_path/'db.sqlite3',out,generated_at='2026-10-08T00:00:00Z')
    data=json.loads(out.read_text())
    assert data['source_proposals'][0]['name']=='Agency' and 'active_by_lane' in data['coverage']
