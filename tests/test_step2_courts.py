import json
from datetime import datetime
from urllib.parse import parse_qs,urlsplit
import pytest
from court_pilot import source_record,install
from court_pipeline import court_candidates,extract_document,nonprecedential
from state_courts import fetch_listing
from step2_courts import additional_specs,specs,illinois_page,pennsylvania_page
from story_store import StoryStore


def ilrow(n=0,date='11/13/2026',court='First District Appellate Court',kind='Opinion',notes='',link=True):
    fields={'CitationNum':'2026 IL App (1st) 123456','FilingDate':date,'Court':court,'Type':kind,'Notes':notes}
    return '<tr><td>'+('<a href="/resources/abcd-1234-'+str(n)+'/file">Example v. Insurer</a>' if link else '')+'</td>'+''.join(f'<td><span id="ctl04_gvDecisions_lbl{k}_{n}">{v}</span></td>' for k,v in fields.items())+'</tr>'

def ilpage(rows,nextpage=False):
    return '<input type="hidden" name="__VIEWSTATE" value="state"><table id="ctl04_gvDecisions">'+rows+'</table>'+('<a href="javascript:__doPostBack(\'ctl00$ctl04$gvDecisions\',\'Page$2\')">2</a>' if nextpage else '')

def parow(kind=1,status='Precedential',court=2):
    return dict(Id=1,CourtType=court,Caption='Policyholder v. Insurer',DispositionDate='2026-11-13',Postings=[dict(PostingTypeId=str(kind),FileName='opinion 123.pdf',PublicationType={'Description':status},PostType={'PostingTypeCode':'Opinion'},FileVersion=2)])

def papage(items,page=1,total=1,pages=1):return json.dumps(dict(Items=items,PageNumber=page,TotalItems=total,TotalPages=pages)).encode()

def test_illinois_scope_dates_and_publication_status():
    rows,_,_=illinois_page(ilpage(ilrow(kind='Rule 23')),'https://www.illinoiscourts.gov')
    assert rows[0]['date'].startswith('2026-11-13')
    assert nonprecedential(['Opinion text'],rows[0]['title'])
    with pytest.raises(ValueError,match='outside First'):
        illinois_page(ilpage(ilrow(court='Second District Appellate Court')),'https://www.illinoiscourts.gov')
    with pytest.raises(ValueError,match='metadata|date, citation'):
        illinois_page(ilpage(ilrow(date='undated')),'https://www.illinoiscourts.gov')

def test_illinois_withdrawn_without_document_is_not_navigation():
    rows,_,_=illinois_page(ilpage(ilrow()+ilrow(1,link=False,notes='Corrected 11/01/26, Withdrawn 11/10/26')),'https://www.illinoiscourts.gov')
    assert len(rows)==1

def test_illinois_posts_pagination_and_stops_after_cutoff():
    s=source_record(specs()[0]);calls=[]
    def fetch(url,**kw):
        calls.append(kw)
        b=ilpage(ilrow(),True) if len(calls)==1 else ilpage(ilrow(1,'10/01/2026'),True)
        return b.encode(),'text/html',url
    body,_,_=fetch_listing(s,fetch,datetime(2026,11,16))
    assert len(court_candidates(s,body.decode()))==1 and len(calls)==2
    form=parse_qs(calls[1]['request_data'].decode())
    assert form['__EVENTARGUMENT']==['Page$2'] and form['__VIEWSTATE']==['state']

def test_illinois_repeated_page_fails_instead_of_partial_success():
    s=source_record(specs()[0])
    with pytest.raises(ValueError,match='repeated'):
        fetch_listing(s,lambda url,**kw:(ilpage(ilrow(),True).encode(),'text/html',url),datetime(2026,11,16))

def test_pennsylvania_paginates_and_keeps_separate_nonprecedential_documents():
    s=source_record(specs()[1]);calls=[]
    def fetch(url,**kw):
        calls.append(url);page=int(parse_qs(urlsplit(url).query)['pageNumber'][0]);r=parow(status='Non-Precedential');r['Id']=page;r['Postings'][0]['FileName']=str(page)+'.pdf'
        return papage([r],page,2,2),'application/json',url
    body,_,_=fetch_listing(s,fetch,datetime(2026,11,16));cs=court_candidates(s,body.decode())
    assert len(cs)==2 and len(calls)==2
    assert all(nonprecedential(['text'],c.headline) for c in cs)

