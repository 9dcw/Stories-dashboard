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
from urllib.parse import urlsplit


def register(ctx):
    # Register for discoverability; the event hook handles the trusted source context.
    ctx.register_command('story',lambda args:'Use /story in a registered Stories topic.',description='Research artifacts and editorial decisions',args_hint='<operation>')
    ctx.register_hook('pre_gateway_dispatch',dispatch_event)
    ctx.register_hook('pre_tool_call',guard_discussion_tools)
    ctx.register_tool(name='stories_read_artifact',toolset='stories',schema={'name':'stories_read_artifact','description':'Read one published research artifact for the current Stories topic. Human Doc edits require explicit import.','parameters':{'type':'object','properties':{'artifact':{'type':'string','enum':['angles','plan','evidence','visuals','treatment','review']}},'required':['artifact'],'additionalProperties':False}},handler=read_artifact)


def guard_discussion_tools(tool_name,**kwargs):
    from gateway.session_context import get_session_env
    if get_session_env('HERMES_SESSION_PLATFORM')!='telegram' or get_session_env('HERMES_SESSION_CHAT_ID')!=os.environ.get('PHASE2_CHAT_ID','-1003984316484'):
        return None
    if tool_name not in {'web_search','web_extract','vision_analyze','clarify','stories_read_artifact'}:
        return {'action':'block','message':'Stories discussion is read-only. Propose an explicit /story command for artifact changes; do not execute alternative shell, browser, file, messaging or delegation operations.'}


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
    with urllib.request.urlopen(request,timeout=2) as response:
        return json.load(response)['result']


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
        payload=dict(chat_id=str(source.chat_id),thread_id=str(source.thread_id),user_id=str(source.user_id),message_id=str(event.message_id),text=event.text)
        result=call_backend(payload)
        reply(json.dumps(result,ensure_ascii=False))
    except Exception:
        # Consume the explicit command even when the service is unavailable: no LLM fallback.
        reply('Stories operation was not accepted. Check /story status after the backend is available; do not repeat writes through shell tools.')
    return {'action':'skip','reason':'explicit Stories command handled'}
