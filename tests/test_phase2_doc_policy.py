import copy
import pytest
from phase2.doc_policy import SPECS, render_document, audit, words
from phase2.google_docs import GoogleDocs
from phase2.orchestrator import Worker
from phase2.registry import snapshot, validate_output
from tests.test_phase2_workflow import sample, workflow, worker, drain


def test_only_editorial_fields_are_published_without_dropping_stored_evidence():
    output=sample('evidence')
    output['sections']=[dict(heading='Retained research log',text='PRIVATE RESEARCH DETAIL '*2000)]
    retained=copy.deepcopy(output)
    rendered=render_document('Navigation\n','evidence',output)
    assert 'PRIVATE RESEARCH DETAIL' not in rendered.text
    assert output==retained
    assert rendered.grid[0]==['Topic','At a glance','Detail / evidence']


def test_cell_excess_requires_visible_exception_and_never_truncates():
    output=sample('evidence')
    output['editorial_rows'][0]['detail']='Important qualification '*100
    with pytest.raises(ValueError,match='cell budget'): render_document('Header\n','evidence',output)
    output['policy_exception']='Material contradictory testimony needs this explanation.'
    rendered=render_document('Header\n','evidence',output)
    assert output['editorial_rows'][0]['detail'] in rendered.text
    assert 'Exception requires human review:' in rendered.text


def test_whole_document_budget_counts_human_notes_and_urls():
    output=sample('evidence')
    rendered=render_document('Header\n','evidence',output)
    check=audit('evidence',rendered.text+' human '*1500,rendered.text,rendered.grid)
    assert 'Whole document exceeds soft cap' in check['issues']
    assert words('Source https://example.org/a-long/source?with=params')==2


@pytest.mark.parametrize('detail',['```json raw output', 'Tool output: raw execution', 'A section\nSearch diary: raw history', '<table>nested</table>'])
def test_excluded_content_rejected_even_when_short(detail):
    output=sample('evidence');output['editorial_rows'][0]['detail']=detail
    with pytest.raises(ValueError,match='not reader-facing'): render_document('Header\n','evidence',output)


def test_major_review_findings_cannot_be_hidden_in_retained_output():
    output=sample('review')
    output['findings']=[dict(severity='major',artifact='evidence',issue='Missing evidence qualification',recommendation='Restore allegation label.')]
    with pytest.raises(ValueError,match='critical/major'): render_document('Header\n','review',output)
    output['editorial_rows'][0]['short']='Missing evidence qualification'
    render_document('Header\n','review',output)


def test_legacy_doc_review_records_actual_policy_failures():
    output=sample('review')
    checks=audit('evidence','Long legacy narrative', 'Long legacy narrative')
    inputs={'current_docs':{'evidence':dict(doc_id='legacy',revision='actual-revision',checks=checks)}}
    Worker.add_review_checks({'step_key':'review'},inputs,output)
    assert output['readiness']=='needs_research'
    assert output['policy_checks']['evidence']['revision']=='actual-revision'
    assert any('Required three-column table missing' in row['detail'] for row in output['editorial_rows'])
    Worker.check_evidence({'step_key':'review'},inputs,output)
    render_document('Header\n','review',output)


def test_native_table_text_is_visible_to_reviews_and_human_edit_detection():
    content=[{'startIndex':2,'endIndex':20,'table':{'tableRows':[{'tableCells':[{'content':[{'paragraph':{'elements':[{'textRun':{'content':'Alleged, not found.\n'}}]}}]}]}]}}]
    assert GoogleDocs.text(content)=='Alleged, not found.\n'
    original=GoogleDocs.fingerprint(content,1,20)
    content[0]['table']['tableRows'][0]['tableCells'][0]['content'][0]['paragraph']['elements'][0]['textRun']['content']='Human edit\n'
    assert GoogleDocs.fingerprint(content,1,20)!=original


def test_snapshot_pins_policy_for_every_artifact():
    manifest=snapshot()
    assert manifest['content_policy_version']=='tables.v1'
    for key in manifest['steps']:
        assert 'editorial_rows' in manifest['schemas'][key]['required']
        assert 'tables.v1' in manifest['prompts'][key]
        validate_output(manifest,key,sample(key))


def test_review_refuses_to_publish_after_a_reviewed_doc_changes(workflow,tmp_path):
    store,story=workflow
    store.enqueue(story,'initial')
    w=worker(store,tmp_path)
    w.google.read=lambda doc_id: {'revisionId':'changed-during-review'}
    drain(w)
    assert store.status(story)['state']=='blocked'
    assert not any(key=='review' for key,_,_ in w.google.writes)


def test_worker_upgrades_legacy_producer_contract_without_changing_research_budget():
    from phase2.registry import apply_content_policy
    manifest=snapshot()
    import copy
    legacy=copy.deepcopy(manifest)
    legacy.pop('content_policy_version')
    for schema in legacy['schemas'].values():
        for name in ['editorial_rows','policy_exception','policy_checks']:
            schema['properties'].pop(name,None)
            if name in schema['required']: schema['required'].remove(name)
    legacy['hash']='legacy-api-hash'
    upgraded=apply_content_policy(legacy)
    assert upgraded['base_workflow_hash']=='legacy-api-hash'
    assert upgraded['budgets']==legacy['budgets']
    assert upgraded['engine']==legacy['engine']
    assert 'editorial_rows' not in legacy['schemas']['review']['required']
    validate_output(upgraded,'review',sample('review'))
