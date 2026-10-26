"""Explicit editorial commands and Telegram transport; no conversational guessing."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.request

from phase2.registry import canonical
from phase2.store import Conflict

COMMANDS={'revise':'revise_artifact','investigate':'investigate','refresh':'refresh_artifact','review':'review_artifact','create':'create_artifact'}
DECISIONS={'approve':'approve_direction','reject':'reject_angle','instruction':'standing_instruction','question':'unresolved_question','note':'human_note'}


def signature(secret,timestamp,body):
    return hmac.new(secret.encode(),str(timestamp).encode()+b'\n'+body,hashlib.sha256).hexdigest()


def parse(text):
    words=text.strip().split(maxsplit=3)
    if not words or words[0].split('@')[0].lower()!='/story':
        raise ValueError('Use an explicit /story command')
    if len(words)==2 and words[1] in {'status','retry','start'}:
        return dict(action=words[1])
    if len(words)>=3 and words[1]=='reconcile':
        return dict(action='operate',operation='reconcile_package',target='package',instruction=text.strip().split(maxsplit=2)[2])
    if len(words)>=3 and words[1] in COMMANDS:
        instruction=words[3] if len(words)>3 else ''
        if words[1] in {'revise','investigate'} and not instruction.strip():
            raise ValueError('A scoped revision or investigation instruction is required')
        return dict(action='operate',operation=COMMANDS[words[1]],target=words[2],instruction=instruction)
    if len(words)==4 and words[1]=='decision' and words[2] in DECISIONS:
        return dict(action='decision',kind=DECISIONS[words[2]],text=words[3])
    if len(words)==4 and words[1]=='supersede':
        return dict(action='supersede',identifier=words[2],text=words[3])
    if len(words)==3 and words[1] in {'cancel','append','import','read'}:
        return dict(action=words[1],identifier=words[2])
    raise ValueError('Use /story status, reconcile <direction> for the whole package, revise|investigate|refresh|review|create <artifact> [instruction], decision approve|reject|instruction|question|note <text>, retry, cancel <run>, append <proposal>, or import <artifact>')


def dispatch(store,event,allowed_users,chat_id):
    if str(event['user_id']) not in allowed_users or str(event['chat_id'])!=str(chat_id) or not event.get('thread_id'):
        raise PermissionError('Unauthorized editorial event')
    mapping=store.by_topic(event['chat_id'],event['thread_id'])
    command=parse(event['text'])
    request_id='telegram:'+str(event['chat_id'])+':'+str(event['message_id'])
    requester='telegram:'+str(event['user_id'])
    action=command.pop('action')
    if action=='status':
        return store.status(mapping['story_id'])
    if action=='start':
        return {'run_id':store.enqueue(mapping['story_id'],request_id,requester=requester)}
    if action=='read':
        key=command['identifier']
        artifact=store.artifacts(mapping['workspace_id']).get(key)
        if not artifact or not artifact['current_version']:
            raise Conflict('Artifact has no published structured version')
        version=store.version(artifact['current_version'])
        return dict(artifact=key,version=version['version_id'],stale=bool(artifact['stale']),output=json.loads(version['output']),doc_url='https://docs.google.com/document/d/'+artifact['doc_id']+'/edit')
    if action=='retry':
        return {'run_id':store.retry(mapping['story_id'])}
    if action=='decision':
        decision=store.decide(mapping['workspace_id'],request_id,command['kind'],command['text'],requester,str(event['message_id']))
        return {'decision_id':decision,'message':'Decision recorded; existing artifacts are potentially stale. This does not launch rewrites. Use /story reconcile <direction> to assess the whole package, keep suitable documents, revise the others and finish with a Quality Review. For one document use /story revise <artifact> <direction>. Use /story help research for examples.'}
    if action=='supersede':
        with store.connect() as db:
            original=db.execute('SELECT kind FROM editorial_decisions WHERE workspace_id=? AND decision_id=?',(mapping['workspace_id'],command['identifier'])).fetchone()
        if not original:
            raise Conflict('Decision does not belong to this story')
        return {'decision_id':store.decide(mapping['workspace_id'],request_id,original['kind'],command['text'],requester,str(event['message_id']),supersedes=command['identifier'])}
    if action=='cancel':
        store.cancel(mapping['workspace_id'],command['identifier'])
        return {'message':'Run cancelled; external artifacts retained.'}
    if action in {'append','import'}:
        return {'run_id':store.enqueue_resolution(mapping['story_id'],request_id,action,command['identifier'],requester)}
    return {'run_id':store.enqueue(mapping['story_id'],request_id,requester=requester,**command)}


class Telegram:
    def __init__(self,token,chat_id='-1003984316484'):
        self.token=token
        self.chat_id=str(chat_id)

    def call(self,method,payload):
        request=urllib.request.Request('https://api.telegram.org/bot'+self.token+'/'+method,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:
                result=json.load(response)
        except Exception:
            # Never propagate exception URLs containing the bot credential.
            raise RuntimeError('Telegram request failed') from None
        if not result.get('ok'):
            raise RuntimeError('Telegram rejected the request')
        return result['result']

    def create_topic(self,title):
        return self.call('createForumTopic',{'chat_id':self.chat_id,'name':title[:128]})['message_thread_id']

    def send(self,thread_id,text):
        return self.call('sendMessage',{'chat_id':self.chat_id,'message_thread_id':int(thread_id),'text':text[:4000]})['message_id']

    def verify_topic(self,thread_id):
        # Bot API provides no read-only getForumTopic. A topic-local action verifies routing.
        self.call('sendChatAction',{'chat_id':self.chat_id,'message_thread_id':int(thread_id),'action':'typing'})


def claim_notification(store):
    now=time.time()
    with store.connect(write=True) as db:
        rows=db.execute('''SELECT r.run_id,r.notifications,w.thread_id,w.chat_id FROM workflow_runs r
                           JOIN story_workspaces w USING(workspace_id) WHERE w.thread_id IS NOT NULL''').fetchall()
        for row in rows:
            notes=json.loads(row['notifications'])
            for note in notes:
                if not note['sent'] and note['due']<=now:
                    note['due']=now+60
                    note['attempts']+=1
                    db.execute('UPDATE workflow_runs SET notifications=? WHERE run_id=?',(canonical(notes),row['run_id']))
                    return dict(row),note


def notify_one(store,telegram):
    pending=claim_notification(store)
    if not pending:
        return
    row,note=pending
    status=None
    with store.connect() as db:
        run=db.execute('SELECT workspace_id FROM workflow_runs WHERE run_id=?',(row['run_id'],)).fetchone()
    artifacts=store.artifacts(run['workspace_id'])
    text=note['text']
    if artifacts.get('hub',{}).get('doc_id'):
        text+='\nHub: https://docs.google.com/document/d/'+artifacts['hub']['doc_id']+'/edit'
    key=note['key'].removeprefix('step-')
    if key in artifacts and artifacts[key]['doc_id']:
        text+='\nArtifact: https://docs.google.com/document/d/'+artifacts[key]['doc_id']+'/edit'
    try:
        if str(row['chat_id'])!=telegram.chat_id:
            raise Conflict('Notification group mismatch')
        telegram.send(row['thread_id'],text)
    except Exception:
        return  # Persisted independent outbox retries; never rerun research.
    with store.connect(write=True) as db:
        notes=json.loads(db.execute('SELECT notifications FROM workflow_runs WHERE run_id=?',(row['run_id'],)).fetchone()[0])
        for item in notes:
            if item['key']==note['key']:
                item['sent']=True
        db.execute('UPDATE workflow_runs SET notifications=? WHERE run_id=?',(canonical(notes),row['run_id']))
