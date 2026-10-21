import json

import pytest

from phase2.migration import inventory,adopt,verify_entry
from phase2.store import WorkflowStore,Conflict


def test_inventory_detects_shared_resource_ids_despite_url_variants(api_db):
    path,old,first,second=api_db
    for item,url in [(first,'https://docs.google.com/document/d/shared/edit'),(second,'https://docs.google.com/document/d/shared/edit?usp=drivesdk')]:
        sid=old.promote_item(item,'Legacy','Phase 2')
        old.update_story_links(sid,google_doc_url=url,research_folder_url='https://drive.google.com/drive/folders/shared-folder')
    before=path.read_bytes()
    report=inventory(path)
    assert report['blocked_count']==2
    assert all(any('Shared google_doc_url' in i for i in p['issues']) for p in report['projects'])
    assert path.read_bytes()==before


def test_adoption_is_idempotent_and_preserves_original_links(api_db):
    path,old,item,_=api_db
    sid=old.promote_item(item,'Legacy','Phase 2')
    old.update_story_links(sid,google_doc_url='https://docs.google.com/document/d/legacy/edit')
    store=WorkflowStore(path);store.initialize()
    entry=dict(story_id=sid,root_key='canonical',event_key='event',folder_id='folder',images_id='images',legacy_doc_id='legacy',chat_id='-1003984316484',thread_id='2',first_message_id='3')
    proof=dict(revision='r1',excerpt='Legacy evidence, not freshly verified')
    wid=adopt(store,entry,proof)
    assert adopt(store,entry,proof)==wid
    assert old.get_story(sid)['google_doc_url'].endswith('/legacy/edit')
    assert len(store.artifacts(wid))==7
    assert all(a['current_version'] is None for a in store.artifacts(wid).values())


def test_distinct_roots_cannot_share_a_folder(api_db):
    path,old,first,second=api_db
    store=WorkflowStore(path);store.initialize()
    proof=dict(revision='r',excerpt='Unverified')
    for index,item in enumerate([first,second]):
        sid=old.promote_item(item,'Legacy','Phase 2')
        entry=dict(story_id=sid,root_key='root-'+str(index),event_key='e',folder_id='same',images_id='images',legacy_doc_id='legacy',chat_id='-1003984316484',thread_id=str(index+1),first_message_id='3')
        if index==0:
            adopt(store,entry,proof)
        else:
            with pytest.raises(Conflict,match='collision'):
                adopt(store,entry,proof)