def test_pennsylvania_rejects_other_courts_bad_metadata_and_lists():
    with pytest.raises(ValueError,match='another court'):pennsylvania_page(papage([parow(court=1)]),'https://www.pacourts.us',1)
    with pytest.raises(ValueError,match='publication status'):pennsylvania_page(papage([parow(status='Unknown')]),'https://www.pacourts.us',1)
    rows,_=pennsylvania_page(papage([parow(kind=21)]),'https://www.pacourts.us',1)
    assert rows==[]
    r=parow();r['Postings'][0]['FileName']='../escape.pdf'
    with pytest.raises(ValueError,match='unsafe'):pennsylvania_page(papage([r]),'https://www.pacourts.us',1)

def test_pennsylvania_repeated_pages_fail():
    s=source_record(specs()[1])
    def fetch(url,**kw):
        page=int(parse_qs(urlsplit(url).query)['pageNumber'][0]);return papage([parow()],page,2,2),'application/json',url
    with pytest.raises(ValueError,match='repeated'):fetch_listing(s,fetch,datetime(2026,11,16))

def test_registration_is_scoped_and_idempotent(tmp_path):
    store=StoryStore(tmp_path/'db');a=install(store,specs(),activate=True);b=install(store,specs(),activate=True)
    assert a==b and len(store.list_sources())==2
    assert json.loads(store.list_sources()[0]['collector_config'])['court_ids']==['illappct1']


def test_illinois_other_district_and_superseded_rule_23():
    source = source_record(next(s for s in additional_specs() if s['court_id'] == 'illappct5'))
    court = 'Fifth District Appellate Court'
    listing = ilpage(ilrow(court=court) + ilrow(
        1, court=court, kind='Rule 23', link=False, notes='Withdrawn - Motion to Publish Allowed 11/18/26'))
    body, _, _ = fetch_listing(source, lambda url, **kw: (listing.encode(), 'text/html', url),
                              datetime(2026,11,24))
    assert len(court_candidates(source, body.decode())) == 1


def test_commonwealth_uses_its_own_court_and_document_path():
    source = source_record(next(s for s in additional_specs() if s['court_id'] == 'pacommwct'))
    def fetch(url, **kwargs):
        assert parse_qs(urlsplit(url).query)['courtType'] == ['COMMONWEALTH']
        return papage([parow(kind='mo',court=0)]), 'application/json', url
    body, _, _ = fetch_listing(source,fetch,datetime(2026,11,24))
    candidates = court_candidates(source,body.decode())
    assert len(candidates) == 1 and '/Commonwealth/out/' in candidates[0].url


def test_oklahoma_rss_and_full_text_keep_footnotes_only_from_opinion():
    source = source_record(next(s for s in additional_specs() if s['court_id'] == 'oklacivapp'))
    feed = b'''<rss><channel><title>Oklahoma Court of Civil Appeals Cases</title>
      <item><title>Case opinion</title><pubDate>Tue, 24 Nov 2026 09:00:00 CST</pubDate>
      <link>http://www.oscn.net/applications/oscn/DeliverDocument.asp?CiteID=123</link>
      </item></channel></rss>'''
    body, _, _ = fetch_listing(source,lambda url, **kw: (feed,'application/rss+xml',url))
    candidate, = court_candidates(source,body.decode())
    opinion = ('navigation<!--BEGIN DOCUMENT--><p>' + 'Opinion text. '*12 +
               '</p><p>Footnote details.</p><!--END DOCUMENT-->citation index').encode()
    doc = extract_document(candidate.url,source['collector_config'],
                           lambda url, **kw: (opinion,'text/html',url))
    assert candidate.url.startswith('https://')
    assert doc['method'] == 'html_full_text'
    assert doc['pages'][0].endswith('\nFootnote details.')
    assert 'citation index' not in doc['pages'][0]
