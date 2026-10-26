"""Installed Hermes plugin. Never modifies Hermes core or consumes another bot's updates."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
import urllib.request
import urllib.error
from urllib.parse import urlsplit



HELP = """Story commands — use in the story's Telegram topic.

/story status — progress, current versions, stale flags and proposals
/story read evidence — read a published artifact and get its Doc link
/story reconcile <direction> — assess the whole package and update documents as needed
/story decision instruction <text> — save lasting editorial direction
/story decision approve|reject|question|note <text> — save a decision or context
/story revise <artifact> <instruction> — revise one document
/story investigate evidence|visuals <instruction> — commission scoped research
/story refresh <artifact> [instruction] — update one document with current inputs
/story review treatment [instruction] — generate a Quality Review
/story create <artifact> [instruction] — create a missing artifact

Artifacts: angles, plan, evidence, visuals, treatment, review.

/story supersede <decision-id> <text> — replace a saved decision
/story import <artifact> — capture Doc text as unverified human notes
/story append <version-id> — adopt a reviewed retained proposal
/story retry — retry the latest blocked run; cannot override a candidate conflict
/story cancel <run-id> — stop a run, retaining completed documents
/story start — initial package; use scoped operations after v1

Decisions save direction; they do not launch research. Ordinary chat does not change the package. Replace <placeholders> with your text or the returned ID.

/story help research — post-v1 workflow and examples"""

RESEARCH_HELP = """Direct research after v1

1. Inspect the package:
/story status
/story read review
/story read evidence

2. Save lasting direction when needed:
/story decision instruction Distinguish refunds ordered from refunds actually paid.

Decisions update the Hub and mark all existing research artifacts potentially stale. They do not launch research. A decision question records a gap; investigate commissions work on it.

For comprehensive editorial direction, use one package command:
/story reconcile Reassess every document against the standing direction. Focus on policyholder consequences and distinguish ordered refunds from payments. Update only what needs changing and identify missing evidence.

Reconcile reads actual Docs and structured evidence, works through dependencies, keeps suitable documents unchanged, revises or creates others, and ends with a full Quality Review. It can make substantial rewrites. It pauses for human-edit conflicts with retained proposals; it does not overwrite annotations or grant publication approval. Finish or cancel any active/blocked research run first. The instruction applies to this run; save a decision instruction separately for lasting direction.

3. Commission a specific follow-up:
/story investigate evidence Find the compliance filings. Establish whether ordered refunds were paid, cite the supporting passages, and leave missing figures unresolved.

4. Wait for completion, then inspect:
/story status
/story read evidence

5. Update the affected documents, waiting for each operation to finish:
/story refresh treatment Reflect the new evidence and remaining uncertainty.
/story review treatment Check that payment claims match the evidence.

Each operation updates ONE research artifact. Refresh does not rebuild its descendants. Dependencies:
angles -> plan
angles + plan -> evidence
plan -> visuals
angles + evidence + visuals -> treatment
all five -> review

For a changed angle or new standing decision, work through angles, plan, evidence and visuals, treatment, then review. Evidence and visuals can run alongside each other after the plan; wait for both before treatment. Check stale flags at the end.

To change presentation:
/story revise treatment Open with the documented policyholder consequence. Identify missing interview material as reporting needs.

Give research a question, source priority and completion criterion. Save decisions before starting work; changed inputs or decisions can block an active run.

Doc edits/comments are not automatically research instructions. Import captures unverified notes; inspect retained proposals before using append. Retry cannot override human-edit conflicts.

