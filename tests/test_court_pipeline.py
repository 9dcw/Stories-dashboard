import json
from datetime import datetime, timezone

import pymupdf
import pytest

from court_pipeline import (RULE_VERSION,court_candidates,extract_document,nonprecedential,
    read_court_state,run_pipeline,screen_document)
from export_stories import build_read_model
from story_store import StoryStore


def answer(subject='NONE', *, quote='',page=1,review=False):
    return json.dumps({'display_headline':'Court resolves the parties’ dispute on the merits',
        'gist':'The court resolved the dispute based on the record and applicable law. The opinion explains the procedural posture and the parties’ arguments.',
        'subject_exclusion':subject,'needs_review':review,'reason':'Central subject',
        'evidence':[{'page':page,'quote':quote}] if quote else [],'court':'','case_number':'','opinion_date':''})


@pytest.mark.parametrize('notice',['NOT PRECEDENTIAL','non-precedential','UNPUBLISHED',
    'NOT FOR PUBLICATION','NOT RECOMMENDED FOR PUBLICATION','NOT RECOMMENDED FOR FULL-TEXT PUBLICATION','SUMMARY ORDER',
    'NOTE: This disposition is nonprecedential.','This disposition is\nnonprecedential.'])
def test_explicit_status_markers_are_deterministic(notice):
    assert nonprecedential([notice+'\nUnited States Court of Appeals\nCase No. 123'])


def test_quoted_citation_and_uncertain_status_pass():
    assert nonprecedential(['UNITED STATES COURT OF APPEALS\nWe discussed Smith, an unpublished opinion, in resolving this appeal.']) is None
    assert nonprecedential(['This case cites a nonprecedential ruling.']) is None
    assert nonprecedential(['OPINION\nNo publication status is supplied.']) is None


def test_title_status_is_current_item_metadata():
    assert nonprecedential(['Text'],'26-1983: RUTH v. US [ORDER], Nonprecedential')['page']==0


def test_nonprecedential_does_not_spend_agent_call():
    result=screen_document({'headline':'Case'}, {'pages':['UNPUBLISHED\nOpinion']},lambda _:pytest.fail('agent called'))
    assert result['decision']=='EXCLUDED' and result['reason_code']=='NONPRECEDENTIAL'


@pytest.mark.parametrize('subject',['IMMIGRATION','VIOLENT_CRIME'])
def test_subject_exclusion_requires_matching_page_evidence(subject):
    passage='This appeal challenges the petitioner’s removal order and denial of asylum.'
    doc={'pages':[passage]}
    assert screen_document({'headline':'Case'},doc,lambda _:answer(subject,quote=passage))['decision']=='EXCLUDED'
    bad=screen_document({'headline':'Case'},doc,lambda _:answer(subject,quote='A quotation that is absent from the document.'))
    assert bad['decision']=='PASS' and bad['needs_review']
    wrong=screen_document({'headline':'Case'},doc,lambda _:answer(subject,quote=passage,page=2))
    assert wrong['decision']=='PASS'


def test_uncertain_subject_and_noninsurance_cases_pass():
    text='This is a commercial contract dispute between two technology companies.'
    r=screen_document({'headline':'Case'},{'pages':[text]},lambda _:answer('IMMIGRATION',quote=text,review=True))
    assert r['decision']=='PASS' and r['needs_review']
    assert screen_document({'headline':'Case'},{'pages':[text]},lambda _:answer())['decision']=='PASS'


def test_long_opinion_reads_all_chunks_and_disagreement_passes():
    pages=['A commercial dispute about contract interpretation. '*600,'A discussion of immigration appears only in a citation. '*600]
    calls=[]
    def agent(prompt):
        calls.append(prompt)
        return answer('IMMIGRATION',quote=pages[0][:100]) if len(calls)==1 else answer()
    result=screen_document({'headline':'Case'},{'pages':pages},agent)
    assert len(calls)>=2 and '[PAGE 2]' in ''.join(calls)
    assert result['decision']=='PASS' and result['needs_review']


