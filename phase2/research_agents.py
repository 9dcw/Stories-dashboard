"""Mandatory, isolated source searches before narrative development."""
from concurrent.futures import ThreadPoolExecutor
import json
import re

from jsonschema import Draft202012Validator

VERSION='case-research.v1'
POLICY="""Research the producer's direction and questions before revising the narrative.
The mandatory search-agent packets are research leads, not editorial authority.
Use verified source quotations and retrieve further context when necessary. Unverified agent findings, including claims to have found a dissent, must not be presented as established facts; verify them or report the gap. Do not repeat completed searches or downloads merely to restate an agent finding; focus additional research on unresolved gaps.
Explicitly answer the current user question with sources, conflicting evidence,
or a concrete research gap. Look for dissenting and concurring opinions in this
same case, including separate PDFs and opinions appended after the majority.
Match caption, court, docket, date and judges; a dissent in a cited case is not
this case's dissent. Identify author, opinion posture, reasoning and relevant
facts. Distinguish dissenting arguments from the majority holding. If none was
located, say not located after the searches performed; never infer unanimity
from a snippet or a truncated PDF. Keep findings beside the relevant narrative
beats and retain all source, asset and producer-instruction provenance. Describe material source or evidence gaps in the narrative, not agent format or budget diagnostics. Do not
publish raw search logs or create additional research documents. These research
requirements apply even when an older pinned prompt suggests limited research.
"""
SCHEMA=dict(type='object',additionalProperties=False,properties={
 'summary':dict(type='string',maxLength=8000),
 'findings':dict(type='array',maxItems=12,items=dict(type='object',additionalProperties=False,properties={
   k:dict(type='string',maxLength=6000) for k in ['claim','url','quote','locator','opinion_type','case_match']},required=['claim','url','quote','locator','opinion_type','case_match'])),
 'gaps':dict(type='array',maxItems=12,items=dict(type='string',maxLength=1500)),
 'dissent_status':dict(enum=['found','not_found_after_search','not_applicable','unresolved'])},required=['summary','findings','gaps','dissent_status'])
SCHEMA['properties']['findings']['items']['properties']['opinion_type']={'enum':['majority','dissent','concurrence','reporting','other']}

def needs_source_research(job,inputs):
    return job['step_key']=='narrative' and job['operation']!='build_visuals' and bool(inputs.get('story')) and not (inputs.get('validation_repair') and inputs.get('research_packets'))


TASKS={
 'direction':"""Use web_search to investigate every factual question and research direction
in the latest user instruction and producer guidance. Start from the exact case
and its sources. Follow primary records and credible reporting, and actively
seek evidence that challenges the suggested interpretation. Read useful sources
with stories_fetch_source; a search snippet is not verification. Purely editorial
directions do not establish facts; identify any factual research they require.
For case_match copy the exact caption or docket from the source when available. Return focused findings, exact supporting quotations, source URLs and locators,
plus remaining gaps. Do not rewrite the narrative or create artifacts.""",
 'dissents':"""Use web_search to locate dissenting and concurring opinions in this exact
case. Search the caption and docket with dissent, dissenting, concurrence,
concurring, and any separate-opinion author identified. Inspect the official case
page, opinion links and the majority PDF's opinion_index. Fetch relevant later
PDF pages with page_start/page_count and look for separate opinion PDFs. For case_match copy the exact case caption or docket from the retrieved source. Match
the case identity and distinguish majority, concurrence, dissent and a dissent
from denial of rehearing. Extract additional facts and competing reasoning
relevant to the producer's question; do not mistake dissents in cited cases for
this case's opinions. If nothing is located or access fails, report the searches
and gaps honestly; do not assert no dissent exists. Read sources with
stories_fetch_source, rather than relying on search snippets."""}


class ResearchContractError(RuntimeError):
    usage=None


def activity(result):
    messages=result.get('messages',[])
    answered={m.get('tool_call_id') for m in messages if isinstance(m,dict) and m.get('role')=='tool'}
    searches=[];retrievals=[]
    for message in messages:
        if not isinstance(message,dict):continue
        for call in message.get('tool_calls',[]):
            if not isinstance(call,dict) or call.get('id') not in answered:continue
            function=call.get('function',{})
            try: args=json.loads(function.get('arguments','{}'))
            except (ValueError,TypeError):continue
            if function.get('name')=='web_search':
                searches.append(args)
            elif function.get('name')=='stories_fetch_source' and isinstance(args.get('url'),str):
                retrievals.append(args['url'])
    return dict(searches=searches,retrieved_urls=list(dict.fromkeys(retrievals)))


