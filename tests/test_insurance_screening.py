import json

import pytest

from insurance_screening import apply_result, classify_batch, combine, screen_insurance, segments


def test_full_opinion_is_checked_and_underlying_insurer_counts():
    pages = ['The plaintiffs disputed ownership of the property. ' * 700,
             'Acme Insurance paid the claim and sought subrogation from the defendants.']
    seen = []

    def agent(prompt):
        batch = json.loads(prompt.split('INPUT:\n')[1])
        seen.extend(batch)
        results = []
        for part in batch:
            positive = any('Acme Insurance' in p['text'] for p in part['pages'])
            page = part['pages'][-1]
            results.append(dict(key=part['key'], involvement='YES' if positive else 'NO',
                                company='Acme Insurance' if positive else '',
                                reason='Source facts', page=page['page'], quote=page['text'][:100]))
        return json.dumps({'results':results})

    result = screen_insurance({'item_id':1},{'pages':pages},agent)
    assert result['involvement'] == 'YES'
    reconstructed = ''.join(page['text'] for part in seen for page in part['pages'])
    assert reconstructed == ''.join(pages)


@pytest.mark.parametrize('status,decision,reason',[
    ('YES','PASS',''),('NO','EXCLUDED','NO_INSURANCE_COMPANY'),
    ('UNCERTAIN','EXCLUDED','INSURANCE_COMPANY_UNCERTAIN')])
def test_gate_preserves_source_record_and_classifies(status,decision,reason):
    original = {'decision':'PASS','gist':'Original factual summary','reason_code':''}
    result = apply_result(original, combine([{'involvement':status}]))
    assert result['decision'] == decision
    assert result['reason_code'] == reason
    assert result['gist'] == original['gist']
    assert original['decision'] == 'PASS'


@pytest.mark.parametrize('quote,page', [('An invented quotation absent from the opinion',1),
                                       ('The court decides a commercial contract dispute.',2)])
def test_unverified_evidence_cannot_establish_yes_or_no(quote,page):
    batch = segments(1,{'pages':['The court decides a commercial contract dispute.']})
    for status in ['YES','NO']:
        output = {'results':[dict(key='1:0',involvement=status,quote=quote,page=page)]}
        result = classify_batch(batch,lambda _:json.dumps(output))
        assert result['1:0']['involvement'] == 'UNCERTAIN'


def test_missing_and_duplicate_segments_are_rejected():
    batch = segments(1,{'pages':['Some sufficiently detailed case facts.']})
    with pytest.raises(ValueError,match='Incomplete'):
        classify_batch(batch,lambda _:json.dumps({'results':[]}))
    result = dict(key='1:0',involvement='UNCERTAIN')
    with pytest.raises(ValueError,match='repeated'):
        classify_batch(batch,lambda _:json.dumps({'results':[result,result]}))


def test_absence_requires_every_segment_and_uncertainty_stays_excluded():
    assert combine([{'involvement':'NO'},{'involvement':'UNCERTAIN'}])['involvement'] == 'UNCERTAIN'
    assert combine([{'involvement':'NO'},{'involvement':'NO'}])['involvement'] == 'NO'


@pytest.mark.parametrize('involvement,expected',[('YES','PASS'),('NO','EXCLUDED'),('ERROR','EXCLUDED')])
def test_pipeline_applies_insurance_gate(tmp_path,involvement,expected):
    from court_pipeline import run_pipeline
    from story_store import StoryStore
    store = StoryStore(tmp_path/'stories.sqlite3')
    source = store.add_source('Court','B','US','https://www.ca1.uscourts.gov')
    item = store.add_item(source,'Commercial dispute','https://www.ca1.uscourts.gov/opinions/a.pdf')
    text = 'Acme Insurance paid the property claim and sought reimbursement.'
    doc = {'pages':[text],'resolved_url':'https://www.ca1.uscourts.gov/opinions/a.pdf',
           'content_hash':'abc','method':'pdf_full_text'}
    with store._connect() as connection:
        connection.execute('INSERT INTO court_documents VALUES(?,?)',(item.item_id,json.dumps(doc)))

    def agent(prompt):
        if 'INPUT:\n' in prompt:
            if involvement == 'ERROR':
                raise RuntimeError('Model unavailable')
            batch = json.loads(prompt.split('INPUT:\n')[1])
            return json.dumps({'results':[dict(key=batch[0]['key'],involvement=involvement,
                page=1,quote=text,reason='Case facts',company='Acme Insurance')]})
        return json.dumps(dict(subject_exclusion='NONE',needs_review=False,
                               gist='The court resolved a commercial dispute.',display_headline='Commercial dispute resolved'))
    first = run_pipeline(store,agent,poll=False)
    with store._connect() as connection:
        row = connection.execute('SELECT decision FROM court_screenings WHERE item_id=?',(item.item_id,)).fetchone()
    assert row[0] == expected
    assert first['errors'] == 0
    if involvement != 'ERROR':
        second = run_pipeline(store,lambda _:pytest.fail('Already screened'),poll=False)
        assert second['processed'] == 0
    else:
        second = run_pipeline(store,agent,poll=False)
        assert second['processed'] == 1


def test_audit_apply_is_reversible_and_preserves_promoted_items(tmp_path,monkeypatch):
    import sys
    from scripts.screen_insurance_candidates import main
    from story_store import StoryStore
    store = StoryStore(tmp_path/'stories.sqlite3')
    source = store.add_source('Court','B','US','https://www.ca1.uscourts.gov')
    audit = tmp_path/'audit.jsonl'
    records = []
    for index, status in enumerate(['YES','NO','ERROR','PROMOTED']):
        item = store.add_item(source,'A case',f'https://www.ca1.uscourts.gov/opinions/{index}.pdf')
        result = dict(decision='ERROR' if status=='ERROR' else 'PASS',reason='Original screening',
                      gist='Original summary',needs_review=False)
        doc = {'pages':['The court describes the concrete facts of this dispute.']}
        with store._connect() as connection:
            connection.execute('INSERT INTO court_screenings VALUES(?,?,?,?,?,?,?,?)',
                (item.item_id,result['decision'],'',0,json.dumps(result),'hash','court_exclusions_v5','before'))
            if status != 'ERROR':
                connection.execute('INSERT INTO court_documents VALUES(?,?)',(item.item_id,json.dumps(doc)))
            if status == 'PROMOTED':
                connection.execute("UPDATE items SET status='PROMOTED' WHERE item_id=?",(item.item_id,))
        if status in {'YES','NO'}:
            records.append(dict(key=f'{item.item_id}:0',content_hash='hash',policy_version='insurance_company_v1',
                                involvement=status,quote=doc['pages'][0],page=1))
    audit.write_text(''.join(json.dumps(r)+'\n' for r in records))
    monkeypatch.setattr(sys,'argv',['screen','--db',str(store.db_path),'--audit',str(audit),'--apply'])
    main()
    with store._connect() as connection:
        actual = connection.execute('SELECT decision,reason_code FROM court_screenings ORDER BY item_id').fetchall()
    assert [(r[0],r[1]) for r in actual] == [('PASS',''),('EXCLUDED','NO_INSURANCE_COMPANY'),
        ('EXCLUDED','INSURANCE_COMPANY_UNCERTAIN'),('PASS','')]
    assert len(json.loads(audit.with_suffix('.rollback.json').read_text())) == 3
