"""Persistent, leased Phase 2 outbox. Webhook acceptance is never completion."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import uuid

import requests

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
SETUP_TIMEOUT = 1800


class Phase2Jobs:
    def __init__(self, store, config):
        self.store, self.config = store, config
        with store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS phase2_jobs (
                story_id INTEGER PRIMARY KEY REFERENCES story_projects(story_id),
                run_id TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL,
                deadline REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                artifacts TEXT NOT NULL DEFAULT '{}')""")

    def enqueue(self, story_id, retry=False):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            story = db.execute('SELECT status FROM story_projects WHERE story_id=?', (story_id,)).fetchone()
            if not story or story['status'] != 'Phase 2 pending':
                return
            row = db.execute('SELECT state FROM phase2_jobs WHERE story_id=?', (story_id,)).fetchone()
            if row and not (retry and row['state'] == 'failed'):
                return
            db.execute("""INSERT INTO phase2_jobs(story_id,run_id,state,due) VALUES (?,?,'queued',?)
                ON CONFLICT(story_id) DO UPDATE SET run_id=excluded.run_id,state='queued',
                step='queued',attempts=0,due=excluded.due,deadline=0,error=''""",
                (story_id, uuid.uuid4().hex, time.time()))

    def public(self, story_id):
        with self.store._connect() as db:
            row = db.execute('SELECT state,step,attempts,error,due FROM phase2_jobs WHERE story_id=?', (story_id,)).fetchone()
            return dict(row) if row else None

    def token(self, run_id):
        return hmac.new(self.config.session_secret.encode(), ('phase2:'+run_id).encode(), hashlib.sha256).hexdigest()

    def callback(self, story_id, payload):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM phase2_jobs WHERE story_id=?', (story_id,)).fetchone()
            if not row or not hmac.compare_digest(self.token(row['run_id']), payload.completion_token):
                raise PermissionError('Invalid callback capability')
            if row['state'] == 'complete':
                return
            if row['state'] not in {'dispatching', 'waiting', 'queued'} or not row['attempts']:
                raise ValueError('Run is no longer active; retry from the dashboard')
            artifacts = json.loads(row['artifacts'])
            artifacts.update(payload.artifacts)
            if payload.state == 'complete':
                validate_artifacts(artifacts)
                db.execute("""UPDATE story_projects SET status=CASE WHEN status='Phase 2 pending'
                    THEN 'Phase 2' ELSE status END, google_doc_url=?,telegram_thread_url=?,
                    research_folder_url=?,updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE story_id=?""",
                    (artifacts['google_doc_url'], artifacts['telegram_thread_url'], artifacts['research_folder_url'], story_id))
            db.execute('UPDATE phase2_jobs SET state=?,step=?,error=?,artifacts=? WHERE story_id=?',
                       (payload.state, payload.step, payload.error, json.dumps(artifacts), story_id))

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Recover a process death between promotion and outbox insertion.
            for pending in db.execute("SELECT story_id FROM story_projects WHERE status='Phase 2 pending' AND story_id NOT IN (SELECT story_id FROM phase2_jobs)").fetchall():
                db.execute("INSERT INTO phase2_jobs(story_id,run_id,state,due) VALUES (?,?,'queued',?)", (pending['story_id'], uuid.uuid4().hex, now))
            db.execute("UPDATE phase2_jobs SET state='failed',error='Webhook delivery failed after 5 attempts.' WHERE state='queued' AND attempts>=?", (MAX_ATTEMPTS,))
            db.execute("""UPDATE phase2_jobs SET state='failed',error='Setup timed out. Check existing artifacts before retrying.'
                WHERE state='waiting' AND deadline<=?""", (now,))
            row = db.execute("""SELECT * FROM phase2_jobs WHERE
                (state='queued' AND due<=?) OR (state='dispatching' AND due<=?) LIMIT 1""", (now, now)).fetchone()
            if not row:
                return
            row = dict(row)
            if row['attempts'] >= MAX_ATTEMPTS:
                db.execute("UPDATE phase2_jobs SET state='failed',error='Webhook delivery failed after 5 attempts.' WHERE story_id=?", (row['story_id'],))
                return
            db.execute("UPDATE phase2_jobs SET state='dispatching',attempts=attempts+1,due=?,deadline=? WHERE story_id=?", (now+60, now+SETUP_TIMEOUT, row['story_id']))
        story = self.store.get_story(row['story_id'])
        payload = dict(story_id=row['story_id'], item_id=story['origin_item_id'], title=story['title'],
                       source_url=story['origin_url'], idempotency_key=f"stories-phase2-{row['story_id']}",
                       callback_url=f"{self.config.public_origin}/api/v1/stories/{row['story_id']}/phase2/callback",
                       completion_token=self.token(row['run_id']), previous_artifacts=json.loads(row['artifacts']))
        body = json.dumps(payload, separators=(',', ':')).encode()
        state, error = 'waiting', ''
        try:
            if not self.config.hermes_webhook_url or not self.config.hermes_webhook_secret:
                raise ValueError('Hermes webhook is not configured. Configure the URL and signing secret, then retry.')
            response = requests.post(self.config.hermes_webhook_url, data=body, timeout=(5, 20), allow_redirects=False,
                headers={'Content-Type': 'application/json', 'X-Request-ID': row['run_id'],
                         'X-Webhook-Signature': hmac.new(self.config.hermes_webhook_secret.encode(), body, hashlib.sha256).hexdigest()})
            if response.status_code not in {200, 202}:
                state = 'queued' if response.status_code == 429 or response.status_code >= 500 else 'failed'
                error = f'Hermes webhook returned HTTP {response.status_code}.'
            else:
                result = response.json()
                if result.get('status') not in {'accepted', 'duplicate'}:
                    raise ValueError('Unexpected Hermes acknowledgement; setup has not been confirmed.')
        except requests.RequestException:
            state, error = 'queued', 'Hermes could not be reached; delivery will retry.'
        except (ValueError, TypeError):
            state, error = 'failed', 'Hermes configuration or acknowledgement is invalid. Check server configuration and retry.'
        if state == 'queued' and row['attempts'] + 1 >= MAX_ATTEMPTS:
            state = 'failed'
        with self.store._connect() as db:
            db.execute("""UPDATE phase2_jobs SET state=?,step=?,error=?,due=?,deadline=?
                WHERE story_id=? AND run_id=? AND state='dispatching'""",
                (state, 'setting up' if state == 'waiting' else 'delivery', error,
                 now+min(300, 2**(row['attempts']+1)*5), now+SETUP_TIMEOUT, row['story_id'], row['run_id']))

    def run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('Phase 2 worker iteration failed')
            stop.wait(2)


def validate_artifacts(artifacts):
    import re
    patterns = {
        'google_doc_url': r'https://docs\.google\.com/document/d/[\w-]+(?:/[^\s]*)?',
        'research_folder_url': r'https://drive\.google\.com/drive/(?:u/\d+/)?folders/[\w-]+(?:\?[^\s]*)?',
        'images_folder_url': r'https://drive\.google\.com/drive/(?:u/\d+/)?folders/[\w-]+(?:\?[^\s]*)?',
        'telegram_thread_url': r'https://t\.me/(?:c/\d+|[A-Za-z][\w]+)/\d+',
    }
    if any(not re.fullmatch(pattern, artifacts.get(key, '')) for key, pattern in patterns.items()):
        raise ValueError('Completion requires valid Doc, research folder, Images folder and direct topic links')
    if not all(artifacts.get(key) for key in ('story_root_key', 'event_key', 'verified_at')):
        raise ValueError('Completion requires story identity and artifact verification evidence')
