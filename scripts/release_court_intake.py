#!/usr/bin/env python3
"""Manually release one immutable image to both the web app and court runner."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import urlopen
import uuid

REQUIRED = ('court_pipeline.py', 'insurance_screening.py', 'court_failures.py',
            'step2_courts.py', 'state_courts.py', 'source_fetch.py', 'story_store.py',
            'scripts/court_daily.py', 'requirements.lock', 'data/court_inventory.json')


def command(argv, *, cwd=None, env=None):
    return subprocess.check_output([str(a) for a in argv], cwd=cwd, env=env, text=True).strip()


def image_id(image):
    return command(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'])


def verify_image(image, revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('revision must be the full tested main commit SHA')
    identity = image_id(image)
    label = command(['docker', 'image', 'inspect', identity, '--format',
                     '{{index .Config.Labels "org.opencontainers.image.revision"}}'])
    if label != revision:
        raise ValueError('Image revision label does not match the tested commit')
    return identity


def inspect_container(container):
    return command(['docker', 'inspect', container, '--format', '{{.Image}}'])


def compose(root, image, domain, *args):
    return command(['docker', 'compose', '--project-directory', root, *args], cwd=root,
                   env=os.environ | {'STORIES_IMAGE': image, 'DOMAIN': domain})


def switch_runner(root, target):
    temporary = root / ('.court-runner-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, root / 'court-runner')
    finally:
        temporary.unlink(missing_ok=True)


def stage_runner(root, identity, revision, runner_python):
    releases = root / 'releases'
    releases.mkdir(exist_ok=True)
    target = releases / (revision + '-' + identity.removeprefix('sha256:')[:12])
    if target.exists():
        record = json.loads((target / 'release-image.json').read_text())
        if record['image_id'] != identity or record['revision'] != revision or any(
            hashlib.sha256((target / name).read_bytes()).hexdigest() != digest
            for name, digest in record['checksums'].items()
        ):
            raise RuntimeError('Existing immutable runner release failed verification')
        return target
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=releases))
    container = None
    try:
        container = command(['docker', 'create', '--network', 'none', '--entrypoint', 'true', identity])
        command(['docker', 'cp', container + ':/app/.', staging])
        if (staging / '.env').exists() or any(staging.rglob('*.sqlite3')):
            raise RuntimeError('Image contains environment secrets or a database')
        for name in REQUIRED:
            if not (staging / name).is_file():
                raise RuntimeError('Runner image is missing ' + name)
        command([runner_python, '-c',
                 'import court_pipeline, insurance_screening, court_failures, step2_courts, pymupdf; '
                 'from state_courts import fetch_listing; from summarize_items import command_summarizer'],
                cwd=staging)
        record = {'revision': revision, 'image_id': identity,
                  'checksums': {name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
                                for name in REQUIRED}}
        (staging / 'release-image.json').write_text(json.dumps(record, indent=2) + '\n')
        staging.rename(target)
        return target
    finally:
        if container:
            command(['docker', 'rm', container])
        if staging.exists():
            shutil.rmtree(staging)


def backup_database(db, directory):
    directory.mkdir(exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='before-manual-release-', suffix='.sqlite3', dir=directory)
    os.close(descriptor)  # mkstemp creates a private 0600 file.
    destination = Path(name)
    try:
        with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(destination) as backup:
                source.backup(backup)
        with sqlite3.connect(destination) as check:
            if check.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RuntimeError('Release backup failed integrity check')
            if check.execute('PRAGMA foreign_key_check').fetchall():
                raise RuntimeError('Release backup has foreign-key violations')
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def wait_healthy(url, timeout=90):
    end = time.monotonic() + timeout
    while True:
        try:
            with urlopen(url, timeout=5) as response:
                if response.status == 200 and json.load(response).get('status') == 'ok':
                    return
        except (OSError, ValueError):
            pass
        if time.monotonic() >= end:
            raise RuntimeError('Health check failed: ' + url)
        time.sleep(3)


def queue_state(db):
    # Read-only: deployment never enrolls sources, reruns classification, or clears records.
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        row = connection.execute('''SELECT COUNT(*) FROM items i JOIN sources s USING(source_id)
            LEFT JOIN court_screenings cs USING(item_id)
            WHERE s.active=1 AND s.lane='B' AND i.status='NEW' AND cs.item_id IS NULL''').fetchone()
        pending = connection.execute('''SELECT COUNT(*) FROM items i JOIN sources s USING(source_id)
            LEFT JOIN candidate_notes n USING(item_id)
            WHERE s.active=1 AND s.lane='B' AND i.status='NEW'
            AND COALESCE(n.summary_status,'PENDING')='PENDING' ''').fetchone()
    return {'unscreened_documents': row[0], 'pending_summaries': pending[0]}


def release(root, image, revision, health_url, runner_python):
    root = Path(root).resolve()
    db = root / 'story-data/stories.sqlite3'
    current = root / 'court-runner'
    parsed = urlsplit(health_url)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('health-url must be an HTTP(S) URL without credentials')
    if not db.is_file() or not current.is_symlink() or not current.resolve().is_dir():
        raise ValueError('Expected an existing database and court-runner symlink')
    if not Path(runner_python).is_file():
        raise ValueError('Runner Python does not exist: ' + runner_python)
    record = {'revision': revision, 'requested_image': image,
              'started_at': datetime.now(timezone.utc).isoformat()}
    log = root / 'releases' / ('release-' + uuid.uuid4().hex + '.json')
    with (root / 'story-data/manual-release.lock').open('a') as release_lock:
        fcntl.flock(release_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = verify_image(image, revision)
        target = stage_runner(root, identity, revision, runner_python)
        record.update(image_id=identity, runner=str(target))
        # Same lock as court_daily.py: never change the runner underneath a screening run.
        with (root / 'story-data/court-daily.lock').open('a') as daily_lock:
            fcntl.flock(daily_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous_runner = current.resolve()
            previous_container = compose(root, identity, parsed.hostname, 'ps', '-q', 'app')
            if not previous_container:
                raise RuntimeError('No running app found; this command releases an existing installation')
            previous_image = inspect_container(previous_container)
            compose(root, identity, parsed.hostname, 'config', '--quiet')
            record.update(previous_runner=str(previous_runner), previous_image_id=previous_image,
                          queue_before=queue_state(db))
            record['backup'] = str(backup_database(db, root / 'backups'))
            switched = False
            try:
                switch_runner(root, target)
                switched = True
                compose(root, identity, parsed.hostname, 'up', '-d', '--no-deps', '--pull', 'never', 'app')
                wait_healthy(health_url)
                deployed = compose(root, identity, parsed.hostname, 'ps', '-q', 'app')
                if inspect_container(deployed) != identity or current.resolve() != target:
                    raise RuntimeError('App and runner are not using the same release')
                record.update(status='COMPLETE', queue_after=queue_state(db))
            except BaseException as error:
                record.update(status='FAILED', error=str(error))
                if switched:
                    try:
                        switch_runner(root, previous_runner)
                        compose(root, previous_image, parsed.hostname, 'up', '-d', '--no-deps', '--pull', 'never', 'app')
                        wait_healthy(health_url)
                        record['rollback'] = 'COMPLETE'
                    except BaseException as rollback_error:
                        record.update(rollback='FAILED', rollback_error=str(rollback_error))
                raise
            finally:
                record['finished_at'] = datetime.now(timezone.utc).isoformat()
                log.write_text(json.dumps(record, indent=2) + '\n')
                print('Release audit: ' + str(log), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/srv/stories-dashboard'))
    parser.add_argument('--image', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--health-url', required=True)
    parser.add_argument('--runner-python', default='/home/david/.hermes/hermes-agent/venv/bin/python3')
    args = parser.parse_args()
    try:
        result = release(args.root, args.image, args.revision, args.health_url, args.runner_python)
    except BlockingIOError:
        parser.exit(1, 'Release stopped: another release or court processing run holds the lock. No running release was interrupted.\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
