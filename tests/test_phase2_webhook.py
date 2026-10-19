from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json

import pytest
import requests

from api.models import Phase2Callback
from api.phase2 import Phase2Jobs, SETUP_TIMEOUT


ARTIFACTS = dict(google_doc_url='https://docs.google.com/document/d/doc123/edit',
    research_folder_url='https://drive.google.com/drive/folders/story123',
    images_folder_url='https://drive.google.com/drive/folders/images123',
    telegram_thread_url='https://t.me/c/123456/789', story_root_key='test-story',
    event_key='test-event', verified_at='2026-10-19T15:00:00Z')


@pytest.fixture
def setup(api_db, runtime_config):
    _, store, item, _ = api_db
    story = store.request_phase2(item, 'Test')
    config = replace(runtime_config, hermes_webhook_url='http://localhost/webhooks/stories-phase2', hermes_webhook_secret='secret')
    jobs = Phase2Jobs(store, config)
    jobs.enqueue(story)
    return jobs, store, story


def accepted(monkeypatch, status=202, data=None):
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(data or {'status': 'accepted'}).encode()
        return response
    monkeypatch.setattr('api.phase2.requests.post', post)
    return calls


def callback(jobs, story, calls, **kwargs):
    token = json.loads(calls[0]['data'])['completion_token']
    jobs.callback(story, Phase2Callback(completion_token=token, **kwargs))


def test_signed_delivery_acceptance_progress_and_verified_completion(setup, monkeypatch):
    jobs, store, story = setup
    calls = accepted(monkeypatch)
    jobs.tick()
    assert jobs.public(story)['state'] == 'waiting'
    assert store.get_story(story)['status'] == 'Phase 2 pending'
    assert calls[0]['headers']['X-Webhook-Signature'] == hmac.new(b'secret', calls[0]['data'], hashlib.sha256).hexdigest()
    callback(jobs, story, calls, state='waiting', step='topic', artifacts={'telegram_thread_url': ARTIFACTS['telegram_thread_url']})
    assert jobs.public(story)['step'] == 'topic'
    callback(jobs, story, calls, state='complete', step='complete', artifacts=ARTIFACTS)
    callback(jobs, story, calls, state='complete', step='complete', artifacts=ARTIFACTS)
    assert jobs.public(story)['state'] == 'complete'
    assert store.get_story(story)['status'] == 'Phase 2'
    jobs.enqueue(story, retry=True)
    assert jobs.public(story)['state'] == 'complete'


def test_transport_failure_retries_same_delivery_and_survives_restart(setup, monkeypatch):
    jobs, store, story = setup
    ids = []
    def timeout(*args, **kwargs):
        ids.append(kwargs['headers']['X-Request-ID'])
        raise requests.Timeout()
    monkeypatch.setattr('api.phase2.requests.post', timeout)
    jobs.tick()
    jobs = Phase2Jobs(store, jobs.config)
    for _ in range(5):
        jobs.tick(now=jobs.public(story)['due']+1)
    assert len(ids) == 5 and len(set(ids)) == 1
    assert jobs.public(story)['state'] == 'failed'
    assert store.get_story(story)['status'] == 'Phase 2 pending'


@pytest.mark.parametrize('status,state', [(401, 'failed'), (404, 'failed'), (429, 'queued'), (503, 'queued')])
def test_http_error_classification(setup, monkeypatch, status, state):
    jobs, _, story = setup
    accepted(monkeypatch, status=status)
    jobs.tick()
    assert jobs.public(story)['state'] == state


def test_timeout_retry_rejects_stale_callback_and_reuses_partial_artifacts(setup, monkeypatch):
    jobs, store, story = setup
    calls = accepted(monkeypatch)
    jobs.tick()
    callback(jobs, story, calls, state='waiting', step='drive', artifacts=ARTIFACTS)
    jobs.tick(now=__import__('time').time()+SETUP_TIMEOUT+1)
    assert jobs.public(story)['state'] == 'failed'
    jobs.enqueue(story, retry=True)
    jobs.tick()
    with pytest.raises(PermissionError):
        callback(jobs, story, calls, state='complete', step='complete', artifacts=ARTIFACTS)
    assert json.loads(calls[1]['data'])['previous_artifacts'] == ARTIFACTS


