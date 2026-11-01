import json

import pytest

from originating_courts import extract_courts, save_courts, load_courts, activity_report
from story_store import StoryStore


@pytest.mark.parametrize('jurisdiction,text,name', [
    ('US', 'Appeal from the United States District Court\nfor the Northern District of California\nJudge Smith', 'United States District Court For The Northern District Of California'),
    ('US-FL', 'On appeal from the Circuit Court for Seminole County.\nMelissa Souto, Judge.', 'Seminole County Circuit Court'),
    ('US-CA', 'APPEAL from a judgment of the Superior Court of Fresno County. Smith, Judge.', 'Fresno County Superior Court'),
    ('US-TX', 'On Appeal from the 123rd District Court\nHarris County, Texas', 'Harris County 123Rd District Court'),
])
def test_trial_origins_have_explicit_evidence(jurisdiction, text, name):
    result = extract_courts([text], jurisdiction)
    assert result['status'] == 'identified'
    court = result['courts'][0]
    assert court['name'] == name
    assert court['relationships'] == ['reviewed_court','trial_court']
    assert court['evidence'][0]['quote'] in text


def test_supreme_court_tracks_intermediate_and_trial_separately():
    text = ('IN THE SUPREME COURT OF CALIFORNIA\nTHE PEOPLE v. BALL\nS292969\n'
            'Fourth Appellate District, Division Three\nG064274\n'
            'Orange County Superior Court\n20HF0795\nJustice Evans authored the opinion.\n'
            'We cite a case from Fresno County Superior Court.')
    result = extract_courts([text], 'US-CA')
    assert len(result['courts']) == 2
    trial = next(c for c in result['courts'] if c['level'] == 'trial')
    assert trial['relationships'] == ['trial_court']
    intermediate = next(c for c in result['courts'] if c['level'] == 'appellate')
    assert intermediate['relationships'] == ['reviewed_court']


def test_us_and_texas_supreme_review_captions():
    text = 'ON WRIT OF CERTIORARI TO THE UNITED STATES COURT OF APPEALS FOR THE NINTH CIRCUIT'
    court = extract_courts([text])['courts'][0]
    assert court['level'] == 'appellate' and court['relationships'] == ['reviewed_court']
    text = 'On Petition for Review from the\nCourt of Appeals for the Fourteenth District of Texas'
    assert extract_courts([text], 'US-TX')['courts'][0]['level'] == 'appellate'


def test_citations_agencies_and_unknowns_do_not_invent_courts():
    for text in ['We cited an appeal from the United States District Court for the District of Arizona.',
                 'Petition for review of the Board of Immigration Appeals',
                 'Supreme Court of Florida\nOriginal proceeding. No lower court identified.',
                 'We discuss Fresno County Superior Court in another case.']:
        assert extract_courts([text], 'US-CA')['status'] == 'unknown'


def test_canonical_identity_ignores_wrapping_and_case_and_preserves_jurisdiction():
    a = extract_courts(['On appeal from the Circuit Court for Seminole County.'], 'US-FL')['courts'][0]
    b = extract_courts(['ON APPEAL FROM THE CIRCUIT COURT FOR\nSEMINOLE COUNTY.'], 'US-FL')['courts'][0]
    assert a['court_id'] == b['court_id']


def test_cache_backfill_is_separate_from_editorial_state_and_report_is_honest(tmp_path):
    store = StoryStore(tmp_path/'db')
    sid = store.add_source('Ninth Circuit','B','US','https://www.ca9.uscourts.gov')
    pages = ['No. 25-12345\nAppeal from the United States District Court\nfor the District of Arizona']
    for n in [1,2]:
        item = store.add_item(sid, 'Opinion', f'https://www.ca9.uscourts.gov/{n}.pdf').item_id
        with store._connect() as c:
            c.execute('INSERT INTO court_documents(item_id,document_json) VALUES(?,?)', (item,json.dumps({'pages':pages})))
    records = load_courts(store)
    assert len(records) == 2
    report = activity_report(store, records)
    assert report['courts'][0]['document_count'] == 2
    assert report['courts'][0]['identified_appellate_docket_count'] == 1
    with store._connect() as c:
        for _ in range(2):save_courts(c, item, pages, 'US')
        assert c.execute('SELECT count(*) FROM court_origin_identity').fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM items WHERE status='NEW'").fetchone()[0] == 2


def test_florida_supreme_endmatter_preserves_reviewed_court_without_guessing_trial_court():
    pages = ['Supreme Court of Florida'] + ['Opinion discussion'] * 8 + [
        'Application for Review of the Decision of the District Court of Appeal\n'
        'Certified Direct Conflict of Decisions\nSixth District - Case No. 6D2023-0296\n(Collier County)']
    result = extract_courts(pages, 'US-FL')
    assert len(result['courts']) == 1
    court = result['courts'][0]
    assert court['name'] == 'Florida Sixth District Court Of Appeal'
    assert court['relationships'] == ['reviewed_court']
    assert court['evidence'][0]['page'] == 10


def test_issuing_california_appeals_header_is_not_its_own_origin():
    text = ('IN THE COURT OF APPEAL OF CALIFORNIA\nSecond Appellate District\n'
            'APPEAL from a judgment of the Superior Court of Los Angeles County. Smith, Judge.')
    result = extract_courts([text], 'US-CA')
    assert len(result['courts']) == 1
    assert result['courts'][0]['level'] == 'trial'


def test_dc_and_texas_probate_origins():
    text = 'Appeal from the United States District Court\nfor the District of Columbia\n(No. 1:21-cr-00675-1)'
    assert extract_courts([text])['courts'][0]['name'].endswith('District Of Columbia')
    text = 'On Appeal from the Probate Court No. 3\nHarris County, Texas\nTrial Court Case No. 484217'
    assert extract_courts([text], 'US-TX')['courts'][0]['name'] == 'Harris County Probate Court No. 3'
