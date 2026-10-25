"""Explicit, immutable per-step input snapshots; no conversation-history import."""
import json

from phase2.registry import canonical, digest
from phase2.store import Conflict


def build_context(store, job):
    with store.connect() as db:
        db.execute('BEGIN')
        story=store.story(db,job['story_id'])
        workspace=db.execute('SELECT * FROM story_workspaces WHERE workspace_id=?',(job['workspace_id'],)).fetchone()
        key=job['step_key']
        dependencies=job['manifest']['artifacts'][key]['depends_on'][:]
        if job['operation']=='review_artifact':
            dependencies=list(dict.fromkeys(job['manifest']['artifacts'][job['target']]['depends_on']+[job['target']]))
        upstream={}
        versions={}
        for dep in dependencies:
            row=db.execute('''SELECT a.current_version,a.stale,v.output FROM artifacts a
                              LEFT JOIN artifact_versions v ON v.version_id=a.current_version
                              WHERE a.workspace_id=? AND a.artifact_key=?''',(job['workspace_id'],dep)).fetchone()
            if not row or not row['current_version'] or row['stale']:
                raise Conflict('Required upstream artifact is missing or stale')
            versions[dep]=row['current_version']
            upstream[dep]=json.loads(row['output'])
        current=db.execute('''SELECT a.current_version,v.output FROM artifacts a LEFT JOIN artifact_versions v
                               ON v.version_id=a.current_version WHERE a.workspace_id=? AND a.artifact_key=?''',(job['workspace_id'],key)).fetchone()
        all_decisions=[dict(d) for d in db.execute('SELECT decision_id,kind,text,author,supersedes FROM editorial_decisions WHERE workspace_id=? ORDER BY created_at,decision_id',(job['workspace_id'],))]
        superseded={d['supersedes'] for d in all_decisions if d['supersedes']}
        decisions=[d for d in all_decisions if d['decision_id'] not in superseded]
        value=dict(research_brief=story.get('research_brief', ''),story={k:story[k] for k in ['story_id','title','origin_url','origin_item_id','event_key']},root_key=workspace['root_key'],operation=job['operation'],target=job['target'],instruction=job['instruction'],upstream=upstream,dependency_versions=versions,target_version=current['current_version'],current_output=json.loads(current['output']) if current['output'] else None,decisions=decisions,decision_ids=[d['decision_id'] for d in all_decisions])
        # Legacy excerpts are explicit, bounded, unverified input, never accepted evidence.
        resources=json.loads(workspace['resources'])
        if resources.get('legacy_excerpt'):
            value['legacy_unverified_excerpt']=resources['legacy_excerpt']
        if len(canonical(value))>180000:
            raise Conflict('Input snapshot exceeds context budget; narrow the requested investigation')
        value['hash']=digest(value)
        return value
