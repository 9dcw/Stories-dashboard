import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
import yaml

from scripts import release_court_intake as release
from story_store import StoryStore

REVISION = 'a' * 40
IMAGE = 'sha256:' + 'b' * 64
PREVIOUS = 'sha256:' + 'c' * 64


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / 'deployment'
    (root / 'story-data').mkdir(parents=True)
    old = root / 'releases/old'
    old.mkdir(parents=True)
    (root / 'court-runner').symlink_to(old)
    db = root / 'story-data/stories.sqlite3'
    StoryStore(db)
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE human_notes(note TEXT)')
        connection.execute("INSERT INTO human_notes VALUES('Keep my notes')")
    image_files = tmp_path / 'image'
    for name in release.REQUIRED:
        file = image_files / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('test image content for ' + name)
    state = {'image': PREVIOUS, 'calls': [], 'created': 0, 'health_failure': False}

    def command(argv, *, cwd=None, env=None):
        argv = [str(a) for a in argv]
        state['calls'].append((argv, env))
        if argv[:3] == ['docker', 'image', 'inspect']:
            return IMAGE if argv[-1] == '{{.Id}}' else REVISION
        if argv[:2] == ['docker', 'create']:
            assert argv[-1] == IMAGE
            state['created'] += 1
            return 'staging-container'
        if argv[:2] == ['docker', 'cp']:
            shutil.copytree(image_files, Path(argv[-1]), dirs_exist_ok=True)
            return ''
        if argv[:2] == ['docker', 'rm']:
            return ''
        if argv[0] == sys.executable:
            return ''  # Imports are independently tested in the real container smoke check.
        if argv[:2] == ['docker', 'inspect']:
            return state['image']
        if argv[:2] == ['docker', 'compose']:
            action = argv[4:]
            if action[:1] == ['ps']:
                return 'app-container'
            if action[:1] == ['config']:
                return ''
            if action[:1] == ['up']:
                assert '--pull' in action and 'never' in action
                state['image'] = env['STORIES_IMAGE']
                if state['image'] == IMAGE:
                    assert (root / 'court-runner').resolve() != old
                else:
                    assert (root / 'court-runner').resolve() == old
                return ''
        raise AssertionError(argv)

    def health(url, timeout=90):
        if state['health_failure'] and state['image'] == IMAGE:
            # User activity after the release backup must survive an app rollback.
            with sqlite3.connect(db) as connection:
                connection.execute("INSERT INTO human_notes VALUES('New user activity')")
            raise RuntimeError('New app failed its health check')

    monkeypatch.setattr(release, 'command', command)
    monkeypatch.setattr(release, 'wait_healthy', health)
    return root, old, db, image_files, state


def run(root):
    return release.release(root, 'registry.example/app:tested', REVISION,
                           'https://stories.example/api/v1/health', sys.executable)


def test_one_release_updates_both_components_and_preserves_database(deployment):
    root, old, db, image_files, state = deployment
    result = run(root)
    assert result['status'] == 'COMPLETE' and state['image'] == IMAGE
    target = (root / 'court-runner').resolve()
    assert target == Path(result['runner']) and target != old
    assert json.loads((target / 'release-image.json').read_text())['image_id'] == IMAGE
    assert result['queue_after'] == {'unscreened_documents': 0, 'pending_summaries': 0}
    with sqlite3.connect(db) as connection:
        assert connection.execute('select * from human_notes').fetchall() == [('Keep my notes',)]
    with sqlite3.connect(result['backup']) as backup:
        assert backup.execute('select * from human_notes').fetchall() == [('Keep my notes',)]
    assert Path(result['backup']).stat().st_mode & 0o777 == 0o600


def test_failed_health_rolls_back_app_and_runner_without_restoring_database(deployment):
    root, old, db, image_files, state = deployment
    state['health_failure'] = True
    with pytest.raises(RuntimeError, match='health check'):
        run(root)
    assert state['image'] == PREVIOUS and (root / 'court-runner').resolve() == old
    with sqlite3.connect(db) as connection:
        assert len(connection.execute('select * from human_notes').fetchall()) == 2
    record = json.loads(next((root / 'releases').glob('release-*.json')).read_text())
    assert record['status'] == 'FAILED' and record['rollback'] == 'COMPLETE'


def test_running_processor_blocks_activation_and_backup(deployment):
    root, old, db, image_files, state = deployment
    with (root / 'story-data/court-daily.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            run(root)
    assert state['image'] == PREVIOUS and (root / 'court-runner').resolve() == old
    assert not (root / 'backups').exists()
    assert not any(call[0][:2] == ['docker', 'compose'] and 'up' in call[0] for call in state['calls'])


@pytest.mark.parametrize('bad_file', ['insurance_screening.py', 'step2_courts.py', 'court_failures.py'])
def test_missing_image_module_fails_before_touching_live_release(deployment, bad_file):
    root, old, db, image_files, state = deployment
    (image_files / bad_file).unlink()
    with pytest.raises(RuntimeError, match='missing'):
        run(root)
    assert state['image'] == PREVIOUS and (root / 'court-runner').resolve() == old
    assert not (root / 'backups').exists()
    assert any(call[0][:2] == ['docker', 'rm'] for call in state['calls'])


def test_image_revision_must_match_tested_commit(deployment, monkeypatch):
    root, old, db, image_files, state = deployment
    original = release.command
    def mismatch(argv, **kwargs):
        if 'org.opencontainers.image.revision' in str(argv):
            return 'd' * 40
        return original(argv, **kwargs)
    monkeypatch.setattr(release, 'command', mismatch)
    with pytest.raises(ValueError, match='revision label'):
        run(root)
    assert state['created'] == 0 and (root / 'court-runner').resolve() == old


def test_repeated_release_reuses_verified_runner_and_detects_tampering(deployment):
    root, old, db, image_files, state = deployment
    result = run(root)
    assert run(root)['status'] == 'COMPLETE' and state['created'] == 1
    (Path(result['runner']) / 'court_pipeline.py').write_text('unexpected edit')
    with pytest.raises(RuntimeError, match='failed verification'):
        run(root)
    assert state['created'] == 1


def test_production_workflow_is_manual_and_requires_exact_main_ci():
    path = Path(__file__).resolve().parents[1] / '.github/workflows/deploy-contabo.yml'
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow['on']) == {'workflow_dispatch'}
    steps = workflow['jobs']['deploy']['steps']
    gate = next(s for s in steps if s.get('id') == 'tested')['with']['script']
    assert "main.data.commit.sha !== sha" in gate
    assert "workflow_id:'test.yml'" in gate and "run.conclusion === 'success'" in gate
    script = steps[-1]['with']['script']
    assert 'release_court_intake.py' in script
    assert 'finalize_court_failures' not in script and 'configure_state_courts' not in script
