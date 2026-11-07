"""Cached, verified Drive-folder counts; dashboard requests never call Google."""
from __future__ import annotations

import json
import time

from phase2.registry import canonical

REFRESH_SECONDS=600


def refresh_inventory(store,google,workspace_id):
    # Inventory is supplemental: its failure must never invalidate an upload.
    try: return _refresh_inventory(store,google,workspace_id)
    except Exception: return False


def _refresh_inventory(store,google,workspace_id):
    resources=json.loads(store.workspace(workspace_id)['resources'])
    folder=resources.get('images')
    if not folder: return False
    started=time.time()
    files=[]
    try:
        metadata=google.metadata(folder)
        if metadata.get('trashed') or metadata.get('mimeType')!='application/vnd.google-apps.folder' or resources.get('folder') and resources['folder'] not in metadata.get('parents',[]):
            raise ValueError('Asset folder unavailable')
        page=None
        while True:
            options=dict(q="'"+folder+"' in parents and trashed=false",fields='nextPageToken,files(id,mimeType)',pageSize=1000)
            if page: options['pageToken']=page
            result=google.drive.files().list(**options).execute()
            for item in result.get('files',[]):
                mime=item.get('mimeType','')
                if mime.startswith(('image/','video/')) or mime=='application/pdf':
                    files.append(dict(id=item['id'],mime_type=mime))
            page=result.get('nextPageToken')
            if not page: break
        files=list({f['id']:f for f in files}.values())
        error=''
    except Exception:
        # Retain the previous count; never turn an integration failure into zero.
        error='Drive count could not be refreshed'
    with store.connect(write=True) as db:
        current=db.execute('SELECT * FROM asset_folder_inventory WHERE workspace_id=?',(workspace_id,)).fetchone()
        actual=json.loads(db.execute('SELECT resources FROM story_workspaces WHERE workspace_id=?',(workspace_id,)).fetchone()[0]).get('images')
        if actual!=folder or current and current['attempted_at']>started: return False
        if error and current and current['folder_id']==folder:
            db.execute('UPDATE asset_folder_inventory SET attempted_at=?,error=? WHERE workspace_id=?',(started,error,workspace_id))
        else:
            db.execute('INSERT INTO asset_folder_inventory(workspace_id,folder_id,files,checked_at,attempted_at,error) VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET folder_id=excluded.folder_id,files=excluded.files,checked_at=excluded.checked_at,attempted_at=excluded.attempted_at,error=excluded.error',
                       (workspace_id,folder,canonical(files) if not error else '[]',started if not error else None,started,error))
    return not error


def refresh_due(store,google,limit=10):
    now=time.time()
    with store.connect() as db:
        candidates=db.execute("""SELECT DISTINCT w.workspace_id,w.resources,i.attempted_at,i.folder_id
             FROM story_workspaces w JOIN story_projects p USING(workspace_id)
             LEFT JOIN asset_folder_inventory i USING(workspace_id)
             WHERE p.status IN ('Phase 2','Phase 2 pending','Phase 3')
             ORDER BY COALESCE(i.attempted_at,0)""").fetchall()
    due=[r for r in candidates if json.loads(r['resources']).get('images') and
         (r['folder_id']!=json.loads(r['resources']).get('images') or not r['attempted_at'] or r['attempted_at']<now-REFRESH_SECONDS)]
    for row in due[:limit]: refresh_inventory(store,google,row['workspace_id'])
    return min(len(due),limit)


def progress(db,workspace_id,now=None):
    now=time.time() if now is None else now
    resources=json.loads(db.execute('SELECT resources FROM story_workspaces WHERE workspace_id=?',(workspace_id,)).fetchone()[0])
    folder=resources.get('images')
    result=dict(saved=None,folder_url='https://drive.google.com/drive/folders/'+folder if folder else '',checked_at=None,stale=False,refresh_failed=False,build=None)
    inventory=db.execute('SELECT * FROM asset_folder_inventory WHERE workspace_id=?',(workspace_id,)).fetchone()
    result['refresh_failed']=bool(inventory and inventory['folder_id']==folder and inventory['error'])
    known=bool(inventory and inventory['folder_id']==folder and inventory['checked_at'] is not None)
    ids={f['id'] for f in json.loads(inventory['files'])} if known else set()
    if known:
        result.update(saved=len(ids),checked_at=inventory['checked_at'],stale=bool(inventory['error']) or now-inventory['checked_at']>REFRESH_SECONDS*2)
    run=db.execute("""SELECT s.inputs,r.created_at FROM step_executions s JOIN workflow_runs r USING(run_id)
         WHERE r.workspace_id=? AND r.operation='build_visuals' AND s.inputs IS NOT NULL
         ORDER BY r.created_at DESC,s.attempt DESC LIMIT 1""",(workspace_id,)).fetchone()
    if run:
        current=db.execute("SELECT v.created_at FROM artifacts a JOIN artifact_versions v ON v.version_id=a.current_version WHERE a.workspace_id=? AND a.artifact_key='narrative'",(workspace_id,)).fetchone()
        if current and current[0]>run['created_at']:
            result['build_outdated']=True
            return result
        items=json.loads(run['inputs']).get('items',[])
        if items:
            counts=dict(saved=0,total=len(items),blocked=0,skipped=0)
            for item in items:
                row=db.execute('SELECT state,file_id,error,updated_at FROM visual_builds WHERE workspace_id=? AND visual_id=? AND instruction_hash=?',(workspace_id,item['visual_id'],item['instruction_hash'])).fetchone()
                if row:
                    present=bool(row['file_id']) and (not known or row['updated_at']>inventory['checked_at'] or row['file_id'] in ids)
                    if row['state']=='ready' and present: counts['saved']+=1
                    elif row['state']=='skipped': counts['skipped']+=1
                    elif row['error']: counts['blocked']+=1
            result['build']=counts
    return result