def test_pdf_extraction_reads_beyond_three_pages():
    with pymupdf.open() as pdf:
        for n in range(5):
            p=pdf.new_page();p.insert_text((72,72),f'Page {n+1}: This is the full text of a court opinion with substantive content.')
        body=pdf.tobytes()
    def fetch(url,**kwargs):return body,'application/pdf',url
    doc=extract_document('https://www.ca1.uscourts.gov/opinion.pdf','{}',fetch)
    assert len(doc['pages'])==5 and 'Page 5' in doc['pages'][4]
    assert len(doc['content_hash'])==64


def test_landing_page_resolution_and_external_redirect_rejection():
    def external(url,**kwargs):return b'pdf','application/pdf','https://example.com/a.pdf'
    with pytest.raises(ValueError,match='outside official'):
        extract_document('https://www.ca1.uscourts.gov/a.pdf','{}',external)
    def listing(url,**kwargs):return b'<h1>Search opinions</h1>','text/html',url
    with pytest.raises(ValueError,match='no identifiable'):
        extract_document('https://www.ca1.uscourts.gov/search','{}',listing)


def test_collector_rejects_menus_but_keeps_old_and_undated_documents():
    s={'source_id':1,'collector_type':'html_list','poll_url':'https://www.ca1.uscourts.gov/opinions',
        'collector_config':json.dumps({'opinion_url_pattern':r'/opnfiles/.+\.pdf'})}
    body='<a href="/manual.pdf">Court manual</a><a href="/opnfiles/old.pdf">Case from 2020</a><a href="/opnfiles/old.pdf">PDF</a>'
    docs=court_candidates(s,body)
    assert len(docs)==1 and docs[0].published_at is None


def make_source(store):
    return store.add_source('Court','B','US','https://www.ca1.uscourts.gov',collector_type='html_list',poll_url='https://www.ca1.uscourts.gov/opinions',collector_config=json.dumps({'opinion_url_pattern':r'/opnfiles/.+\.pdf'}))


def test_pipeline_retains_errors_exclusions_and_editorial_status(tmp_path):
    store=StoryStore(tmp_path/'db.sqlite3');sid=make_source(store)
    def fetch(url,**kwargs):
        if url.endswith('/opinions'):return b'<a href="/opnfiles/a.pdf">An opinion</a>','text/html',url
        raise OSError('Fetch failed')
    result=run_pipeline(store,lambda _:answer(),fetcher=fetch)
    assert result['errors']==1 and result['status']=='PARTIAL'
    model=build_read_model(store)
    assert model['recent_candidates'][0]['screening']['decision']=='ERROR'
    assert model['recent_candidates'][0]['status']=='NEW'
    assert model['sources'][0]['court_poll']['documents_found']==1
    assert model['court_pipeline']['last_run']['status']=='PARTIAL'


def test_successful_pipeline_is_idempotent_and_caches_documents(tmp_path):
    store=StoryStore(tmp_path/'db.sqlite3');sid=make_source(store)
    with pymupdf.open() as pdf:
        p=pdf.new_page();p.insert_text((72,72),'NOT PRECEDENTIAL\nThis is the court opinion concerning a contract dispute between parties.\nThe court affirms the judgment below.')
        body=pdf.tobytes()
    def fetch(url,**kwargs):
        if url.endswith('/opinions'):return b'<a href="/opnfiles/a.pdf">An opinion</a>','text/html',url
        return body,'application/pdf',url
    first=run_pipeline(store,lambda _:pytest.fail('agent called'),fetcher=fetch)
    second=run_pipeline(store,lambda _:pytest.fail('agent called'),fetcher=fetch)
    assert first['excluded']==1 and second['processed']==0 and second['new_items']==0
    candidate=build_read_model(store)['recent_candidates'][0]
    assert candidate['screening']['reason_code']=='NONPRECEDENTIAL' and candidate['status']=='NEW'


def test_schedule_dst_and_disabled_status(tmp_path):
    store=StoryStore(tmp_path/'db.sqlite3')
    assert read_court_state(store)[1]['schedule']['next_run_at'] is None
    with store._connect() as c:c.execute("INSERT INTO court_schedule(singleton,enabled) VALUES(1,1)")
    _,state,_=read_court_state(store,datetime(2026,10,24,12,tzinfo=timezone.utc))
    assert state['schedule']['next_run_at']=='2026-10-25T07:00:00+01:00'
