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
        if key=='narrative':
            guidance=[story.get('research_brief','')]
            guidance.extend(r[0] for r in db.execute("SELECT instruction FROM workflow_runs WHERE workspace_id=? AND target='narrative' AND operation='revise_artifact' ORDER BY created_at,rowid",(job['workspace_id'],)) if r[0])
            value['producer_guidance']=' | '.join(g for g in guidance if g)
            value['prompt_version']=job['manifest'].get('prompt_version',0)
            value['asset_folder_id']=json.loads(workspace['resources']).get('images','')
            if not current['current_version']:
                value['legacy_material']={}
                legacy_sources={}
                for a in db.execute("SELECT a.artifact_key,v.output FROM artifacts a JOIN artifact_versions v ON v.version_id=a.current_version WHERE a.workspace_id=? AND a.artifact_key<>'narrative'",(job['workspace_id'],)):
                    saved=json.loads(a['output'])
                    value['legacy_material'][a['artifact_key']]={k:saved[k] for k in ['editorial_rows','visuals'] if k in saved}
                    for source in saved.get('sources',[]): legacy_sources.setdefault(source['url'],source)
                    if a['artifact_key']=='evidence': value['legacy_material']['evidence']['claims']=saved.get('claims',[])
                value['legacy_sources']=list(legacy_sources.values())
        # Legacy excerpts are explicit, bounded, unverified input, never accepted evidence.
        resources=json.loads(workspace['resources'])
        if resources.get('legacy_excerpt') and not (key=='narrative' and current['current_version']):
            value['legacy_unverified_excerpt']=resources['legacy_excerpt']
        if len(canonical(value))>(100000 if key=='narrative' else 180000):
            raise Conflict('Input snapshot exceeds context budget; narrow the requested investigation')
        value['hash']=digest(value)
        return value
