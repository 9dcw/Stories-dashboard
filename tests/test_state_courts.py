import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs,urlsplit

import pytest

from court_pilot import source_record,install
from court_pipeline import court_candidates,RULE_VERSION
from state_court_sources import sources,pending_new_york
from state_courts import fetch_listing,california_page,texas_day,texas_dates
from story_store import StoryStore


def florida_row(i):
    return {'content':{'fields':{'case_number':str(i),'case_style':'Example case','opinion':{'uri':f'/content/download/{i}/opinion/opinion.pdf'},'disposition_date':{'date':{'date':'2026-10-20 00:00:00'}}}}}


def test_florida_fetches_all_pages_without_subject_or_procedural_filter():
    source=source_record(sources()[0]);calls=[]
    def fetch(url,**kwargs):
        calls.append(url);q=parse_qs(urlsplit(url).query)
        assert 'type[]' not in q
        offset=int(q['offset'][0]);data=dict(searchResults=[florida_row(offset+1)],totalCount=2)
        return json.dumps(data).encode(),'application/json',url
    body,_,_=fetch_listing(source,fetch,datetime(2026,10,29))
    assert len(court_candidates(source,body.decode()))==2 and len(calls)==2


def test_florida_pagination_cannot_silently_truncate():
    source=source_record(sources()[0])
    def fetch(url,**kwargs):return json.dumps(dict(searchResults=[],totalCount=1)).encode(),'application/json',url
    with pytest.raises(ValueError,match='pagination ended'):fetch_listing(source,fetch)


def test_california_extracts_title_date_and_pdf_not_navigation():
    body='<div class="result-excerpt"><span class="result-excerpt__brow-secondary">October 28, 2026</span><h2>Contract dispute</h2><a href="/opinions/documents/A123.PDF">PDF</a><a href="/manual.pdf">Manual</a></div><a rel="next" href="?page=1">Next</a>'
    rows,nexts=california_page(body,'https://courts.ca.gov/opinions/publishedcitable-opinions')
    assert rows[0]['title']=='Contract dispute' and rows[0]['date'].startswith('2026-10-28')
    assert len(rows)==1 and nexts[0].endswith('?page=1')


def test_texas_keeps_opinions_and_repairs_only_court_parameter():
    body='<h1>Released Orders &amp; Opinions</h1><table><tr class="rgRow"><td><a href="Case.aspx?cn=01-26-123-CV">01-26-123-CV</a><table><tr><td><a href="SearchMedia.aspx?MediaVersionID=version&amp;MediaID=media&amp;coa=&quot; + bad + &quot;&amp;DT=Opinion">PDF</a></td></tr></table></td><td class="caseStyle">Example insurance dispute</td></tr><tr class="rgRow"><td><a href="SearchMedia.aspx?MediaVersionID=v&amp;MediaID=m&amp;DT=Order">Order</a></td></tr></table>'
    rows=texas_day(body,'https://search.txcourts.gov/Docket.aspx','coa01','2026-10-29')
    assert len(rows)==1 and 'Example insurance dispute' in rows[0]['title']
    assert parse_qs(urlsplit(rows[0]['url']).query)==dict(MediaVersionID=['version'],MediaID=['media'],coa=['coa01'],DT=['Opinion'])


def test_state_registration_is_idempotent_and_district_ids_are_independent(tmp_path):
    store=StoryStore(tmp_path/'db')
    specs=sources();install(store,specs,activate=True);install(store,specs,activate=True)
    assert len(store.list_sources())==33
    assert len({s['court_id'] for s in specs})==31
    assert all('courtlistener' not in json.dumps(s).lower() for s in specs+pending_new_york())
    assert RULE_VERSION=='court_exclusions_v2'
    assert len(pending_new_york())==7


def test_criminal_high_court_uses_separate_handdown_case_layout():
    body='<h1>Hand Down List</h1><div class="rowx"><div class="col1"><a href="Case.aspx?cn=AP-77">AP-77</a></div><div class="col2">DEFENDANT FROM COUNTY</div><div><div class="col2"><a href="SearchMedia.aspx?MediaVersionID=v&amp;MediaID=m&amp;coa=coscca&amp;DT=OPINION">PDF</a></div></div></div>'
    rows=texas_day(body,'https://search.txcourts.gov/handdown.aspx','coscca','2026-10-22')
    assert len(rows)==1 and rows[0]['title']=='AP-77: DEFENDANT FROM COUNTY'


def test_bounded_source_run_does_not_poll_or_screen_other_sources(tmp_path):
    from court_pipeline import run_pipeline
    store=StoryStore(tmp_path/'db')
    ids=[]
    for n in range(2):
        sid=store.add_source(f'Court {n}','B','US',f'https://www.ca{n+1}.uscourts.gov',collector_type='html_list',poll_url=f'https://www.ca{n+1}.uscourts.gov/opinions',collector_config=json.dumps({'opinion_url_pattern':r'/opnfiles/.+\.pdf'}))
        ids.append(sid);store.add_item(sid,'Existing opinion',f'https://www.ca{n+1}.uscourts.gov/opnfiles/a.pdf')
    calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        if url.endswith('/opinions'):return b'<h1>Opinions</h1>','text/html',url
        raise OSError('Document needs retry')
    result=run_pipeline(store,lambda _:pytest.fail('No full text'),source_ids=[ids[0]],fetcher=fetch)
    assert result['sources_checked']==1 and result['processed']==1
    assert all('ca2' not in url for url in calls)
    with store._connect() as c:assert c.execute('SELECT COUNT(*) FROM court_screenings').fetchone()[0]==1