A completed run does not mean all facts are verified or publication is approved.
/story help — command reference"""


def help_response(text):
    words = text.strip().split()
    if len(words) < 2 or words[1] != 'help':
        return None
    if len(words) == 2:
        return HELP
    if words[2:] == ['research']:
        return RESEARCH_HELP
    return 'Use /story help or /story help research.'


def register(ctx):
    # Register for discoverability; the event hook handles the trusted source context.
    ctx.register_command('story',lambda args:'Use /story in a registered Stories topic.',description='Research artifacts and editorial decisions',args_hint='help | help research | <operation>')
    ctx.register_hook('pre_gateway_dispatch',dispatch_event)
    ctx.register_hook('pre_tool_call',guard_discussion_tools)
    ctx.register_tool(name='stories_read_artifact',toolset='stories',schema={'name':'stories_read_artifact','description':'Read one published research artifact for the current Stories topic. Human Doc edits require explicit import.','parameters':{'type':'object','properties':{'artifact':{'type':'string','enum':['angles','plan','evidence','visuals','treatment','review']}},'required':['artifact'],'additionalProperties':False}},handler=read_artifact)


def guard_discussion_tools(tool_name,**kwargs):
    from gateway.session_context import get_session_env
    if get_session_env('HERMES_SESSION_PLATFORM')!='telegram' or get_session_env('HERMES_SESSION_CHAT_ID')!=os.environ.get('PHASE2_CHAT_ID','-1003984316484'):
        return None
    if tool_name not in {'web_search','web_extract','vision_analyze','clarify','stories_read_artifact'}:
        return {'action':'block','message':'Stories discussion is read-only. Propose an explicit /story command for artifact changes; do not execute alternative shell, browser, file, messaging or delegation operations.'}


class StoriesRejection(RuntimeError):
    """A bounded, user-facing validation response from the local Stories API."""


def call_backend(payload):
    endpoint=os.environ.get('PHASE2_API_URL','http://127.0.0.1:8000/api/v1/internal/phase2/dispatch')
    parsed=urlsplit(endpoint)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password or parsed.path!='/api/v1/internal/phase2/dispatch':
        raise ValueError('Stories endpoint must be the configured loopback dispatch route')
    secret=os.environ.get('PHASE2_SERVICE_SECRET','')
    if len(secret)<32:
        raise ValueError('Stories service authentication is unavailable')
    body=json.dumps(payload,separators=(',',':')).encode()
    timestamp=str(int(time.time()))
    signature=hmac.new(secret.encode(),timestamp.encode()+b'\n'+body,hashlib.sha256).hexdigest()
    request=urllib.request.Request(endpoint,data=body,headers={'Content-Type':'application/json','X-Stories-Timestamp':timestamp,'X-Stories-Signature':signature})
    try:
        with urllib.request.urlopen(request,timeout=2) as response:
            return json.load(response)['result']
    except urllib.error.HTTPError as exc:
        if exc.code in {400,409}:
            try:
                detail=json.loads(exc.read(8000)).get('detail')
            except (ValueError,TypeError):
                detail=None
            if isinstance(detail,str):
                raise StoriesRejection(detail[:3000]) from None
        raise


def read_artifact(args,**kwargs):
    try:
        from gateway.session_context import get_session_env
        if isinstance(args,str):
            args=json.loads(args)
        if args.get('artifact') not in {'angles','plan','evidence','visuals','treatment','review'}:
            raise ValueError('Unknown artifact')
        result=call_backend(dict(chat_id=get_session_env('HERMES_SESSION_CHAT_ID'),thread_id=get_session_env('HERMES_SESSION_THREAD_ID'),user_id=get_session_env('HERMES_SESSION_USER_ID'),message_id='read-'+uuid.uuid4().hex,text='/story read '+args['artifact']))
        return json.dumps(result,ensure_ascii=False)
    except Exception:
        return json.dumps({'error':'Selected artifact could not be read; use /story status.'})


def dispatch_event(event,gateway,**kwargs):
    source=event.source
    platform=getattr(source.platform,'value',source.platform)
    if platform!='telegram' or not event.text or event.text.split(maxsplit=1)[0].split('@')[0].lower()!='/story':
        return None
    def reply(text):
        adapter=gateway.adapters.get(source.platform)
        if adapter:
            asyncio.get_running_loop().create_task(adapter.send(source.chat_id,text[:4000],metadata={'thread_id':source.thread_id}))
    try:
        allowed={x.strip() for x in os.environ.get('PHASE2_ALLOWED_USERS','').split(',') if x.strip()}
        if str(source.user_id) not in allowed or str(source.chat_id)!=os.environ.get('PHASE2_CHAT_ID','-1003984316484') or not source.thread_id or not gateway._is_user_authorized(source):
            return {'action':'skip','reason':'unauthorized Stories command'}
        if not event.message_id:
            raise ValueError('Missing Telegram message identity')
        help_text = help_response(event.text)
        if help_text is not None:
            reply(help_text)
            return {'action':'skip','reason':'Stories help handled'}
        payload=dict(chat_id=str(source.chat_id),thread_id=str(source.thread_id),user_id=str(source.user_id),message_id=str(event.message_id),text=event.text)
        result=call_backend(payload)
        reply(json.dumps(result,ensure_ascii=False))
    except StoriesRejection as exc:
        reply('Stories operation was not accepted: '+str(exc))
    except Exception:
        # Consume the explicit command even when the service is unavailable: no LLM fallback.
        reply('Stories operation was not accepted. Check /story status after the backend is available; do not repeat writes through shell tools.')
    return {'action':'skip','reason':'explicit Stories command handled'}
