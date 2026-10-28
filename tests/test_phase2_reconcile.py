"""A single editorial request selectively reconciles a durable document package."""
import json

import pytest

from phase2.store import Conflict
from phase2.telegram import parse, dispatch
from tests.test_phase2_workflow import workflow, worker, drain, FakeResearch


def completed(workflow, tmp_path):
    store, story = workflow
    w = worker(store, tmp_path)
    store.enqueue(story, 'initial')
    drain(w)
    return store, story, w


class SelectiveResearch(FakeResearch):
    def run(self, job, inputs, heartbeat):
        result = super().run(job, inputs, heartbeat)
        if job['step_key'] not in {'treatment', 'review'}:
            result['text'] = json.dumps(dict(action='keep', reason='Existing evidence and framing already meet the new direction.'))
        return result


def test_reconcile_keeps_suitable_docs_and_revises_others(workflow, tmp_path):
    store, story, w = completed(workflow, tmp_path)
    wid = store.status(story)['artifacts']
    before = {a['artifact_key']: a['current_version'] for a in wid}
    with store.connect() as db:
        workspace = store.story(db, story)['workspace_id']
    store.decide(workspace, 'direction', 'standing_instruction', 'Lead with policyholder consequences', 'editor')
    drain(w)  # Hub projection only.
    w.research = SelectiveResearch()
    w.google.writes.clear()
    run = store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Apply standing direction across the package')
    drain(w)
    status = store.status(story)
    assert status['run_id'] == run
    assert status['state'] == 'completed'
    assert [k for k, _ in w.research.calls] == ['angles', 'plan', 'evidence', 'visuals', 'treatment', 'review']
    assert [k for k, _, _ in w.google.writes if k != 'hub'] == ['treatment', 'review']
    for artifact in status['artifacts']:
        if artifact['artifact_key'] != 'hub':
            assert not artifact['stale']
        if artifact['artifact_key'] in {'angles', 'plan', 'evidence', 'visuals'}:
            assert artifact['current_version'] == before[artifact['artifact_key']]
    assert sum('assessment' in s for s in status['steps']) == 4
    for key, inputs in w.research.calls:
        assert inputs['instruction'] == 'Apply standing direction across the package'
        assert inputs['current_docs']
        if key != 'review':
            assert key in inputs['current_docs']


def test_package_dependencies_wait_even_when_old_versions_are_fresh(workflow, tmp_path):
    store, story, w = completed(workflow, tmp_path)
    store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Reconsider the angle')
    angles = store.claim()
    assert angles['step_key'] == 'angles'
    assert store.claim() is None  # Plan cannot run against the previous angles.
    w.execute(angles)
    plan = store.claim()
    assert plan['step_key'] == 'plan'
    w.execute(plan)
    evidence, visuals = store.claim(), store.claim()
    assert {evidence['step_key'], visuals['step_key']} == {'evidence', 'visuals'}
    assert store.claim() is None


def test_reconcile_builds_missing_versions_after_partial_initial_run(workflow, tmp_path):
    store, story = workflow
    w = worker(store, tmp_path)
    original = store.enqueue(story, 'initial')
    w.tick()  # Provision the complete set of Docs.
    w.tick()  # Only angles has a structured version.
    with store.connect() as db:
        workspace = store.story(db, story)['workspace_id']
    store.cancel(workspace, original)
    store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Complete and reassess the reporting package')
    drain(w)
    assert store.status(story)['state'] == 'completed'
    assert all(a['current_version'] for a in store.status(story)['artifacts'] if a['artifact_key'] != 'hub')


def test_reconcile_requires_direction_and_exclusive_run(workflow, tmp_path):
    store, story, w = completed(workflow, tmp_path)
    with pytest.raises(ValueError, match='requires editorial direction'):
        store.enqueue(story, 'empty', 'reconcile_package')
    run = store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Review the package')
    assert store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Review the package') == run
    with pytest.raises(Conflict, match='cancel '+run):
        store.enqueue(story, 'other', 'revise_artifact', 'angles', 'Change the angle')
    with store.connect() as db:
        workspace = store.story(db, story)['workspace_id']
    store.cancel(workspace, run)
    targeted = store.enqueue(story, 'targeted', 'revise_artifact', 'angles', 'Change the angle')
    with pytest.raises(Conflict, match='cancel '+targeted):
        store.enqueue(story, 'new-package', 'reconcile_package', instruction='Review all')


@pytest.mark.parametrize('problem', ['policy', 'human_edits', 'changed_revision', 'changed_direction'])
def test_unsafe_keep_cannot_clear_staleness(workflow, tmp_path, problem):
    store, story, w = completed(workflow, tmp_path)
    with store.connect() as db:
        workspace = store.story(db, story)['workspace_id']
    store.decide(workspace, 'direction', 'standing_instruction', 'Reassess the package', 'editor')
    drain(w)
    w.research = SelectiveResearch()
    snapshot = w.google.review_snapshot
    def altered(artifact, key):
        result = snapshot(artifact, key)
        if problem == 'policy':
            result['checks']['issues'] = ['Over word cap']
        if problem == 'human_edits':
            result['managed_matches'] = False
        return result
    w.google.review_snapshot = altered
    original = w.research.run
    def result(job, inputs, heartbeat):
        value = original(job, inputs, heartbeat)
        if problem == 'changed_revision':
            w.google.read = lambda doc_id: {'revisionId': 'new-human-revision'}
        if problem == 'changed_direction':
            store.decide(workspace, 'late-direction', 'standing_instruction', 'New direction', 'editor')
        return value
    w.research.run = result
    store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Apply standing direction')
    w.tick()
    status = store.status(story)
    assert status['state'] == 'blocked'
    assert store.artifacts(workspace)['angles']['stale']
    assert not any('assessment' in s for s in status['steps'])


def test_package_rewrite_preserves_proposal_and_resumes_after_append(workflow, tmp_path):
    store, story, w = completed(workflow, tmp_path)
    run = store.enqueue(story, 'reconcile', 'reconcile_package', instruction='Rewrite framing comprehensively')
    w.google.conflict = True
    w.tick()
    status = store.status(story)
    assert status['state'] == 'blocked'
    assert status['recovery'][0]['restart_command'] == '/story reconcile Rewrite framing comprehensively'
    version = w.google.proposals[-1]
    store.enqueue_resolution(story, 'append', 'append', version, 'editor')
    w.google.conflict = False
    drain(w)
    with store.connect() as db:
        assert db.execute('SELECT state FROM workflow_runs WHERE run_id=?', (run,)).fetchone()[0] == 'completed'
    assert store.version(version)['state'] == 'published'


def test_legacy_reconcile_command_is_replaced_by_narrative_revision():
    with pytest.raises(ValueError):
        parse('/story reconcile Reconsider all docs')
    assert parse('/story revise Reconsider the narrative\nKeep verified evidence.')['instruction']=='Reconsider the narrative\nKeep verified evidence.'
