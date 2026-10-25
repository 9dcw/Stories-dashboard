import pytest
from tests.conftest import login
from tests.test_api_dashboard import mutation_headers
from tests.test_phase2_workflow import workflow
from phase2.store import LostLease, Conflict


def test_delete_is_authenticated_idempotent_and_keeps_research(client,api_db):
    _,store,item,_=api_db
    csrf=login(client)
    story=client.post(f'/api/v1/items/{item}/phase2',headers=mutation_headers(csrf)).json()['story']
    sid=story['story_id']
    store.update_story_links(sid,google_doc_url='https://docs.google.com/document/d/retained/edit')
    assert client.delete(f'/api/v1/stories/{sid}').status_code==403
    response=client.delete(f'/api/v1/stories/{sid}',headers=mutation_headers(csrf))
    assert response.status_code==200
    assert response.json()['research_files_preserved']
    assert client.delete(f'/api/v1/stories/{sid}',headers=mutation_headers(csrf)).status_code==200
    assert store.get_story(sid)['google_doc_url'].endswith('/retained/edit')
    assert store.get_story(sid)['status']=='Deleted'
    dashboard=client.get('/api/v1/dashboard').json()
    assert sid not in [s['story_id'] for s in dashboard['stories']]
    assert item not in [c['item_id'] for c in dashboard['recent_candidates']]
    assert client.post(f'/api/v1/stories/{sid}/phase2/retry',headers=mutation_headers(csrf)).status_code==409
    assert client.post(f'/api/v1/items/{item}/phase2',headers=mutation_headers(csrf)).status_code==422


def test_delete_rejects_unknown_and_phase3(client,api_db):
    csrf=login(client);headers=mutation_headers(csrf)
    _,store,item,_=api_db
    sid=store.promote_item(item,'Keep Phase 3','Phase 3')
    assert client.delete('/api/v1/stories/999999',headers=headers).status_code==404
    assert client.delete(f'/api/v1/stories/{sid}',headers=headers).status_code==409
    assert store.get_story(sid)['status']=='Phase 3'


def test_delete_fences_running_research_and_blocks_future_operations(workflow,api_db):
    store,story=workflow
    run=store.enqueue(story,'initial')
    job=store.claim()
    _,legacy,_,_=api_db
    legacy.delete_phase2_story(story)
    with pytest.raises(LostLease): store.heartbeat(job)
    with pytest.raises(Conflict,match='deleted'): store.enqueue(story,'new','revise_artifact','angles')
    assert store.claim() is None
    with store.connect() as db:
        assert db.execute('select state from workflow_runs where run_id=?',(run,)).fetchone()[0]=='cancelled'
