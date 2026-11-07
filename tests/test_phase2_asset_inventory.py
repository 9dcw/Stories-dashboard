import json
from types import SimpleNamespace

from phase2.asset_inventory import refresh_inventory,refresh_due
from tests.test_phase2_visuals import visual_worker,run
from tests.test_phase2_google import Result


class FolderGoogle:
    def __init__(self,pages):
        self.pages=pages;self.calls=[];self.failure=False
        self.drive=SimpleNamespace(files=lambda:self)
    def metadata(self,folder):
        return dict(id=folder,mimeType='application/vnd.google-apps.folder',parents=['folder'])
    def list(self,**kwargs):
        self.calls.append(kwargs)
        if self.failure: raise ConnectionError('private credential detail')
        return Result(self.pages[int(kwargs.get('pageToken','0'))])


def assets(*ids):
    return dict(files=[dict(id=i,mimeType='image/png') for i in ids])


def test_unknown_count_is_not_reported_as_zero(visual_worker):
    w,story,_=visual_worker
    value=w.store.status(story)['visual_assets']
    assert value['saved'] is None and value['checked_at'] is None
    assert value['folder_url'].endswith('/images')


def test_inventory_counts_supported_files_across_pages_and_deduplicates(visual_worker):
    w,story,_=visual_worker
    with w.store.connect() as db:wid=w.store.story(db,story)['workspace_id']
    google=FolderGoogle([
        dict(files=[dict(id='image',mimeType='image/png'),dict(id='notes',mimeType='application/vnd.google-apps.document')],nextPageToken='1'),
        dict(files=[dict(id='image',mimeType='image/png'),dict(id='pdf',mimeType='application/pdf'),dict(id='video',mimeType='video/mp4'),dict(id='folder',mimeType='application/vnd.google-apps.folder')])])
    assert refresh_inventory(w.store,google,wid)
    value=w.store.status(story)['visual_assets']
    assert value['saved']==3 and value['checked_at'] and not value['stale']
    assert len(google.calls)==2 and "'images' in parents and trashed=false"==google.calls[0]['q']


def test_failure_retains_last_verified_count(visual_worker):
    w,story,_=visual_worker
    with w.store.connect() as db:wid=w.store.story(db,story)['workspace_id']
    google=FolderGoogle([assets('one','two')]);assert refresh_inventory(w.store,google,wid)
    before=w.store.status(story)['visual_assets']
    google.failure=True;assert not refresh_inventory(w.store,google,wid)
    after=w.store.status(story)['visual_assets']
    assert after['saved']==2 and after['checked_at']==before['checked_at']
    assert after['stale'] and after['refresh_failed']
    assert 'private credential detail' not in json.dumps(after)


def test_empty_folder_is_a_verified_zero(visual_worker):
    w,story,_=visual_worker
    with w.store.connect() as db:wid=w.store.story(db,story)['workspace_id']
    assert refresh_inventory(w.store,FolderGoogle([assets()]),wid)
    value=w.store.status(story)['visual_assets']
    assert value['saved']==0 and value['checked_at'] is not None


def test_old_versions_do_not_inflate_current_build_progress(visual_worker):
    w,story,docs=visual_worker
    run(w,story,'first');docs.grid[1][0]+=' Change arrows.'
    _,job=run(w,story,'changed')
    google=FolderGoogle([assets('file-1','file-2','file-3','manual-upload')])
    assert refresh_inventory(w.store,google,job['workspace_id'])
    value=w.store.status(story)['visual_assets']
    assert value['saved']==4 and value['build']==dict(saved=2,total=2,blocked=0,skipped=0)
    google.pages=[assets('file-3')];assert refresh_inventory(w.store,google,job['workspace_id'])
    value=w.store.status(story)['visual_assets']
    assert value['saved']==1 and value['build']['saved']==1 and value['build']['total']==2


def test_revised_narrative_does_not_show_old_build_as_current(visual_worker):
    w,story,_=visual_worker;_,job=run(w,story,'first')
    with w.store.connect(write=True) as db:
        db.execute('UPDATE artifact_versions SET created_at=(SELECT created_at+1 FROM workflow_runs WHERE run_id=?)',(job['run_id'],))
    value=w.store.status(story)['visual_assets']
    assert value['build'] is None and value['build_outdated']


def test_periodic_refresh_skips_fresh_folders_and_deleted_stories(visual_worker):
    w,story,_=visual_worker;google=FolderGoogle([assets('one')])
    assert refresh_due(w.store,google)==1
    assert refresh_due(w.store,google)==0
    with w.store.connect(write=True) as db:
        db.execute('UPDATE asset_folder_inventory SET attempted_at=0')
        db.execute("UPDATE story_projects SET status='Deleted' WHERE story_id=?",(story,))
    assert refresh_due(w.store,google)==0


def test_folder_identity_change_during_refresh_does_not_publish_count(visual_worker):
    w,story,_=visual_worker
    with w.store.connect() as db:wid=w.store.story(db,story)['workspace_id']
    google=FolderGoogle([assets('one')]);original=google.list
    def changed(**kwargs):
        with w.store.connect(write=True) as db:
            db.execute('UPDATE story_workspaces SET resources=? WHERE workspace_id=?',(json.dumps(dict(folder='folder',images='new-folder')),wid))
        return original(**kwargs)
    google.list=changed
    assert not refresh_inventory(w.store,google,wid)
    assert w.store.status(story)['visual_assets']['saved'] is None


def test_worker_refreshes_counts_in_background_without_a_research_job(visual_worker,monkeypatch):
    import threading
    w,_,_=visual_worker;stop=threading.Event();threads=[]
    monkeypatch.setattr(w.store,'claim',lambda:None)
    def refreshed(store,google):
        threads.append(threading.get_ident());stop.set()
    monkeypatch.setattr('phase2.asset_inventory.refresh_due',refreshed)
    w.run(stop)
    assert len(threads)==1 and threads[0]!=threading.get_ident()
