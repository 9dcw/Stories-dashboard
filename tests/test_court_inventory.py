import json
from datetime import datetime

import pytest

from court_inventory import coverage_report
from court_pilot import pilot_sources,source_record,install
from court_pipeline import court_candidates,extract_document,official_url,RULE_VERSION
from story_store import StoryStore


def test_multiple_feeds_count_once_and_disabled_remains_outstanding(tmp_path):
    store=StoryStore(tmp_path/'db')
    specs=pilot_sources(datetime(2026,10,29))
    install(store,specs,activate=True)
    install(store,specs,activate=True)
    assert len(store.list_sources())==2
    s=source_record(specs[0])
    store.add_source('Second Supreme Court feed','B','US','https://www.supremecourt.gov/other',collector_config=s['collector_config'])
    report=coverage_report(store.list_sources())
    assert report['active']==2
    assert report['expansion_active']==2
    assert report['total']==len({c['court_id'] for c in report['courts']})
    assert next(c for c in report['courts'] if c['court_id']=='scotus')['active_feeds']==2
    for s in store.list_sources():store.set_source_active(s['source_id'],False)
    report=coverage_report(store.list_sources())
    assert report['active']==0 and report['outstanding']==report['total']
    assert report['by_status']['disabled']==2


def test_state_urls_require_exact_source_allowlist_and_reject_redirects():
    spec=pilot_sources()[1];source=source_record(spec)
    url='https://www.courts.maine.gov/courts/sjc/lawcourt/2026/26me104.pdf'
    assert not official_url(url)
    assert official_url(url,source['collector_config'])
    assert not official_url(url.replace('.gov/','.gov.evil.example/'),source['collector_config'])
    assert not official_url(url.replace('https:','http:'),source['collector_config'])
    candidates=court_candidates(source,f'<a href="{url}">Example case</a><a href="https://www.courts.maine.gov/manual.pdf">Manual</a>')
    assert len(candidates)==1
    with pytest.raises(ValueError,match='outside official'):
        extract_document(url,source['collector_config'],lambda *a,**k:(b'pdf','application/pdf','https://example.com/a.pdf'))


def test_supreme_court_patterns_exclude_other_publication_types():
    spec=pilot_sources(datetime(2026,10,29))[0]
    source=source_record(spec)
    body='<a href="/opinions/26pdf/case.pdf">Case opinion</a><a href="/orders/courtorders/order.pdf">Order list</a><a href="/publicinfo/manual.pdf">Manual</a>'
    assert len(court_candidates(source,body))==1
    assert spec['url'].endswith('/26')
    assert pilot_sources(datetime(2027,1,1))[0]['url'].endswith('/26')
    assert RULE_VERSION=='court_exclusions_v2'


def test_term_adapter_changes_listing_only(monkeypatch):
    import source_fetch
    from datetime import datetime as RealDateTime
    class October(RealDateTime):
        @classmethod
        def now(cls,tz=None):return cls(2027,10,5,tzinfo=tz)
    monkeypatch.setattr(source_fetch,'datetime',October)
    policy=source_fetch.FetchPolicy(endpoint_adapter='scotus_current_term')
    assert source_fetch._adapt_endpoint('https://www.supremecourt.gov/opinions/slipopinion/26',policy).endswith('/27')
    pdf='https://www.supremecourt.gov/opinions/26pdf/example.pdf'
    assert source_fetch._adapt_endpoint(pdf,policy)==pdf
