"""Read-only inventory and explicit, identity-verified legacy adoption."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid

from phase2.registry import canonical, snapshot
from phase2.store import Conflict


def resource_id(url,kind):
    pattern=r'^https://docs\.google\.com/document/d/([\w-]+)(?:/[^\s]*)?$' if kind=='doc' else r'^https://drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)(?:\?[^\s]*)?$'
    match=re.fullmatch(pattern,url or '')
    return match.group(1) if match else None


def inventory(path):
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        projects=[dict(r) for r in db.execute("SELECT p.*,i.raw_url AS origin_url FROM story_projects p JOIN items i ON i.item_id=p.origin_item_id WHERE p.status='Phase 2' ORDER BY p.story_id")]
        groups={}
        for p in projects:
            p.pop('phase2_completion_token_hash',None)
            p['issues']=[]
            for key,kind in [('google_doc_url','doc'),('research_folder_url','folder')]:
                identity=resource_id(p[key],kind)
                if not identity:
                    p['issues'].append('Missing or invalid '+key)
                else:
                    groups.setdefault((key,identity),[]).append(p['story_id'])
            if not p['telegram_thread_url']:
                p['issues'].append('Missing topic mapping')
        for (key,_),ids in groups.items():
            if len(ids)>1:
                for p in projects:
                    if p['story_id'] in ids:
                        p['issues'].append('Shared '+key+' with projects '+','.join(map(str,ids)))
        return {'projects':projects,'count':len(projects),'blocked_count':sum(bool(p['issues']) for p in projects)}
    finally:
        db.close()


def verify_entry(store,entry,google,chat_id):
    required={'story_id','root_key','event_key','folder_id','images_id','legacy_doc_id','chat_id','thread_id','first_message_id'}
    if not required<=set(entry) or any(not str(entry[k]).strip() for k in required):
        raise Conflict('Migration entry lacks explicit identity fields')
    if str(entry['chat_id'])!=str(chat_id):
        raise Conflict('Migration topic is outside the Stories group')
    with store.connect() as db:
        story=store.story(db,entry['story_id'])
    if story['status']!='Phase 2':
        raise Conflict('Migration only adopts existing Phase 2 projects')
    for key,parent,mime in [('folder_id',google.parent_id,'application/vnd.google-apps.folder'),('images_id',entry['folder_id'],'application/vnd.google-apps.folder'),('legacy_doc_id',entry['folder_id'],'application/vnd.google-apps.document')]:
        meta=google.metadata(entry[key])
        if meta.get('trashed') or parent not in meta.get('parents',[]) or meta.get('mimeType')!=mime:
            raise Conflict('Migration resource identity/parent mismatch: '+key)
    document=google.read(entry['legacy_doc_id'])
    _,content,_=google.body(document)
    text=google.text(content)
    if story['origin_url'] not in text:
        raise Conflict('Legacy Doc does not identify the original article; resolve mapping before migration')
    return dict(story=story,revision=document.get('revisionId',''),excerpt=text[:30000])


def adopt(store,entry,verified):
    """Bind only verified identities; provisioning creates new Docs, preserving the original."""
    manifest=snapshot()
    with store.connect(write=True) as db:
        story=store.story(db,entry['story_id'])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='phase2_jobs'").fetchone():
            legacy=db.execute('SELECT state,deadline FROM phase2_jobs WHERE story_id=?',(entry['story_id'],)).fetchone()
            if legacy and (legacy['state'] not in {'complete','failed'} or (legacy['state']=='failed' and legacy['deadline']>time.time())):
                raise Conflict('Drain or expire the legacy setup job before migration')
        if story['workspace_id']:
            workspace=db.execute('SELECT * FROM story_workspaces WHERE workspace_id=?',(story['workspace_id'],)).fetchone()
            resources=json.loads(workspace['resources'])
            if workspace['root_key']!=entry['root_key'] or resources.get('folder')!=entry['folder_id'] or resources.get('legacy_doc')!=entry['legacy_doc_id'] or (workspace['chat_id'],workspace['thread_id'])!=(str(entry['chat_id']),str(entry['thread_id'])):
                raise Conflict('Project already belongs to another workspace')
            return workspace['workspace_id']
        # Distinct canonical roots cannot share a folder or source document.
        for row in db.execute('SELECT root_key,resources FROM story_workspaces'):
            resources=json.loads(row['resources'])
            if row['root_key']!=entry['root_key'] and (resources.get('folder')==entry['folder_id'] or resources.get('legacy_doc')==entry['legacy_doc_id']):
                raise Conflict('Cross-story resource collision')
        existing=db.execute('SELECT * FROM story_workspaces WHERE root_key=?',(entry['root_key'],)).fetchone()
        if existing:
            resources=json.loads(existing['resources'])
            if resources.get('folder')!=entry['folder_id'] or (existing['chat_id'],existing['thread_id'])!=(str(entry['chat_id']),str(entry['thread_id'])):
                raise Conflict('Canonical root has conflicting workspace mappings')
            wid=existing['workspace_id']
        else:
            wid=uuid.uuid4().hex
            resources=dict(folder=entry['folder_id'],images=entry['images_id'],legacy_doc=entry['legacy_doc_id'],legacy_revision=verified['revision'],legacy_excerpt=verified['excerpt'],migration='identity_verified_content_unverified')
            db.execute('''INSERT INTO story_workspaces(workspace_id,root_key,resources,chat_id,thread_id,first_message_id,created_at)
                          VALUES (?,?,?,?,?,?,?)''',(wid,entry['root_key'],canonical(resources),str(entry['chat_id']),str(entry['thread_id']),str(entry['first_message_id']),time.time()))
            for key in manifest['artifacts']:
                db.execute('INSERT INTO artifacts(artifact_id,workspace_id,artifact_key) VALUES (?,?,?)',(uuid.uuid4().hex,wid,key))
        db.execute('UPDATE story_projects SET workspace_id=?,event_key=? WHERE story_id=?',(wid,entry['event_key'],entry['story_id']))
        return wid