def test_missing_or_invalid_artifacts_cannot_complete(setup, monkeypatch):
    jobs, store, story = setup
    calls = accepted(monkeypatch)
    jobs.tick()
    for artifacts in ({}, {**ARTIFACTS, 'google_doc_url': 'https://evil.test/document'}):
        with pytest.raises(ValueError):
            callback(jobs, story, calls, state='complete', step='complete', artifacts=artifacts)
    assert store.get_story(story)['status'] == 'Phase 2 pending'


def test_canonical_topic_helper_link_is_accepted(setup, monkeypatch):
    jobs, store, story = setup
    calls = accepted(monkeypatch)
    jobs.tick()
    callback(jobs, story, calls, state='complete', step='complete', artifacts={
        **ARTIFACTS, 'telegram_thread_url': 'https://t.me/c/123456/789?thread=789&topic'})
    assert store.get_story(story)['status'] == 'Phase 2'


def test_concurrent_workers_only_dispatch_once(setup, monkeypatch):
    jobs, _, story = setup
    calls = accepted(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: jobs.tick(), range(4)))
    assert len(calls) == 1
    assert jobs.public(story)['attempts'] == 1


def test_worker_recovers_promotion_without_outbox_row(setup, monkeypatch):
    jobs, store, story = setup
    with store._connect() as db:
        db.execute('DELETE FROM phase2_jobs')
    calls = accepted(monkeypatch)
    jobs.tick()
    assert len(calls) == 1


def test_callback_can_arrive_before_webhook_acknowledgement(setup, monkeypatch):
    jobs, _, story = setup
    def post(*args, **kwargs):
        payload = json.loads(kwargs['data'])
        jobs.callback(story, Phase2Callback(completion_token=payload['completion_token'], state='waiting', step='topic'))
        response = requests.Response()
        response.status_code = 202
        response._content = b'{"status":"accepted"}'
        return response
    monkeypatch.setattr('api.phase2.requests.post', post)
    jobs.tick()
    jobs.tick()
    assert jobs.public(story)['state'] == 'waiting'


def test_same_run_can_report_recovery_before_deadline(setup, monkeypatch):
    jobs, store, story = setup
    calls = accepted(monkeypatch)
    jobs.tick()
    payload = json.loads(calls[0]['data'])
    assert payload['proposed_story_root_key'].startswith('story-')
    callback(jobs, story, calls, state='failed', step='failed', error='Temporary setup error')
    callback(jobs, story, calls, state='waiting', step='verification')
    callback(jobs, story, calls, state='complete', step='complete', artifacts=ARTIFACTS)
    assert store.get_story(story)['status'] == 'Phase 2'


def test_callback_api_authentication_and_body_limit(client, api_db):
    from tests.conftest import login
    csrf = login(client)
    item = api_db[2]
    result = client.post(f'/api/v1/items/{item}/phase2', headers={'Origin': 'http://localhost:8000', 'X-CSRF-Token': csrf})
    story = result.json()['story']['story_id']
    url = f'/api/v1/stories/{story}/phase2/callback'
    assert client.post(url, json={'completion_token': 'wrong', 'state': 'complete', 'step': 'complete'}).status_code == 403
    assert client.post(url, content=b'x'*17000).status_code == 413
    assert client.post('/api/v1/telegram/phase2-complete', json={}).status_code == 405
    assert client.post(f'/api/v1/stories/{story}/phase2/retry').status_code == 403
    dashboard = client.get('/api/v1/dashboard').text
    assert 'completion_token' not in dashboard and 'run_id' not in dashboard
