#!/usr/bin/env python3
"""Opt-in live Google write pilot, isolated from all existing story resources."""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from phase2.google_docs import GoogleDocs
from phase2.store import Conflict


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token',type=Path,required=True)
    parser.add_argument('--parent',required=True)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not args.live:
        parser.error('--live explicitly authorizes a disposable private write pilot and trash cleanup')
    google=GoogleDocs.authenticated(args.token,args.parent)
    marker='stories-pilot-'+uuid.uuid4().hex
    created=[]
    try:
        folder=google.drive.files().create(body={'name':'TEST ONLY — Phase 2 renderer pilot','mimeType':'application/vnd.google-apps.folder','parents':[args.parent],'appProperties':{'storiesPilot':marker}},fields='id').execute()['id']
        created.append(folder)
        doc=google.drive.files().create(body={'name':'TEST ONLY — disposable renderer verification','mimeType':'application/vnd.google-apps.document','parents':[folder],'appProperties':{'storiesPilot':marker}},fields='id').execute()['id']
        created.append(doc)
        artifact={'doc_id':doc,'baseline':''}
        baseline,_=google.write(artifact,'Generated v1 😀\nSource: https://example.org/record\n','pilot-v1')
        artifact['baseline']=baseline
        document=google.read(doc);_,content,tab=google.body(document)
        index=max(p.get('endIndex',1) for p in content)-1
        location={'index':index}
        if tab:location['tabId']=tab
        google.docs.documents().batchUpdate(documentId=doc,body={'requests':[{'insertText':{'location':location,'text':'Human note: KEEP THIS NOTE\n'}}]}).execute()
        baseline,_=google.write(artifact,'Generated v2\nSource: https://example.org/record\n','pilot-v2')
        artifact['baseline']=baseline
        document=google.read(doc);_,content,tab=google.body(document)
        assert 'Human note: KEEP THIS NOTE' in google.text(content)
        location={'index':3}
        if tab:location['tabId']=tab
        google.docs.documents().batchUpdate(documentId=doc,body={'requests':[{'insertText':{'location':location,'text':'HUMAN-EDIT-'}}]}).execute()
        try:
            google.write(artifact,'This must not replace the human edit\n','pilot-conflict')
        except Conflict:
            pass
        else:
            raise RuntimeError('Human edit protection failed')
        google.drive.comments().create(fileId=doc,body={'content':'Private pilot annotation; retain its original content.'},fields='id').execute()
        google.append_revision(artifact,'Approved appended revision\n','pilot-append')
        document=google.read(doc);_,content,_=google.body(document)
        text=google.text(content)
        assert 'HUMAN-EDIT-' in text and 'Human note: KEEP THIS NOTE' in text and 'Approved appended revision' in text
        assert google.drive.comments().list(fileId=doc,fields='comments(id,content)').execute()['comments']
        print(json.dumps({'google_write_pilot':'passed','checks':['create_and_parent','revision_guard','named_ranges','utf16','human_notes_preserved','human_edit_conflict','explicit_append_preserves_annotations']}))
    finally:
        for resource in reversed(created):
            metadata=google.metadata(resource)
            if metadata.get('appProperties',{}).get('storiesPilot')!=marker:
                raise RuntimeError('Refusing cleanup of a resource not owned by this pilot')
            google.drive.files().update(fileId=resource,body={'trashed':True},fields='id,trashed').execute()
        if created:
            print('Disposable pilot resources moved to Trash; existing stories were untouched.')


if __name__=='__main__':
    main()
