"""Deterministic Google workspace provisioning and conservative document updates."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from phase2.registry import canonical
from phase2.store import Conflict


def units(text):
    return len(text.encode('utf-16-le'))//2


def doc_url(doc_id):
    return 'https://docs.google.com/document/d/'+doc_id+'/edit'


def topic_url(workspace):
    return f"https://t.me/c/{str(workspace['chat_id'])[4:]}/{workspace['first_message_id']}?thread={workspace['thread_id']}&topic"


def render_output(output):
    """Render every schema field; structured claims and rights never disappear."""
    def render(value,level=0):
        if isinstance(value,dict):
            return '\n'.join(k.replace('_',' ').title()+': '+render(v,level+1) for k,v in value.items())
        if isinstance(value,list):
            return '\n'+'\n\n'.join('• '+render(v,level+1) for v in value)
        return str(value)
    return render(output)+'\n'


class GoogleDocs:
    def __init__(self,drive,docs,parent_id):
        self.drive,self.docs,self.parent_id=drive,docs,parent_id

    @classmethod
    def authenticated(cls,token_path,parent_id):
        # Reuse authorized-user OAuth, but keep refresh writes out of agent processes.
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
        creds=Credentials.from_authorized_user_file(str(token_path),scopes=['https://www.googleapis.com/auth/drive','https://www.googleapis.com/auth/documents'])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid:
            raise Conflict('Google authentication is unavailable')
        return cls(build('drive','v3',http=AuthorizedHttp(creds,http=httplib2.Http(timeout=30)),cache_discovery=False),build('docs','v1',http=AuthorizedHttp(creds,http=httplib2.Http(timeout=30)),cache_discovery=False),parent_id)

    def metadata(self,file_id):
        return self.drive.files().get(fileId=file_id,fields='id,name,mimeType,parents,trashed,appProperties').execute()

    def ensure_file(self,store,job,key,title,mime,parent):
        workspace=store.workspace(job['workspace_id'])
        resources=json.loads(workspace['resources'])
        existing=store.artifacts(job['workspace_id']).get(key,{}).get('doc_id') if key in job['manifest']['artifacts'] else resources.get(key)
        if existing:
            metadata=self.metadata(existing)
            if metadata.get('trashed') or metadata.get('mimeType')!=mime or parent not in metadata.get('parents',[]):
                raise Conflict('Resource identity or folder parent mismatch: '+key)
            return existing
        marker=job['workspace_id']+':'+key
        files=self.drive.files().list(q="trashed=false and appProperties has { key='storiesResource' and value='"+marker+"' }",fields='files(id,parents,mimeType)',pageSize=100).execute().get('files',[])
        if len(files)>1:
            raise Conflict('Duplicate Google resources require reconciliation: '+key)
        if files:
            file_id=files[0]['id']
            if parent not in files[0].get('parents',[]) or files[0].get('mimeType')!=mime:
                raise Conflict('Reconciled resource has wrong parent or type')
        else:
            if resources.get('creating:'+key):
                raise Conflict('Ambiguous prior resource creation; reconcile before retry: '+key)
            resources['creating:'+key]=True
            store.checkpoint(job,resources=resources)
            # A marker and parent are created atomically with the resource.
            file_id=self.drive.files().create(body=dict(name=title,mimeType=mime,parents=[parent],appProperties={'storiesResource':marker}),fields='id').execute()['id']
        if key in job['manifest']['artifacts']:
            store.document(job,key,file_id)
        else:
            resources[key]=file_id
        resources.pop('creating:'+key,None)
        store.checkpoint(job,resources=resources)
        metadata=self.metadata(file_id)
        if metadata.get('trashed') or parent not in metadata.get('parents',[]):
            raise Conflict('Created resource parent verification failed')
        return file_id

    def provision(self,store,job,telegram):
        workspace=store.workspace(job['workspace_id'])
        parent=self.metadata(self.parent_id)
        if parent.get('mimeType')!='application/vnd.google-apps.folder' or parent.get('trashed'):
            raise Conflict('Canonical Active Stories parent is not available')
        with store.connect() as db:
            story=store.story(db,job['story_id'])
        resources=json.loads(workspace['resources'])
        if not resources.get('folder'):
            folder=self.ensure_file(store,job,'folder',workspace['root_key']+' — '+story['title'],'application/vnd.google-apps.folder',self.parent_id)
        else:
            folder=resources['folder']
            metadata=self.metadata(folder)
            if metadata.get('trashed') or self.parent_id not in metadata.get('parents',[]):
                raise Conflict('Existing story folder is outside the canonical parent')
        images=self.ensure_file(store,job,'images','Images','application/vnd.google-apps.folder',folder)
        for key,spec in job['manifest']['artifacts'].items():
            self.ensure_file(store,job,key,spec['number']+' — '+spec['title'],'application/vnd.google-apps.document',folder)
        workspace=store.workspace(job['workspace_id'])
        if not workspace['thread_id']:
            resources=json.loads(workspace['resources'])
            if resources.get('creating:topic'):
                raise Conflict('Ambiguous topic creation; reconcile the forum before retry')
            resources['creating:topic']=True
            store.checkpoint(job,resources=resources)
            thread=telegram.create_topic('Insurance — '+workspace['root_key'])
            # Save the thread before sending so a failed first message cannot duplicate topics.
            store.checkpoint(job,topic=(telegram.chat_id,str(thread),None))
            workspace=store.workspace(job['workspace_id'])
        if not workspace['first_message_id']:
            first=telegram.send(workspace['thread_id'],f"Story {workspace['root_key']}\n{story['title']}\n{story['origin_url']}\n"+doc_url(store.artifacts(job['workspace_id'])['hub']['doc_id']))
            store.checkpoint(job,topic=(workspace['chat_id'],workspace['thread_id'],str(first)))
        workspace=store.workspace(job['workspace_id'])
        telegram.verify_topic(workspace['thread_id'])
        # Create navigation before any research runs.
        for key in job['manifest']['artifacts']:
            a=store.artifacts(job['workspace_id'])[key]
            if not a['baseline']:
                text=self.header(store,job,key)+('\nWorkspace ready; research has not completed.\n' if key=='hub' else '\nResearch pending.\n')
                baseline,revision=self.write(a,text,'seed-'+a['artifact_id'])
                store.rendered(job,key,baseline,revision)

    def header(self,store,job,key):
        workspace=store.workspace(job['workspace_id'])
        artifacts=store.artifacts(job['workspace_id'])
        with store.connect() as db:
            story=store.story(db,job['story_id'])
        spec=job['manifest']['artifacts'][key]
        lines=[spec['number']+' — '+spec['title'],story['title'],'Story root: '+workspace['root_key'],'Original article: '+story['origin_url'],'Discussion: '+topic_url(workspace),'Navigation:']
        siblings=['hub']+spec['depends_on'] if key!='hub' else list(artifacts)
        for other in dict.fromkeys(siblings):
            if artifacts[other]['doc_id']:
                lines.append(job['manifest']['artifacts'][other]['title']+': '+doc_url(artifacts[other]['doc_id']))
        if key in {'hub','visuals'}:
            resources=json.loads(workspace['resources'])
            lines.append('Images: https://drive.google.com/drive/folders/'+resources['images'])
        if key=='hub':
            resources=json.loads(workspace['resources'])
            if resources.get('legacy_doc'):
                lines.append('Historical Research & Treatment (preserved; content not reverified): '+doc_url(resources['legacy_doc']))
        return '\n'.join(lines)+'\n'

    def hub(self,store,job):
        text=self.header(store,job,'hub')+'\nResearch and editorial status\n'
        for key,a in store.artifacts(job['workspace_id']).items():
            if key!='hub':
                text+=key+': '+('potentially stale' if a['stale'] else a['render_state'])+'\n'
        with store.connect() as db:
            decisions=[dict(d) for d in db.execute('SELECT kind,text,author FROM editorial_decisions WHERE workspace_id=? AND decision_id NOT IN (SELECT supersedes FROM editorial_decisions WHERE workspace_id=? AND supersedes IS NOT NULL) ORDER BY created_at',(job['workspace_id'],job['workspace_id']))]
            blocked=db.execute("SELECT count(*) FROM workflow_runs WHERE workspace_id=? AND state='blocked'",(job['workspace_id'],)).fetchone()[0]
        text+='\nEditorial decisions and standing instructions\n'+render_output(decisions)
        review=store.artifacts(job['workspace_id']).get('review',{})
        if review.get('current_version'):
            output=json.loads(store.version(review['current_version'])['output'])
            text+='\nQuality review readiness: '+output.get('readiness','incomplete')+'\n'
            text+='Open research gaps:\n'+'\n'.join(output.get('gaps',[])[:8])+'\n'
            if review.get('stale'):
                text+='The quality review is potentially stale.\n'
        if blocked:
            text+='\nBlocked operations: '+str(blocked)+'; use /story status.\n'
        text+='\nResearch completion does not approve publication.\n'
        return text

    def read(self,doc_id):
        return self.docs.documents().get(documentId=doc_id,includeTabsContent=True,suggestionsViewMode='SUGGESTIONS_INLINE').execute()

    @staticmethod
    def body(document):
        tabs=document.get('tabs',[])
        if tabs:
            if len(tabs)!=1 or tabs[0].get('childTabs'):
                raise Conflict('Unexpected document tabs; reconcile structure before updating')
            return tabs[0]['documentTab'],tabs[0]['documentTab']['body']['content'],tabs[0]['tabProperties']['tabId']
        return document,document.get('body',{}).get('content',[]),None

    @staticmethod
    def text(content):
        return ''.join(e.get('textRun',{}).get('content','') for p in content for e in p.get('paragraph',{}).get('elements',[]))

    @staticmethod
    def fingerprint(content,start,end):
        def clean(value):
            if isinstance(value,dict):
                return {k:clean(v) for k,v in value.items() if k not in {'startIndex','endIndex'}}
            if isinstance(value,list):
                return [clean(v) for v in value]
            return value
        selected=[p for p in content if p.get('startIndex',0)>=start and p.get('startIndex',0)<end]
        return hashlib.sha256(canonical(clean(selected)).encode()).hexdigest()

    def write(self,artifact,text,marker):
        doc_id=artifact['doc_id']
        document=self.read(doc_id)
        tab,content,tab_id=self.body(document)
        named=tab.get('namedRanges',document.get('namedRanges',{}))
        names=[r for group in named.values() for r in group.get('namedRanges',[])]
        managed=[n for n in names if n.get('name','').startswith('stories-managed-')]
        recovery=[n for n in managed if n['name']=='stories-managed-'+marker]
        if recovery:
            span=recovery[0]['ranges'][0]
            actual=self.text([p for p in content if span['startIndex']<=p.get('startIndex',0)<span['endIndex']])
            if actual!=text:
                raise Conflict('Document changed after a prior write; retain human changes')
            return self.fingerprint(content,span['startIndex'],span['endIndex']),document['revisionId']
        comments=self.drive.comments().list(fileId=doc_id,fields='nextPageToken,comments(id,resolved,deleted)',includeDeleted=False,pageSize=100).execute()
        if any(not c.get('resolved') and not c.get('deleted') for c in comments.get('comments',[])) or comments.get('nextPageToken'):
            raise Conflict('Document has comments; preserve annotations and review the proposed revision')
        if 'suggested' in canonical(content).lower():
            raise Conflict('Document has suggestions requiring editorial review')
        if artifact['baseline']:
            if len(managed)!=1 or len(managed[0].get('ranges',[]))!=1:
                raise Conflict('Managed section boundaries are missing or ambiguous')
            span=managed[0]['ranges'][0]
            start,end=span['startIndex'],span['endIndex']
            if self.fingerprint(content,start,end)!=artifact['baseline']:
                raise Conflict('Human edits changed the managed section')
        else:
            if self.text(content).strip():
                raise Conflict('Unregistered document content cannot be overwritten')
            start,end=1,1
        location={'index':start}
        span={'startIndex':start,'endIndex':start+units(text)}
        if tab_id:
            location['tabId']=tab_id
            span['tabId']=tab_id
        requests=[]
        # Remove range metadata before deleting its text; Docs may otherwise remove it implicitly.
        for name in managed:
            request={'namedRangeId':name['namedRangeId']}
            if tab_id:
                request['tabsCriteria']={'tabIds':[tab_id]}
            requests.append({'deleteNamedRange':request})
        if end>start:
            old={'startIndex':start,'endIndex':end}
            if tab_id:
                old['tabId']=tab_id
            requests.append({'deleteContentRange':{'range':old}})
        requests.append({'insertText':{'location':location,'text':text}})
        if not artifact['baseline']:
            notes_location={'index':start+units(text)}
            if tab_id:
                notes_location['tabId']=tab_id
            requests.append({'insertText':{'location':notes_location,'text':'\nHuman editorial notes (preserved)\n'}})
        requests.append({'createNamedRange':{'name':'stories-managed-'+marker,'range':span}})
        for match in re.finditer(r'https?://[^\s]+',text):
            link_range=dict(span,startIndex=start+units(text[:match.start()]),endIndex=start+units(text[:match.end()]))
            requests.append({'updateTextStyle':{'range':link_range,'textStyle':{'link':{'url':match.group()}},'fields':'link'}})
        self.docs.documents().batchUpdate(documentId=doc_id,body={'writeControl':{'requiredRevisionId':document['revisionId']},'requests':requests}).execute()
        verified=self.read(doc_id)
        _,new_content,_=self.body(verified)
        if self.text([p for p in new_content if start<=p.get('startIndex',0)<start+units(text)])!=text:
            raise Conflict('Document readback did not match rendered content')
        return self.fingerprint(new_content,start,start+units(text)),verified['revisionId']

    def append_revision(self,artifact,text,marker):
        """Explicit conflict resolution: retain all old content and annotations."""
        document=self.read(artifact['doc_id'])
        tab,content,tab_id=self.body(document)
        named=tab.get('namedRanges',document.get('namedRanges',{}))
        names=[r for group in named.values() for r in group.get('namedRanges',[])]
        managed=[n for n in names if n.get('name','').startswith('stories-managed-')]
        recovery=next((n for n in managed if n['name']=='stories-managed-'+marker),None)
        if recovery:
            span=recovery['ranges'][0]
            return self.fingerprint(content,span['startIndex'],span['endIndex']),document['revisionId']
        start=max(p.get('endIndex',1) for p in content)-1
        prefix='\n\nApproved revision (previous content retained above)\n'
        location={'index':start}
        span={'startIndex':start+units(prefix),'endIndex':start+units(prefix+text)}
        if tab_id:
            location['tabId']=tab_id
            span['tabId']=tab_id
        requests=[{'insertText':{'location':location,'text':prefix+text+'\nHuman editorial notes (preserved)\n'}}]
        for old in managed:
            body={'namedRangeId':old['namedRangeId']}
            if tab_id:
                body['tabsCriteria']={'tabIds':[tab_id]}
            requests.append({'deleteNamedRange':body})
        requests.append({'createNamedRange':{'name':'stories-managed-'+marker,'range':span}})
        self.docs.documents().batchUpdate(documentId=artifact['doc_id'],body={'writeControl':{'requiredRevisionId':document['revisionId']},'requests':requests}).execute()
        verified=self.read(artifact['doc_id'])
        _,content,_=self.body(verified)
        if self.text([p for p in content if span['startIndex']<=p.get('startIndex',0)<span['endIndex']])!=text:
            raise Conflict('Appended revision readback mismatch')
        return self.fingerprint(content,span['startIndex'],span['endIndex']),verified['revisionId']

    def proposal(self,store,job,version,text):
        resources=json.loads(store.workspace(job['workspace_id'])['resources'])
        proposal_id=self.ensure_file(store,job,'proposal-'+version,'Proposed revision — '+job['step_key'],'application/vnd.google-apps.document',resources['folder'])
        self.write(dict(doc_id=proposal_id,baseline=''),text,'proposal-'+version)
        with store.connect(write=True) as db:
            store._owned(db,job)
            db.execute('UPDATE artifact_versions SET proposal_doc_id=? WHERE version_id=?',(proposal_id,version))
        return proposal_id
