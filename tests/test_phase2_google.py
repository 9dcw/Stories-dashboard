import copy
from types import SimpleNamespace

import pytest

from phase2.google_docs import GoogleDocs,units
from phase2.store import Conflict


def paragraph(start,text):
    return {'startIndex':start,'endIndex':start+units(text),'paragraph':{'elements':[{'startIndex':start,'endIndex':start+units(text),'textRun':{'content':text}}]}}


def document(text,marker=None,notes='Human notes\n',revision='r1'):
    content=[paragraph(1,text),paragraph(1+units(text),notes)]
    names={}
    if marker:
        names={'group':{'namedRanges':[{'name':'stories-managed-'+marker,'namedRangeId':'named','ranges':[{'startIndex':1,'endIndex':1+units(text),'tabId':'tab'}]}]}}
    return {'revisionId':revision,'tabs':[{'tabProperties':{'tabId':'tab'},'documentTab':{'body':{'content':content},'namedRanges':names}}]}


class Result:
    def __init__(self,value):self.value=value
    def execute(self):return copy.deepcopy(self.value)


class Docs:
    def __init__(self,reads):self.reads=iter(reads);self.batches=[]
    def documents(self):return self
    def get(self,**kwargs):return Result(next(self.reads))
    def batchUpdate(self,**kwargs):self.batches.append(kwargs);return Result({})


class Drive:
    def __init__(self,comments=None):self.comments_value=comments or {'comments':[]}
    def comments(self):return self
    def list(self,**kwargs):return Result(self.comments_value)


def artifact(doc):
    _,content,_=GoogleDocs.body(doc)
    return {'doc_id':'doc','baseline':GoogleDocs.fingerprint(content,1,content[0]['endIndex'])}


def test_revision_guard_named_ranges_utf16_and_preserved_notes():
    before=document('Old generated text\n','old',notes='Do not remove my notes\n')
    text='New 😀 text https://example.org/source\n'
    after=document(text,'new',notes='Do not remove my notes\n',revision='r2')
    docs=Docs([before,after])
    google=GoogleDocs(Drive(),docs,'parent')
    baseline,revision=google.write(artifact(before),text,'new')
    assert revision=='r2' and baseline
    batch=docs.batches[0]['body']
    assert batch['writeControl']=={'requiredRevisionId':'r1'}
    requests=batch['requests']
    assert 'deleteNamedRange' in requests[0]
    deletion=next(r['deleteContentRange']['range'] for r in requests if 'deleteContentRange' in r)
    assert deletion['endIndex']==before['tabs'][0]['documentTab']['body']['content'][0]['endIndex']
    link=next(r['updateTextStyle'] for r in requests if 'updateTextStyle' in r)
    assert link['range']['startIndex']==1+units('New 😀 text ')
    assert link['textStyle']['link']['url']=='https://example.org/source'


def test_human_edits_block_all_replacements():
    baseline=document('Generated\n','old')
    edited=document('Human revision\n','old')
    docs=Docs([edited])
    with pytest.raises(Conflict,match='Human edits'):
        GoogleDocs(Drive(),docs,'parent').write(artifact(baseline),'Replacement\n','new')
    assert docs.batches==[]


@pytest.mark.parametrize('comments',[{'comments':[{'id':'c1','resolved':False}]},{'comments':[],'nextPageToken':'more'}])
def test_unresolved_or_unexamined_comments_block(comments):
    before=document('Generated\n','old')
    docs=Docs([before])
    with pytest.raises(Conflict,match='comments'):
        GoogleDocs(Drive(comments),docs,'parent').write(artifact(before),'Replacement\n','new')
    assert not docs.batches


def test_recovery_after_google_write_does_not_write_twice():
    after=document('New text\n','candidate')
    docs=Docs([after])
    result=GoogleDocs(Drive(),docs,'parent').write({'doc_id':'doc','baseline':'previous'},'New text\n','candidate')
    assert result[1]=='r1'
    assert not docs.batches


def test_missing_boundaries_or_extra_tabs_block():
    before=document('Generated\n')
    with pytest.raises(Conflict,match='boundaries'):
        GoogleDocs(Drive(),Docs([before]),'parent').write(artifact(before),'New\n','new')
    before['tabs'].append(copy.deepcopy(before['tabs'][0]))
    with pytest.raises(Conflict,match='tabs'):
        GoogleDocs.body(before)


def test_explicit_append_never_deletes_old_text():
    before=document('Human edited text\n','old')
    docs=Docs([before])
    google=GoogleDocs(Drive({'comments':[{'resolved':False}]}),docs,'parent')
    # Readback contains a new range at the appended paragraph boundary.
    start=before['tabs'][0]['documentTab']['body']['content'][-1]['endIndex']-1
    prefix='\n\nApproved revision (previous content retained above)\n'
    span_start=start+units(prefix)
    after=document('Human edited text\n','new')
    after['tabs'][0]['documentTab']['body']['content']=[paragraph(1,'Human edited text\n'),paragraph(span_start,'Approved text\n')]
    docs.reads=iter([before,after])
    google.append_revision(artifact(before),'Approved text\n','new')
    assert all('deleteContentRange' not in r for r in docs.batches[0]['body']['requests'])
