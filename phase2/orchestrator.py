"""Small durable workflow executor. All scheduling policy is pinned in each run."""
from __future__ import annotations

import fcntl
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from jsonschema import ValidationError

from phase2.context import build_context
from phase2.google_docs import doc_url, render_output
from phase2.hermes_adapter import ResearchFailure
from phase2.registry import validate_output
from phase2.store import Conflict, LostLease
from phase2.telegram import notify_one

log=logging.getLogger(__name__)


class Worker:
    def __init__(self,store,research,google,telegram,lock_dir):
        self.store,self.research,self.google,self.telegram=store,research,google,telegram
        self.lock_dir=Path(lock_dir)
        self.lock_dir.mkdir(parents=True,exist_ok=True)

    @contextmanager
    def render_lock(self,job):
        # Serialize external writes and Hub updates across processes, while research runs independently.
        with (self.lock_dir/(job['workspace_id']+'.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            self.store.heartbeat(job)
            yield

    def execute(self,job):
        stop=threading.Event()
        lost=threading.Event()
        def keepalive():
            while not stop.wait(15):
                try:
                    self.store.heartbeat(job)
                except Exception:
                    lost.set()
                    return
        keeper=threading.Thread(target=keepalive,daemon=True)
        keeper.start()
        try:
            if job['step_key']=='provision':
                with self.render_lock(job):
                    self.google.provision(self.store,job,self.telegram)
                self.store.finish(job)
                return
            if job['step_key'].startswith('resolve:'):
                self.resolve(job)
                self.store.finish(job)
                return
            if job['step_key']=='hub':
                with self.render_lock(job):
                    self.update_hub(job)
                self.store.finish(job)
                return
            inputs=self.store.save_inputs(job,build_context(self.store,job))
            with self.store.connect() as db:
                checkpoint=db.execute('SELECT output_version FROM step_executions WHERE step_id=?',(job['step_id'],)).fetchone()[0]
            if checkpoint:
                version=checkpoint
                output=json.loads(self.store.version(version)['output'])
            else:
                result=self.research.run(job,inputs,lambda:self.store.heartbeat(job))
                if not self.store.record_usage(job,result['usage']):
                    return
                try:
                    output=json.loads(result['text'])
                    validate_output(job['manifest'],job['step_key'],output)
                    self.check_evidence(job,inputs,output)
                except (ValueError,ValidationError):
                    # One bounded repair using exactly the same snapshot and schema.
                    repair=dict(inputs,validation_repair='The previous response failed the registered contract. Return a complete valid JSON object; do not invent evidence.',previous_invalid_output=str(result.get('text',''))[:32000])
                    repaired=self.research.run(job,repair,lambda:self.store.heartbeat(job))
                    if not self.store.record_usage(job,repaired['usage']):
                        return
                    for field in ['input_tokens','output_tokens','total_tokens']:
                        repaired['usage'][field]=(repaired['usage'].get(field) or 0)+(result['usage'].get(field) or 0)
                    result=repaired
                    output=json.loads(result['text'])
                    validate_output(job['manifest'],job['step_key'],output)
                    self.check_evidence(job,inputs,output)
                if (result['usage'].get('total_tokens') or 0)>job['manifest']['budgets']['step_tokens']:
                    raise Conflict('Step token budget exhausted, including schema repair')
                provenance=dict(result['provenance'],workflow_hash=job['manifest']['hash'],input_hash=inputs['hash'])
                version=self.store.candidate(job,output,inputs,provenance,result['usage'])
            with self.render_lock(job):
                artifact=self.store.artifacts(job['workspace_id'])[job['step_key']]
                # Recover a crash after publication but before finishing the step/Hub.
                if artifact['current_version']!=version:
                    self.store.assert_current(job,inputs)
                    scope='\nReview scope: '+job['target']+' only\n' if job['operation']=='review_artifact' else ''
                    text=self.google.header(self.store,job,job['step_key'])+'\nVersion: '+version+scope+'\n'+render_output(output)
                    try:
                        baseline,revision=self.google.write(artifact,text,version)
                    except Conflict:
                        self.google.proposal(self.store,job,version,text)
                        raise Conflict('Human edits or annotations require review. Proposed version '+version+'; use /story status or explicitly /story append '+version)
                    self.store.publish(job,version,baseline,revision)
                self.update_hub(job)
            self.store.finish(job)
        except LostLease:
            log.info('Step lease superseded: %s',job['step_id'])
        except Conflict as exc:
            self.fail(job,str(exc))
        except (ValidationError,ValueError):
            self.fail(job,'Structured output failed validation after repair')
        except ResearchFailure as exc:
            self.fail(job,str(exc),retryable=exc.retryable)
        except Exception as exc:
            status=getattr(getattr(exc,'resp',None),'status',None)
            # Do not log exception URLs, payloads, credentials, or untrusted text.
            transient=status in {429,500,502,503,504} or isinstance(exc,(TimeoutError,ConnectionError))
            self.fail(job,'External integration failed ('+type(exc).__name__+'); verify credentials, identity and connectivity',retryable=transient)
        finally:
            stop.set()
            keeper.join(timeout=1)

    def fail(self,job,message,retryable=False):
        try:
            self.store.fail(job,message,retryable)
        except LostLease:
            log.info('Failure arrived after lease was superseded: %s',job['step_id'])

    @staticmethod
    def check_evidence(job,inputs,output):
        if job.get('operation')=='investigate' and inputs.get('current_output'):
            for field in ['sources','claims']:
                current={v['id']:v for v in output[field]}
                if any(current.get(v['id'])!=v for v in inputs['current_output'][field]):
                    raise ValueError('Investigations must preserve prior findings; use a revision to replace them')
        if job['step_key']=='angles' and not any(s['url'].rstrip('/')==inputs['story']['origin_url'].rstrip('/') for s in output['sources']):
            raise ValueError('Angles must begin with and cite the original article')
        if job['step_key']=='treatment':
            evidence=inputs['upstream']['evidence']
            supported={c['id']:c for c in evidence['claims']}
            source_urls={s['id']:s['url'] for s in evidence['sources']}
            for claim in output['claims']:
                if claim['id'] not in supported or claim!=supported[claim['id']]:
                    raise ValueError('Treatment claims must match the evidence ledger')
            for source in output['sources']:
                if source_urls.get(source['id'])!=source['url']:
                    raise ValueError('Treatment sources must come from the evidence ledger')

    def update_hub(self,job):
        hub=self.store.artifacts(job['workspace_id'])['hub']
        try:
            baseline,revision=self.google.write(hub,self.google.hub(self.store,job),'hub-'+job['step_id'])
            self.store.rendered(job,'hub',baseline,revision)
        except Conflict:
            # The research artifact is already verified; an annotated Hub must not roll it back.
            with self.store.connect(write=True) as db:
                self.store._owned(db,job)
                db.execute("UPDATE artifacts SET render_state='conflict' WHERE artifact_id=?",(hub['artifact_id'],))
                self.store._notify(db,job['run_id'],'hub-conflict-'+job['step_id'],'Story Hub has human edits/comments; artifact links remain available through /story status.')

    def resolve(self,job):
        with self.render_lock(job):
            self.store.resolve_with_google(job,self.google)
            self.update_hub(job)

    def tick(self):
        self.store.recover_pending()
        job=self.store.claim()
        if job:
            self.execute(job)
        notify_one(self.store,self.telegram)
        return job is not None

    def run(self,stop):
        with ThreadPoolExecutor(max_workers=2) as pool:
            active=set()
            while not stop.is_set():
                self.store.recover_pending()
                active={f for f in active if not f.done()}
                if len(active)<2:
                    job=self.store.claim()
                    if job:
                        active.add(pool.submit(self.execute,job))
                notify_one(self.store,self.telegram)
                stop.wait(1)
