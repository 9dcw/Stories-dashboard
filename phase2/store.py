"""Durable SQLite execution ledger. No network calls inside transactions."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from phase2.registry import OPERATIONS, canonical, dependents, digest, snapshot
from phase2.doc_policy import instructions


class Conflict(ValueError):
    pass


class LostLease(Conflict):
    pass


class WorkflowStore:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=5000')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self.connect() as db:
            db.executescript(Path(__file__).with_name('schema.sql').read_text())
        with self.connect(write=True) as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(story_projects)')}
            if 'workspace_id' not in columns:
                db.execute('ALTER TABLE story_projects ADD COLUMN workspace_id TEXT REFERENCES story_workspaces')
            if 'event_key' not in columns:
                db.execute("ALTER TABLE story_projects ADD COLUMN event_key TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def story(db, story_id):
        row = db.execute('''SELECT p.*, i.raw_url AS origin_url, i.canonical_url, i.headline
                            FROM story_projects p JOIN items i ON i.item_id=p.origin_item_id
                            WHERE p.story_id=?''', (story_id,)).fetchone()
        if not row:
            raise ValueError('Unknown story_id')
        if row['status']=='Deleted':
            raise Conflict('Story was deleted from the dashboard; research files were retained')
        return dict(row)

    def workspace(self, workspace_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM story_workspaces WHERE workspace_id=?', (workspace_id,)).fetchone()
            if not row:
                raise ValueError('Unknown workspace')
            return dict(row)

    def artifacts(self, workspace_id):
        with self.connect() as db:
            return {r['artifact_key']: dict(r) for r in db.execute('SELECT * FROM artifacts WHERE workspace_id=?', (workspace_id,))}

    def narrative_prompt(self):
        with self.connect() as db:
            row=db.execute('SELECT version,prompt FROM narrative_prompt_versions ORDER BY version DESC LIMIT 1').fetchone()
        return dict(row) if row else dict(version=0,prompt=Path(__file__).with_name('prompts').joinpath('narrative.md').read_text())

    def update_narrative_prompt(self,request_id,feedback,author):
        if not feedback.strip() or len(feedback)>8000:
            raise ValueError('Prompt feedback must contain 1–8000 characters')
        with self.connect(write=True) as db:
            old=db.execute('SELECT * FROM narrative_prompt_versions WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['feedback']!=feedback or old['author']!=author: raise Conflict('Prompt feedback identity collision')
                return dict(version=old['version'],prompt=old['prompt'])
            previous=db.execute('SELECT prompt FROM narrative_prompt_versions ORDER BY version DESC LIMIT 1').fetchone()
            base=previous[0] if previous else Path(__file__).with_name('prompts').joinpath('narrative.md').read_text()
            prompt=base+'\nProducer feedback (later feedback takes precedence):\n'+feedback
            if len(prompt)>24000: raise ValueError('Standing prompt is too long; consolidate feedback before adding more')
            cursor=db.execute('INSERT INTO narrative_prompt_versions(request_id,feedback,prompt,author,created_at) VALUES (?,?,?,?,?)',(request_id,feedback,prompt,author,time.time()))
            return dict(version=cursor.lastrowid,prompt=prompt)

    def enqueue(self, story_id, request_id, operation='initial', target='', instruction='', requester='dashboard'):
        if operation not in OPERATIONS | {'initial', 'migrate'}:
            raise ValueError('Unknown registered operation')
        manifest = snapshot()
        if 'narrative' in manifest['steps']:
            prompt=self.narrative_prompt()
            manifest['prompts']['narrative']=prompt['prompt']+'\n'+instructions('narrative')
            manifest['prompt_version']=prompt['version']
        manifest.pop('hash',None)
        manifest['hash']=digest(manifest)
        if operation == 'reconcile_package':
            if not instruction.strip():
                raise ValueError('Package reconciliation requires editorial direction')
            from phase2.registry import package_manifest
            manifest=package_manifest(manifest)
            target='package'
        if operation in OPERATIONS-{'reconcile_package'} and target not in manifest['steps']:
            raise ValueError('Unknown research artifact')
        if operation == 'investigate' and target not in {'evidence', 'visuals'}:
            raise ValueError('Investigations target evidence or visuals')
        now = time.time()
        with self.connect(write=True) as db:
            old = db.execute('SELECT * FROM workflow_runs WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if (old['story_id'], old['operation'], old['target'], old['instruction']) != (story_id, operation, target, instruction):
                    raise Conflict('Idempotency key was used for a different request')
                return old['run_id']
            story = self.story(db, story_id)
            wid = story['workspace_id']
            if operation=='reconcile_package' and not wid:
                raise Conflict('Start the story workspace with /story start before reconciling its documents')
            if not wid:
                # Existing packages must be reconciled by migration, never guessed from titles.
                legacy = story['status'] != 'Phase 2 pending' or any(story[k] for k in ['google_doc_url', 'telegram_thread_url', 'research_folder_url'])
                if legacy:
                    raise Conflict('Existing package requires verified identity migration')
                wid = uuid.uuid4().hex
                root = 'story-' + digest(story['canonical_url'])[:16]
                db.execute('INSERT INTO story_workspaces(workspace_id,root_key,created_at) VALUES (?,?,?)', (wid, root, now))
                db.execute('UPDATE story_projects SET workspace_id=?,event_key=? WHERE story_id=?', (wid, 'event-'+digest(story['canonical_url'])[:16], story_id))
                for key in manifest['artifacts']:
                    db.execute('INSERT INTO artifacts(artifact_id,workspace_id,artifact_key) VALUES (?,?,?)', (uuid.uuid4().hex, wid, key))
            for key in manifest['artifacts']:
                db.execute('INSERT OR IGNORE INTO artifacts(artifact_id,workspace_id,artifact_key) VALUES (?,?,?)',(uuid.uuid4().hex,wid,key))
            workspace = db.execute('SELECT * FROM story_workspaces WHERE workspace_id=?', (wid,)).fetchone()
            if workspace['state'] == 'blocked':
                raise Conflict(workspace['error'] or 'Workspace identity is blocked')
            if operation == 'initial':
                previous = db.execute("SELECT run_id FROM workflow_runs WHERE workspace_id=? AND operation='initial' AND state<>'cancelled' ORDER BY created_at DESC LIMIT 1", (wid,)).fetchone()
                if previous:
                    return previous['run_id']
            active=db.execute("SELECT run_id,operation FROM workflow_runs WHERE workspace_id=? AND state IN ('queued','running','blocked') AND operation<>'projection' ORDER BY created_at LIMIT 1",(wid,)).fetchone()
            if active and target!='narrative' and (operation=='reconcile_package' or active['operation']=='reconcile_package'):
                raise Conflict('Finish or cancel the active run before package work: /story cancel '+active['run_id'])
            if operation == 'create_artifact':
                existing = db.execute('SELECT current_version FROM artifacts WHERE workspace_id=? AND artifact_key=?', (wid, target)).fetchone()
                if existing and existing[0]:
                    return None
            if target=='narrative':
                original=db.execute("SELECT r.manifest FROM artifact_versions v JOIN artifacts a USING(artifact_id) JOIN step_executions s USING(step_id) JOIN workflow_runs r USING(run_id) WHERE a.workspace_id=? AND a.artifact_key='narrative' ORDER BY v.version_number LIMIT 1",(wid,)).fetchone()
                if original:
                    pinned=json.loads(original[0])
                    manifest['prompts']['narrative']=pinned['prompts']['narrative']
                    manifest['prompt_version']=pinned.get('prompt_version',0)
                    manifest.pop('hash',None)
                    manifest['hash']=digest(manifest)
                active=db.execute("SELECT 1 FROM workflow_runs WHERE workspace_id=? AND state IN ('running','queued')",(wid,)).fetchone()
                if active: raise Conflict('Wait for the active story operation to finish')
                # Explicit revise supersedes blocked legacy pipelines without deleting outputs.
                db.execute("UPDATE step_executions SET state='cancelled',fence='' WHERE run_id IN (SELECT run_id FROM workflow_runs WHERE workspace_id=? AND state='blocked') AND state IN ('blocked','queued')",(wid,))
                db.execute("UPDATE workflow_runs SET state='cancelled' WHERE workspace_id=? AND state='blocked'",(wid,))
            run_id = uuid.uuid4().hex
            db.execute('''INSERT INTO workflow_runs(run_id,workspace_id,story_id,request_id,operation,target,instruction,requester,manifest,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (run_id,wid,story_id,request_id,operation,target,instruction,requester,canonical(manifest),now,now))
            keys = ['provision'] + manifest['steps'] if operation == 'initial' else manifest['steps'] if operation=='reconcile_package' else ['provision'] if operation == 'migrate' else ['review' if operation == 'review_artifact' else target]
            if target=='narrative': keys=['provision','narrative']
            for key in keys:
                self._insert_step(db, run_id, key, 1, now)
            self._notify(db, run_id, 'queued', f'Queued {operation} {target}. Run {run_id}')
            return run_id

    @staticmethod
    def _insert_step(db, run_id, key, attempt, due, inputs=None, version=None):
        db.execute('''INSERT INTO step_executions(step_id,run_id,step_key,attempt,due,inputs,output_version)
                      VALUES (?,?,?,?,?,?,?)''', (uuid.uuid4().hex,run_id,key,attempt,due,inputs,version))

    @staticmethod
    def _notify(db, run_id, key, text):
        row = db.execute('SELECT notifications FROM workflow_runs WHERE run_id=?', (run_id,)).fetchone()
        notes = json.loads(row[0])
        if not any(n['key'] == key for n in notes):
            notes.append(dict(key=key,text=text,sent=False,due=0,attempts=0))
            db.execute('UPDATE workflow_runs SET notifications=? WHERE run_id=?', (canonical(notes),run_id))

    def status(self, story_id):
        with self.connect() as db:
            story = self.story(db, story_id)
            if not story['workspace_id']:
                return None
            row = db.execute("SELECT run_id,state,error,operation,target,updated_at,tokens FROM workflow_runs WHERE workspace_id=? AND operation<>'projection' ORDER BY CASE WHEN state IN ('running','queued','blocked') THEN 0 ELSE 1 END, created_at DESC LIMIT 1", (story['workspace_id'],)).fetchone()
            result = dict(row) if row else dict(state='migrated', error='')
            result['can_start_research']=not bool(db.execute("SELECT 1 FROM workflow_runs WHERE workspace_id=? AND operation='initial' AND state<>'cancelled'",(story['workspace_id'],)).fetchone())
            result['artifacts'] = [dict(a) for a in db.execute('SELECT artifact_key,doc_id,current_version,stale,render_state FROM artifacts WHERE workspace_id=? ORDER BY artifact_key', (story['workspace_id'],))]
            result['proposals']=[dict(v) for v in db.execute("SELECT v.version_id,v.proposal_doc_id,a.artifact_key FROM artifact_versions v JOIN artifacts a USING(artifact_id) WHERE a.workspace_id=? AND v.state='candidate'",(story['workspace_id'],))]
            if row:
                steps = [dict(s) for s in db.execute('SELECT step_key,state,attempt,error,output_version FROM step_executions WHERE run_id=? ORDER BY rowid', (row['run_id'],))]
                result['steps'] = steps
                for step in steps:
                    assessment=db.execute('SELECT assessment FROM package_assessments WHERE step_id=(SELECT step_id FROM step_executions WHERE run_id=? AND step_key=? AND attempt=?)',(row['run_id'],step['step_key'],step['attempt'])).fetchone()
                    if assessment:
                        saved=json.loads(assessment[0])
                        step['assessment']={key:saved[key] for key in ['action','reason']}
                conflicts = [s for s in steps if s['state']=='blocked' and s['output_version']]
                result['requires_resolution'] = bool(conflicts)
                result['recovery'] = []
                for step in conflicts:
                    proposal = next((p for p in result['proposals'] if p['version_id']==step['output_version']), {})
                    result['recovery'].append(dict(
                        artifact=step['step_key'], version_id=step['output_version'],
                        proposal_doc_id=proposal.get('proposal_doc_id'), error=step['error'],
                        append_command='/story append '+step['output_version'],
                        cancel_command='/story cancel '+row['run_id'],
                        restart_command='/story start' if row['operation']=='initial' else '/story reconcile '+db.execute('SELECT instruction FROM workflow_runs WHERE run_id=?',(row['run_id'],)).fetchone()[0] if row['operation']=='reconcile_package' else '/story revise '+step['step_key']+' <direction>'))
                active = next((s for s in reversed(steps) if s['state'] in {'running','blocked','failed','queued'}), {})
                result.update(step=active.get('step_key', 'complete'),attempts=active.get('attempt',1))
            return result

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.connect(write=True) as db:
            # Reclaim the same attempt; fence prevents the previous owner committing.
            db.execute("UPDATE step_executions SET state='queued' WHERE state='running' AND lease_until<?", (now,))
            if db.execute("SELECT count(*) FROM step_executions WHERE state='running'").fetchone()[0] >= 2:
                return None
            rows = db.execute('''SELECT s.*,r.workspace_id,r.story_id,r.manifest,r.operation,r.target,r.instruction,r.tokens
                                 FROM step_executions s JOIN workflow_runs r USING(run_id)
                                 JOIN story_workspaces w USING(workspace_id)
                                 WHERE s.state='queued' AND s.due<=? AND r.state IN ('queued','running')
                                 AND w.state<>'blocked' ORDER BY r.created_at,s.rowid''', (now,)).fetchall()
            for item in rows:
                job = dict(item)
                manifest = json.loads(job['manifest'])
                running=db.execute("SELECT count(*) FROM step_executions WHERE state='running'").fetchone()[0]
                if running>=min(2,manifest['budgets']['concurrency']):
                    continue
                key = job['step_key']
                # A workspace-wide resource lock, plus a shared Hub lock during rendering.
                competing = db.execute('''SELECT 1 FROM step_executions s JOIN workflow_runs r USING(run_id)
                                          WHERE r.workspace_id=? AND s.state='running'
                                          AND (s.step_key=? OR s.step_key='provision' OR ?='provision')''', (job['workspace_id'],key,key)).fetchone()
                if competing:
                    continue
                if key not in {'provision','hub'} and not key.startswith('resolve:'):
                    prior = db.execute("SELECT state FROM step_executions WHERE run_id=? AND step_key='provision' ORDER BY attempt DESC LIMIT 1", (job['run_id'],)).fetchone()
                    if prior and prior[0] != 'complete':
                        continue
                    dependencies = manifest['artifacts'][key]['depends_on']
                    if job['operation']=='reconcile_package':
                        # Existing fresh versions must not let descendants outrun this run's assessments.
                        if any(not db.execute("SELECT 1 FROM step_executions WHERE run_id=? AND step_key=? AND state='complete'",(job['run_id'],dep)).fetchone() for dep in dependencies):
                            continue
                    if job['operation'] == 'review_artifact':
                        dependencies = list(dict.fromkeys(manifest['artifacts'][job['target']]['depends_on']+[job['target']]))
                    required = [db.execute('SELECT current_version,stale FROM artifacts WHERE workspace_id=? AND artifact_key=?', (job['workspace_id'],d)).fetchone() for d in dependencies]
                    if any(not r or not r[0] or r[1] for r in required):
                        if job['operation'] != 'initial':
                            self._block(db, job, 'Required upstream artifact is missing or stale')
                        continue
                if job['tokens'] >= manifest['budgets']['run_tokens']:
                    self._block(db, job, 'Run token budget exhausted')
                    continue
                fence = uuid.uuid4().hex
                db.execute("UPDATE step_executions SET state='running',fence=?,lease_until=?,started_at=COALESCE(started_at,?) WHERE step_id=?", (fence,now+90,now,job['step_id']))
                db.execute("UPDATE workflow_runs SET state='running',updated_at=? WHERE run_id=?", (now,job['run_id']))
                job.update(fence=fence,manifest=manifest)
                return job
            return None

    @staticmethod
    def _owned(db, job):
        row = db.execute("SELECT * FROM step_executions WHERE step_id=? AND fence=? AND state='running' AND lease_until>=?", (job['step_id'],job['fence'],time.time())).fetchone()
        if not row:
            raise LostLease('Execution lease was superseded')
        return row

    def heartbeat(self, job):
        with self.connect(write=True) as db:
            self._owned(db, job)
            db.execute('UPDATE step_executions SET lease_until=? WHERE step_id=?', (time.time()+90,job['step_id']))

    def save_inputs(self, job, value):
        with self.connect(write=True) as db:
            row = self._owned(db, job)
            if row['inputs']:
                return json.loads(row['inputs'])
            db.execute('UPDATE step_executions SET inputs=? WHERE step_id=?', (canonical(value),job['step_id']))
            return value

    def assert_current(self,job,inputs):
        with self.connect() as db:
            self._owned(db,job)
            a=db.execute('SELECT * FROM artifacts WHERE workspace_id=? AND artifact_key=?',(job['workspace_id'],job['step_key'])).fetchone()
            self._check_inputs(db,job,inputs,a)

    def checkpoint(self, job, resources=None, topic=None):
        with self.connect(write=True) as db:
            self._owned(db, job)
            if resources is not None:
                db.execute('UPDATE story_workspaces SET resources=? WHERE workspace_id=?', (canonical(resources),job['workspace_id']))
            if topic:
                db.execute('UPDATE story_workspaces SET chat_id=?,thread_id=?,first_message_id=? WHERE workspace_id=?', (*topic,job['workspace_id']))

    def document(self, job, key, doc_id):
        with self.connect(write=True) as db:
            self._owned(db, job)
            db.execute('UPDATE artifacts SET doc_id=? WHERE workspace_id=? AND artifact_key=?', (doc_id,job['workspace_id'],key))

    def candidate(self, job, output, inputs, provenance, usage):
        with self.connect(write=True) as db:
            row = self._owned(db, job)
            if row['output_version']:
                return row['output_version']
            a = db.execute('SELECT * FROM artifacts WHERE workspace_id=? AND artifact_key=?', (job['workspace_id'],job['step_key'])).fetchone()
            self._check_inputs(db,job,inputs,a)
            number = db.execute('SELECT COALESCE(MAX(version_number),0)+1 FROM artifact_versions WHERE artifact_id=?', (a['artifact_id'],)).fetchone()[0]
            vid = uuid.uuid4().hex
            db.execute('INSERT INTO artifact_versions(version_id,artifact_id,version_number,step_id,output,inputs,provenance,created_at) VALUES (?,?,?,?,?,?,?,?)', (vid,a['artifact_id'],number,job['step_id'],canonical(output),canonical(inputs),canonical(provenance),time.time()))
            db.execute('UPDATE step_executions SET output_version=?,usage=? WHERE step_id=?', (vid,canonical(usage),job['step_id']))
            return vid

    def record_usage(self,job,usage):
        with self.connect(write=True) as db:
            row=self._owned(db,job)
            previous=json.loads(row['usage'])
            for key in ['input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens']:
                previous[key]=(previous.get(key) or 0)+(usage.get(key) or 0)
            for key in ['estimated_cost_usd','cost_status','cost_source']:
                previous[key]=usage.get(key)
            db.execute('UPDATE step_executions SET usage=? WHERE step_id=?',(canonical(previous),job['step_id']))
            db.execute('UPDATE workflow_runs SET tokens=tokens+? WHERE run_id=?',(usage.get('total_tokens') or 0,job['run_id']))
            run=db.execute('SELECT tokens FROM workflow_runs WHERE run_id=?',(job['run_id'],)).fetchone()[0]
            if run>job['manifest']['budgets']['run_tokens'] or previous['total_tokens']>job['manifest']['budgets']['step_tokens']:
                # Preserve measured usage even when the limit blocks publication.
                self._block(db,job,'Token budget exhausted')
                return False
            return True

    def recover_pending(self):
        """Recover an explicitly requested promotion interrupted before run insertion."""
        with self.connect() as db:
            legacy=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='phase2_jobs'").fetchone())
            condition=" AND NOT EXISTS (SELECT 1 FROM phase2_jobs j WHERE j.story_id=p.story_id)" if legacy else ''
            rows=db.execute("""SELECT p.story_id FROM story_projects p WHERE p.status='Phase 2 pending'
                               AND NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.story_id=p.story_id)"""+condition).fetchall()
        for row in rows:
            try:
                self.enqueue(row[0],'dashboard:initial:'+str(row[0]))
            except Conflict:
                pass  # Existing links require reconciliation; never auto-adopt them.

    @staticmethod
    def _check_inputs(db,job,inputs,artifact):
        if artifact['current_version'] != inputs['target_version']:
            raise Conflict('Target version changed during execution')
        for key, version in inputs['dependency_versions'].items():
            row=db.execute('SELECT current_version,stale FROM artifacts WHERE workspace_id=? AND artifact_key=?', (job['workspace_id'],key)).fetchone()
            if not row or row[0]!=version or row[1]:
                raise Conflict('Upstream inputs changed during execution')
        decisions = [r[0] for r in db.execute('SELECT decision_id FROM editorial_decisions WHERE workspace_id=? ORDER BY created_at,decision_id', (job['workspace_id'],))]
        if decisions != inputs['decision_ids']:
            raise Conflict('Editorial direction changed during execution')

    def version(self, version_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM artifact_versions WHERE version_id=?', (version_id,)).fetchone()
            if not row:
                raise ValueError('Unknown version')
            return dict(row)

    def publish(self, job, version_id, baseline, revision_id):
        with self.connect(write=True) as db:
            self._owned(db,job)
            v=db.execute('SELECT * FROM artifact_versions WHERE version_id=?', (version_id,)).fetchone()
            a=db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (v['artifact_id'],)).fetchone()
            if a['current_version']==version_id:
                return
            self._check_inputs(db,job,json.loads(v['inputs']),a)
            db.execute("UPDATE artifact_versions SET state='published' WHERE version_id=?", (version_id,))
            db.execute("UPDATE artifacts SET current_version=?,stale=0,render_state='verified',baseline=?,revision_id=? WHERE artifact_id=?", (version_id,baseline,revision_id,a['artifact_id']))
            for key in dependents(job['manifest'],job['step_key']):
                db.execute('UPDATE artifacts SET stale=1 WHERE workspace_id=? AND artifact_key=? AND current_version IS NOT NULL', (job['workspace_id'],key))

    def rendered(self, job, key, baseline, revision):
        with self.connect(write=True) as db:
            self._owned(db,job)
            db.execute("UPDATE artifacts SET baseline=?,revision_id=?,render_state='verified' WHERE workspace_id=? AND artifact_key=?", (baseline,revision,job['workspace_id'],key))

    def finish(self, job, retained=None, inputs=None):
        with self.connect(write=True) as db:
            self._owned(db,job)
            if retained is not None:
                artifact=db.execute('SELECT * FROM artifacts WHERE workspace_id=? AND artifact_key=?',(job['workspace_id'],job['step_key'])).fetchone()
                self._check_inputs(db,job,inputs,artifact)
                db.execute('UPDATE artifacts SET stale=0 WHERE artifact_id=?',(artifact['artifact_id'],))
                db.execute('INSERT INTO package_assessments(step_id,assessment) VALUES (?,?)',(job['step_id'],canonical(retained)))
            now=time.time()
            if job['step_key']=='provision':
                w=db.execute('SELECT * FROM story_workspaces WHERE workspace_id=?', (job['workspace_id'],)).fetchone()
                resources=json.loads(w['resources'])
                primary='narrative' if 'narrative' in job['manifest']['artifacts'] else 'hub'
                hub=db.execute("SELECT doc_id FROM artifacts WHERE workspace_id=? AND artifact_key=?", (job['workspace_id'],primary)).fetchone()[0]
                if not hub or not resources.get('folder') or not resources.get('images') or not w['thread_id']:
                    raise Conflict('Workspace provisioning has not verified all resources')
                db.execute("UPDATE story_workspaces SET state='ready' WHERE workspace_id=?", (job['workspace_id'],))
                topic=f"https://t.me/c/{str(w['chat_id'])[4:]}/{w['first_message_id']}?thread={w['thread_id']}&topic"
                db.execute("UPDATE story_projects SET status=CASE WHEN status='Phase 2 pending' THEN 'Phase 2' ELSE status END,google_doc_url=?,research_folder_url=?,telegram_thread_url=? WHERE workspace_id=?", ('https://docs.google.com/document/d/'+hub+'/edit','https://drive.google.com/drive/folders/'+resources['folder'],topic,job['workspace_id']))
            db.execute("UPDATE step_executions SET state='complete',finished_at=? WHERE step_id=?", (now,job['step_id']))
            self._notify(db,job['run_id'],'step-'+job['step_key'],f"Completed {job['step_key']}"+(' — kept unchanged: '+retained['reason'] if retained else '')+f". Run {job['run_id']}")
            pending=db.execute("SELECT 1 FROM step_executions WHERE run_id=? AND state IN ('queued','running','blocked')", (job['run_id'],)).fetchone()
            if not pending:
                db.execute("UPDATE workflow_runs SET state='completed',updated_at=? WHERE run_id=?", (now,job['run_id']))
                self._notify(db,job['run_id'],'complete','Research operation completed. Ready for human editorial review; no publication approval.')

    @staticmethod
    def _block(db, job, error):
        db.execute("UPDATE step_executions SET state='blocked',error=? WHERE step_id=?", (error,job['step_id']))
        db.execute("UPDATE workflow_runs SET state='blocked',error=?,updated_at=? WHERE run_id=?", (error,time.time(),job['run_id']))
        WorkflowStore._notify(db,job['run_id'],'blocked-'+job['step_id'],error+' Run '+job['run_id'])

    def fail(self, job, error, retryable=False):
        with self.connect(write=True) as db:
            row=self._owned(db,job)
            # Caller supplies a sanitized diagnostic, never an arbitrary provider response.
            error=error[:1000]
            if retryable and job['attempt'] < job['manifest']['budgets']['max_attempts']:
                db.execute("UPDATE step_executions SET state='failed',error=?,finished_at=? WHERE step_id=?", (error,time.time(),job['step_id']))
                self._insert_step(db,job['run_id'],job['step_key'],job['attempt']+1,time.time()+5*2**job['attempt'],row['inputs'],row['output_version'])
            else:
                self._block(db,job,error)

    def retry(self, story_id):
        with self.connect(write=True) as db:
            story=self.story(db,story_id)
            r=db.execute("SELECT * FROM workflow_runs WHERE workspace_id=? AND state='blocked' ORDER BY created_at DESC LIMIT 1", (story['workspace_id'],)).fetchone()
            if not r:
                raise Conflict('No blocked run to retry')
            blocked=db.execute("SELECT * FROM step_executions WHERE run_id=? AND state='blocked'", (r['run_id'],)).fetchall()
            for s in blocked:
                if s['output_version']:
                    raise Conflict('A saved draft requires review. Open the story recovery details, review the proposal, then use /story append '+s['output_version']+' in its Telegram topic to append it and resume. If inputs or direction changed, cancel run '+r['run_id']+' and request fresh work instead.')
                db.execute("UPDATE step_executions SET state='failed' WHERE step_id=?", (s['step_id'],))
                self._insert_step(db,r['run_id'],s['step_key'],s['attempt']+1,time.time())
            db.execute("UPDATE workflow_runs SET state='queued',error='' WHERE run_id=?", (r['run_id'],))
            return r['run_id']

    def decide(self, workspace_id, request_id, kind, text, author, message_id='', supersedes=None):
        if kind not in {'approve_direction','reject_angle','standing_instruction','unresolved_question','human_note'} or not text.strip():
            raise ValueError('Invalid editorial decision')
        with self.connect(write=True) as db:
            old=db.execute('SELECT * FROM editorial_decisions WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if (old['workspace_id'],old['kind'],old['text'],old['author'],old['supersedes']) != (workspace_id,kind,text,author,supersedes):
                    raise Conflict('Decision request ID collision')
                return old['decision_id']
            if supersedes:
                original=db.execute('SELECT kind FROM editorial_decisions WHERE workspace_id=? AND decision_id=?',(workspace_id,supersedes)).fetchone()
                if not original or original['kind']!=kind or db.execute('SELECT 1 FROM editorial_decisions WHERE supersedes=?',(supersedes,)).fetchone():
                    raise Conflict('Decision is unknown or already superseded')
            did=uuid.uuid4().hex
            db.execute('INSERT INTO editorial_decisions(decision_id,workspace_id,request_id,kind,text,author,message_id,created_at,supersedes) VALUES (?,?,?,?,?,?,?,?,?)',(did,workspace_id,request_id,kind,text,author,message_id,time.time(),supersedes))
            db.execute('UPDATE artifacts SET stale=1 WHERE workspace_id=? AND current_version IS NOT NULL',(workspace_id,))
            project=db.execute('SELECT story_id FROM story_projects WHERE workspace_id=? ORDER BY story_id LIMIT 1',(workspace_id,)).fetchone()
            if project:
                run=uuid.uuid4().hex
                now=time.time()
                db.execute('''INSERT INTO workflow_runs(run_id,workspace_id,story_id,request_id,operation,target,instruction,requester,manifest,created_at,updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(run,workspace_id,project[0],'projection:'+did,'projection','hub','',author,canonical(snapshot(legacy=True)),now,now))
                self._insert_step(db,run,'hub',1,now)
            return did

    def by_topic(self, chat_id, thread_id):
        with self.connect() as db:
            rows=db.execute('''SELECT p.story_id,w.workspace_id FROM story_workspaces w JOIN story_projects p USING(workspace_id)
                               WHERE w.chat_id=? AND w.thread_id=? AND w.state='ready' AND p.status<>'Deleted' ORDER BY p.story_id''',(str(chat_id),str(thread_id))).fetchall()
            if len(rows)!=1:
                raise Conflict('Topic has no unique project; supply an explicitly mapped story ID')
            return dict(rows[0])

    def cancel(self,workspace_id,run_id):
        with self.connect(write=True) as db:
            row=db.execute('SELECT state FROM workflow_runs WHERE run_id=? AND workspace_id=?',(run_id,workspace_id)).fetchone()
            if not row or row[0]=='completed':
                raise Conflict('Run is unknown or already completed')
            db.execute("UPDATE workflow_runs SET state='cancelled',updated_at=? WHERE run_id=?",(time.time(),run_id))
            db.execute("UPDATE step_executions SET state='cancelled',fence='' WHERE run_id=? AND state IN ('queued','running','blocked')",(run_id,))
            self._notify(db,run_id,'cancelled','Run cancelled; completed artifacts and proposals retained.')

    def enqueue_resolution(self,story_id,request_id,action,identifier,requester):
        if action not in {'append','import'}:
            raise ValueError('Unknown resolution')
        manifest=snapshot()
        with self.connect(write=True) as db:
            story=self.story(db,story_id)
            previous=db.execute('SELECT * FROM workflow_runs WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if previous['story_id']!=story_id or previous['operation']!=action or previous['instruction']!=identifier:
                    raise Conflict('Resolution request ID collision')
                return previous['run_id']
            if action=='append':
                version=db.execute('''SELECT v.*,a.workspace_id,a.artifact_key FROM artifact_versions v
                                      JOIN artifacts a USING(artifact_id) WHERE v.version_id=?''',(identifier,)).fetchone()
                if not version or version['workspace_id']!=story['workspace_id'] or version['state']!='candidate':
                    raise Conflict('Proposal is not a candidate in this story')
                target=version['artifact_key']
                original=db.execute('SELECT r.manifest FROM workflow_runs r JOIN step_executions s USING(run_id) WHERE s.step_id=?',(version['step_id'],)).fetchone()
                manifest=json.loads(original[0])
            else:
                if identifier not in manifest['steps']:
                    raise ValueError('Unknown artifact to import')
                target=identifier
            run=uuid.uuid4().hex
            db.execute('''INSERT INTO workflow_runs(run_id,workspace_id,story_id,request_id,operation,target,instruction,requester,manifest,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(run,story['workspace_id'],story_id,request_id,action,target,identifier,requester,canonical(manifest),time.time(),time.time()))
            self._insert_step(db,run,'resolve:'+action,1,time.time())
            return run

    def resolve_with_google(self,job,google):
        target=job['target']
        artifact=self.artifacts(job['workspace_id'])[target]
        if job['operation']=='import':
            document=google.read(artifact['doc_id'])
            _,content,_=google.body(document)
            text=google.text(content)
            if len(text)>30000:
                raise Conflict('Human import exceeds bounded context; record a selected excerpt with /story decision note')
            self.decide(job['workspace_id'],'import:'+job['run_id'],'human_note','Explicit human Doc import; unverified factual input:\n'+text,'explicit-import:'+job['run_id'])
            return
        version=self.version(job['instruction'])
        inputs=json.loads(version['inputs'])
        effective=dict(job,step_key=target)
        if artifact['current_version']!=version['version_id']:
            self.assert_current(effective,inputs)
            from phase2.doc_policy import render_document
            text=render_document(google.header(self,job,target)+'\nApproved appended revision: '+version['version_id']+'\n',target,dict(json.loads(version['output']),producer_guidance=inputs.get('producer_guidance','')) if target=='narrative' else json.loads(version['output']))
            baseline,revision=google.append_revision(artifact,text,version['version_id'])
            self.publish(effective,version['version_id'],baseline,revision)
        # Explicit append resolves the original blocked step and resumes its authorized DAG.
        with self.connect(write=True) as db:
            self._owned(db,job)
            original=db.execute('SELECT run_id FROM step_executions WHERE step_id=?',(version['step_id'],)).fetchone()
            db.execute("UPDATE step_executions SET state='complete',finished_at=?,error='' WHERE step_id=? AND state='blocked'",(time.time(),version['step_id']))
            pending=db.execute("SELECT 1 FROM step_executions WHERE run_id=? AND state IN ('queued','running','blocked')",(original[0],)).fetchone()
            db.execute("UPDATE workflow_runs SET state=?,error='',updated_at=? WHERE run_id=? AND state='blocked'",('queued' if pending else 'completed',time.time(),original[0]))
