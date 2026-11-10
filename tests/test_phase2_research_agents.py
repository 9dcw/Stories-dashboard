import json
import threading

import pytest

from phase2.research_agents import (ResearchContractError,activity,packet,run_search_agents,needs_source_research)

URL='https://court.example.test/case.pdf'
QUOTE='The dissent explains that additional testimony contradicts the majority account.'
IDENTITY='Example v. Insurer, No. 12-345'


def result(search=True,quote=QUOTE,status='found'):
    calls=[];responses=[]
    if search:
        calls.append(dict(id='search',function=dict(name='web_search',arguments=json.dumps(dict(query='Example 12-345 dissent')))))
        responses.append(dict(role='tool',tool_call_id='search',content='Search results'))
    calls.append(dict(id='fetch',function=dict(name='stories_fetch_source',arguments=json.dumps(dict(url=URL)))))
    responses.append(dict(role='tool',tool_call_id='fetch',content='Source result'))
    text=dict(summary='Competing account.',findings=[dict(claim='There is contrary testimony.',url=URL,quote=quote,locator='dissent p. 39',opinion_type='dissent',case_match=IDENTITY)],gaps=[],dissent_status=status)
    return dict(completed=True,interrupted=False,final_response=json.dumps(text),messages=[dict(role='assistant',tool_calls=calls)]+responses,total_tokens=10,input_tokens=7,output_tokens=3)


def sources(truncated=False):
    return [dict(url=URL,requested_url=URL,text=IDENTITY+'\n'+QUOTE,truncated=truncated)]


def test_both_search_agents_receive_user_question_and_run_independently():
    seen=[];barrier=threading.Barrier(2)
    def run(role,prompt,context,limit):
        barrier.wait(timeout=3)
        seen.append((role,context['instruction'],threading.get_ident(),limit))
        return result()
    packets,usage=run_search_agents(dict(story=dict(title='Case'),instruction='Investigate contrary testimony'),run,sources(),dict(step_tokens=120000))
    assert {s[0] for s in seen}=={'direction','dissents'}
    assert all(s[1]=='Investigate contrary testimony' for s in seen)
    assert len({s[2] for s in seen})==2
    assert usage['total_tokens']==20 and len(packets)==2
    assert all(p['findings'][0]['source_verified'] for p in packets)


def test_missing_search_cannot_silently_publish_a_revision():
    with pytest.raises(ResearchContractError,match='did not perform web search'):
        packet('direction',result(search=False),sources())


def test_unanswered_search_call_does_not_count_as_execution():
    value=result();value['messages']=[value['messages'][0]]
    assert not activity(value)['searches']


def test_invented_dissent_quote_is_not_verified():
    value=packet('dissents',result(quote='An invented sentence that is not present in the opinion.'),sources())
    assert not value['findings'][0]['source_verified']
    assert value['dissent_status']=='unresolved' and value['gaps']


def test_matching_same_case_dissent_is_verified_and_labelled():
    value=packet('dissents',result(),sources())
    assert value['dissent_status']=='found'
    assert value['findings'][0]['source_verified'] and value['findings'][0]['case_match_verified']
    assert value['activity']['retrieved_urls']==[URL]


def test_unverified_case_identity_cannot_establish_a_same_case_dissent():
    value=result();body=json.loads(value['final_response']);body['findings'][0]['case_match']='Different v. Person, No. 98-765';value['final_response']=json.dumps(body)
    value=packet('dissents',value,sources())
    assert value['dissent_status']=='unresolved'


def test_partial_source_cannot_establish_absence_of_dissent():
    value=packet('dissents',result(status='not_found_after_search'),sources(truncated=True))
    assert value['dissent_status']=='unresolved'


def test_shared_step_token_budget_is_enforced():
    with pytest.raises(ResearchContractError,match='exhausted'):
        run_search_agents(dict(story={}),lambda *a:result(),sources(),dict(step_tokens=20))


def test_old_pinned_prompt_does_not_bypass_research_and_visual_builds_are_unaffected():
    job=dict(step_key='narrative',operation='revise_artifact',manifest=dict(prompts=dict(narrative='Research only a little.')))
    assert needs_source_research(job,dict(story=dict(title='Case'),instruction='Why did the dissent disagree?'))
    assert not needs_source_research(dict(job,operation='build_visuals'),dict(story={}))
    assert not needs_source_research(dict(job,step_key='visuals'),dict(story={}))


def test_partial_research_keeps_real_searches_sources_and_an_explicit_gap():
    original=result();original.update(completed=False,interrupted=True,final_response=None)
    value=packet('dissents',original,sources())
    assert not value['agent_completed'] and value['dissent_status']=='unresolved'
    assert value['activity']['searches'] and value['source_extracts'][0]['url']==URL
    assert value['gaps'] and not value['findings']


def test_research_policy_updates_legacy_queued_narratives_without_changing_engine():
    from phase2.registry import snapshot,apply_research_policy
    manifest=snapshot();manifest.pop('research_policy_version');manifest['budgets'].update(step_tokens=120000,run_tokens=140000)
    engine=dict(manifest['engine']);prompt=manifest['prompts']['narrative']
    effective=apply_research_policy(manifest)
    assert effective['budgets']['step_tokens']==300000 and effective['budgets']['run_tokens']==400000
    assert effective['engine']==engine and effective['prompts']['narrative']==prompt
    assert apply_research_policy(effective) is effective


def test_direction_agent_cannot_claim_unverified_dissent_either():
    value=packet('direction',result(quote='Invented dissent text not in the source document.'),sources())
    assert value['dissent_status']=='unresolved'


def test_unstructured_search_result_retains_sources_as_unverified_leads():
    value=result();value['final_response']='A lead that must be checked against the retrieved opinion.'
    value=packet('direction',value,sources())
    assert value['agent_completed'] and not value['structured_findings']
    assert value['source_extracts'][0]['url']==URL and value['gaps']
    assert value['dissent_status']=='unresolved' and not value['findings']


def test_schema_repair_reuses_completed_search_packets():
    job=dict(step_key='narrative',operation='revise_artifact')
    assert not needs_source_research(job,dict(story=dict(title='Case'),validation_repair='Repair JSON',research_packets=[dict(role='direction')]))
