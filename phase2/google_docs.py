"""Deterministic Google workspace provisioning and conservative document updates."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

from phase2.doc_policy import TableDocument, render_document, audit
from phase2.registry import canonical
from phase2.store import Conflict


def units(text):
    return len(text.encode('utf-16-le'))//2


def doc_url(doc_id):
    return 'https://docs.google.com/document/d/'+doc_id+'/edit'


def topic_url(workspace):
    return f"https://t.me/c/{str(workspace['chat_id'])[4:]}/{workspace['first_message_id']}?thread={workspace['thread_id']}&topic"



class GoogleDocs:
    def __init__(self,drive,docs,parent_id,client_factory=None):
        self.parent_id=parent_id
        self._fixed_clients=(drive,docs)
        self._client_factory=client_factory
        self._thread_clients=threading.local()

    def _clients(self):
        if self._client_factory is None:
            return self._fixed_clients
        if not hasattr(self._thread_clients,'services'):
            self._thread_clients.services=self._client_factory()
        return self._thread_clients.services

    @property
    def drive(self):
        return self._clients()[0]

    @property
    def docs(self):
        return self._clients()[1]

    @classmethod
    def authenticated(cls,token_path,parent_id):
        # httplib2 transports are not thread-safe. Credentials and both services
        # belong to the calling thread; refresh never rewrites the shared token.
        def clients():
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
            return (build('drive','v3',http=AuthorizedHttp(creds,http=httplib2.Http(timeout=30)),cache_discovery=False),build('docs','v1',http=AuthorizedHttp(creds,http=httplib2.Http(timeout=30)),cache_discovery=False))
        instance=cls(None,None,parent_id,client_factory=clients)
        instance._clients()  # Validate authentication before starting the worker.
        return instance

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
        artifacts=store.artifacts(job['workspace_id'])
        with store.connect() as db:
            story=store.story(db,job['story_id'])
            decisions=[dict(d) for d in db.execute('SELECT kind,text FROM editorial_decisions WHERE workspace_id=? AND decision_id NOT IN (SELECT supersedes FROM editorial_decisions WHERE workspace_id=? AND supersedes IS NOT NULL) ORDER BY created_at',(job['workspace_id'],job['workspace_id']))]
            blocked=db.execute("SELECT count(*) FROM workflow_runs WHERE workspace_id=? AND state='blocked'",(job['workspace_id'],)).fetchone()[0]
        review=artifacts.get('review',{})
        output=json.loads(store.version(review['current_version'])['output']) if review.get('current_version') else {}
        status=output.get('readiness','Research in progress')
        rows=[
            dict(topic='Story',short='Current story and editorial direction.',detail=story['title']),
            dict(topic='Current assessment',short=status,detail='Research completion does not approve publication.'),
            dict(topic='Progress',short='See linked documents for current work.',detail='; '.join(k+': '+('potentially stale' if a['stale'] else a['render_state']) for k,a in artifacts.items() if k!='hub')),
            dict(topic='Blockers',short=str(blocked)+' blocked operations.',detail='Use /story status for operational blockers; see Quality Review for unresolved research gaps.'),
            dict(topic='Next decision',short='Review the package and resolve material gaps.',detail='; '.join(d['kind']+': '+d['text'] for d in decisions) or 'No standing editorial decisions recorded.'),
        ]
        # Human instructions are preserved in storage; necessary excess remains explicit.
        return render_document(self.header(store,job,'hub'),'hub',dict(editorial_rows=rows,policy_exception='Standing human instructions retained in full; editorial condensation requires review.' if any(len(r['detail'].split())>60 for r in rows) else ''))

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
        result=''
        for p in content:
            result += ''.join(e.get('textRun',{}).get('content','') for e in p.get('paragraph',{}).get('elements',[]))
            for row in p.get('table',{}).get('tableRows',[]):
                for cell in row.get('tableCells',[]):
                    result += GoogleDocs.text(cell.get('content',[]))
        return result

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
        if isinstance(text,TableDocument):
            return self.write_table(artifact,text,marker)
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
        if isinstance(text,TableDocument):
            return self.write_table(artifact,text,marker,append=True)
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

    @staticmethod
    def table_rows(content):
        return [[[GoogleDocs.text(cell.get('content',[])).removesuffix('\n') for cell in row['tableCells']]
                 for row in p['table']['tableRows']] for p in content if 'table' in p]

    def review_snapshot(self, artifact, key):
        document=self.read(artifact['doc_id'])
        tab,content,_=self.body(document)
        names=[n for g in tab.get('namedRanges',document.get('namedRanges',{})).values() for n in g.get('namedRanges',[]) if n.get('name','').startswith('stories-managed-')]
        spans=[r for n in names for r in n.get('ranges',[])]
        managed=[p for p in content if any(r['startIndex']<=p.get('startIndex',0)<r['endIndex'] for r in spans)]
        tables=self.table_rows(managed)
        text=self.text(content)
        checks=audit(key,text,self.text(managed),tables[0] if len(tables)==1 else None)
        if len(self.table_rows(content))>1:
            checks['issues'].append('Historical or extra tables remain outside the current editorial table')
            checks['outcome']='revise'
        matches=len(spans)==1 and bool(artifact['baseline']) and self.fingerprint(content,spans[0]['startIndex'],spans[0]['endIndex'])==artifact['baseline']
        return dict(doc_id=artifact['doc_id'],revision=document['revisionId'],text=text,checks=checks,managed_matches=matches)

    def write_table(self, artifact, plan, marker, append=False):
        document=self.read(artifact['doc_id'])
        tab,content,tab_id=self.body(document)
        names=[n for g in tab.get('namedRanges',document.get('namedRanges',{})).values() for n in g.get('namedRanges',[])]
        managed=[n for n in names if n.get('name','').startswith('stories-managed-')]
        final=next((n for n in managed if n['name']=='stories-managed-'+marker),None)
        if final:
            span=final['ranges'][0]
            selected=[p for p in content if span['startIndex']<=p.get('startIndex',0)<span['endIndex']]
            if self.text(selected)!=plan.text or self.table_rows(selected)!=[plan.grid]:
                raise Conflict('Document changed after prior table write; retain human changes')
            return self.fingerprint(content,span['startIndex'],span['endIndex']),document['revisionId']
        if not append:
            comments=self.drive.comments().list(fileId=artifact['doc_id'],fields='nextPageToken,comments(id,resolved,deleted)',includeDeleted=False,pageSize=100).execute()
            if comments.get('nextPageToken') or any(not c.get('resolved') and not c.get('deleted') for c in comments.get('comments',[])):
                raise Conflict('Document has comments; preserve annotations')
            if 'suggested' in canonical(content).lower(): raise Conflict('Document has suggestions requiring review')
            if artifact['baseline']:
                if len(managed)!=1 or len(managed[0].get('ranges',[]))!=1:
                    raise Conflict('Managed section boundaries missing or ambiguous')
                old=managed[0]['ranges'][0]; start,end=old['startIndex'],old['endIndex']
                if self.fingerprint(content,start,end)!=artifact['baseline']:
                    raise Conflict('Human edits changed the managed section')
            else:
                if self.text(content).strip(): raise Conflict('Unregistered document content cannot be overwritten')
                start=end=1
        else:
            start=end=max(p.get('endIndex',1) for p in content)-1
        retained=self.text([p for p in content if not (start<=p.get('startIndex',0)<end)])
        from phase2.doc_policy import SPECS, words
        if words(retained+plan.text)>SPECS[plan.key][0] and not plan.exception:
            raise Conflict('Whole document exceeds soft cap including retained content; prepare a shorter revision or explicit exception')
        def location(index):
            return dict(index=index,**({'tabId':tab_id} if tab_id else {}))
        def span(a,b):
            return dict(startIndex=a,endIndex=b,**({'tabId':tab_id} if tab_id else {}))
        requests=[]
        for n in managed:
            request={'namedRangeId':n['namedRangeId']}
            if tab_id: request['tabsCriteria']={'tabIds':[tab_id]}
            requests.append({'deleteNamedRange':request})
        if end>start: requests.append({'deleteContentRange':{'range':span(start,end)}})
        prefix=plan.prefix
        requests.append({'insertText':{'location':location(start),'text':prefix}})
        if not artifact['baseline'] or append:
            requests.append({'insertText':{'location':location(start+units(prefix)),'text':'\nHuman editorial notes (preserved)\n'}})
        at=start+units(prefix)
        requests.append({'insertTable':{'rows':len(plan.grid),'columns':3,'location':location(at)}})
        # insertTable adds a newline before the table. Empty cells contain a newline.
        table_start=at+1
        entries=[]
        for r,row in enumerate(plan.grid):
            for c,value in enumerate(row):
                entries.append((table_start+3+r*7+c*2,value))
        for index,value in reversed(entries):
            requests.append({'insertText':{'location':location(index),'text':value}})
        final_end=table_start+2+len(plan.grid)*7+sum(units(v) for row in plan.grid for v in row)
        requests.append({'createNamedRange':{'name':'stories-managed-'+marker,'range':span(start,final_end)}})
        for match in re.finditer(r'https?://[^\s]+',prefix):
            requests.append({'updateTextStyle':{'range':span(start+units(prefix[:match.start()]),start+units(prefix[:match.end()])), 'textStyle':{'link':{'url':match.group()}},'fields':'link'}})
        # Link and format using final cell offsets, including non-BMP characters.
        offset=0
        for n,(index,value) in enumerate(entries):
            actual=index+offset
            for match in re.finditer(r'https?://[^\s]+',value):
                requests.append({'updateTextStyle':{'range':span(actual+units(value[:match.start()]),actual+units(value[:match.end()])), 'textStyle':{'link':{'url':match.group()}},'fields':'link'}})
            if n<3:
                requests.append({'updateTextStyle':{'range':span(actual,actual+units(value)), 'textStyle':{'bold':True},'fields':'bold'}})
            offset+=units(value)
        self.docs.documents().batchUpdate(documentId=artifact['doc_id'],body={'writeControl':{'requiredRevisionId':document['revisionId']},'requests':requests}).execute()
        verified=self.read(artifact['doc_id']); _,new_content,_=self.body(verified)
        selected=[p for p in new_content if start<=p.get('startIndex',0)<final_end]
        if self.text(selected)!=plan.text or self.table_rows(selected)!=[plan.grid]:
            raise Conflict('Table readback mismatch; reconcile before retry')
        return self.fingerprint(new_content,start,final_end),verified['revisionId']