def add_usage(values):
    fields=['input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens']
    result={k:sum(v.get(k) or 0 for v in values) for k in fields}
    costs=[v.get('estimated_cost_usd') for v in values]
    result['estimated_cost_usd']=sum(c for c in costs if isinstance(c,(int,float))) if any(isinstance(c,(int,float)) for c in costs) else None
    result['cost_status']='estimated' if all(isinstance(c,(int,float)) for c in costs) else 'unknown_or_partial'
    result['cost_source']='sum of isolated research agents and narrative agent'
    return result


def packet(role,result,snapshots):
    calls=activity(result)
    complete=bool(result.get('completed')) and not result.get('interrupted')
    if not calls['searches']:
        raise ResearchContractError('A mandatory search agent did not perform web search; no narrative was published')
    formatted=complete
    if complete:
        try:
            text=re.sub(r'^```(?:json)?\s*|\s*```$','',str(result.get('final_response','')).strip())
            value=json.loads(text);Draft202012Validator(SCHEMA).validate(value)
        except Exception:
            formatted=False
            value=dict(summary=str(result.get('final_response',''))[:8000],findings=[],gaps=['Search-agent findings were unstructured. Treat the summary as unverified leads and verify against the retrieved source extracts before use.'],dissent_status='unresolved')
    else:
        value=dict(summary='The search agent performed web research but reached its bounded work limit before final synthesis.',findings=[],gaps=['Search-agent research is partial. Use the retrieved source extracts and perform any necessary follow-up; do not claim the investigation or dissent search is exhaustive.'],dissent_status='unresolved')
    sources=[s for s in snapshots if s.get('requested_url',s['url']) in calls['retrieved_urls'] or s['url'] in calls['retrieved_urls']]
    if not formatted:
        value['source_extracts']=[{k:s[k] for k in ['url','requested_url','sha256','opinion_index','total_pages','page_start','page_end'] if k in s}|dict(text=s['text'][:6000],truncated=s.get('truncated',False) or len(s['text'])>6000) for s in sources[:4]]
    for finding in value['findings']:
        matches=[s for s in sources if finding['url'] in {s['url'],s.get('requested_url')}]
        quote=' '.join(finding['quote'].split()).casefold()
        identity=' '.join(finding['case_match'].split()).casefold()
        finding['case_match_verified']=len(identity)>=10 and any(identity in ' '.join(s['text'].split()).casefold() for s in matches)
        finding['source_verified']=len(quote)>=20 and any(quote in ' '.join(s['text'].split()).casefold() for s in matches)
        if not finding['source_verified']:
            value['gaps'].append('A search-agent finding lacks a retrieved, matching source quotation; verify before using it as fact.')
    if value['dissent_status'] in {'found','not_found_after_search'}:
        found=any(f['source_verified'] and f['opinion_type'].lower()=='dissent' and f['case_match_verified'] for f in value['findings'])
        if value['dissent_status']=='found' and not found:
            value['dissent_status']='unresolved';value['gaps'].append('No retrieved same-case dissent passage supported the claimed dissent.')
        if value['dissent_status']=='not_found_after_search' and (not sources or all(s.get('truncated') for s in sources)):
            value['dissent_status']='unresolved';value['gaps'].append('Incomplete source access cannot establish absence of a separate opinion.')
    return dict(role=role,agent_completed=complete,structured_findings=formatted,**value,activity=calls,usage={k:result.get(k) for k in ['input_tokens','output_tokens','total_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens','estimated_cost_usd']})


def run_search_agents(inputs,run_one,snapshots,budget):
    context={k:inputs[k] for k in ['story','instruction','research_brief','producer_guidance','current_output','legacy_sources'] if k in inputs}
    if isinstance(context.get('current_output'),dict):
        context['current_output']={k:context['current_output'][k] for k in ['summary','sources','claims','gaps'] if k in context['current_output']}
    def run(role):
        prompt=POLICY+'\n'+TASKS[role]+'\nReturn only JSON matching:\n'+json.dumps(SCHEMA)
        return role,run_one(role,prompt,context,min(60000,budget['step_tokens']//3))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(run,TASKS))
    try:
        packets=[packet(role,result,snapshots) for role,result in results]
    except ResearchContractError as exc:
        exc.usage=add_usage([result for _,result in results])
        raise
    usage=add_usage([p['usage'] for p in packets])
    if usage['total_tokens']>=budget['step_tokens']:
        error=ResearchContractError('Search-agent research exhausted the step budget; no narrative was published');error.usage=usage;raise error
    return packets,usage
