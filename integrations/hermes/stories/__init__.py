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



HELP = """Visual narrative — use in the story's Telegram topic.

/story revise <guidance> — research, develop the narrative, or find visual assets
/story prompt update <feedback> — append standing feedback for future first drafts
/story prompt show — display the current initial-development prompt

Promotion creates one visual narrative Doc. Revisions update that same Doc and preserve your guidance verbatim. Prompt feedback is global; it does not revise existing stories. Ordinary chat does not change the document. Human edits/comments are protected.
"""
RESEARCH_HELP = HELP


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
    ctx.register_command('story',lambda args:'Use /story in a registered Stories topic.',description='Develop the visual narrative',args_hint='revise | prompt update | prompt show')
    ctx.register_hook('pre_gateway_dispatch',dispatch_event)
    ctx.register_hook('pre_tool_call',guard_discussion_tools)
    ctx.register_tool(name='stories_read_artifact',toolset='stories',schema={'name':'stories_read_artifact','description':'Read one published research artifact for the current Stories topic. Human Doc edits require explicit import.','parameters':{'type':'object','properties':{'artifact':{'type':'string','enum':['narrative']}},'required':['artifact'],'additionalProperties':False}},handler=read_artifact)


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
        if args.get('artifact') not in {'narrative'}:
            raise ValueError('Unknown artifact')
        result=call_backend(dict(chat_id=get_session_env('HERMES_SESSION_CHAT_ID'),thread_id=get_session_env('HERMES_SESSION_THREAD_ID'),user_id=get_session_env('HERMES_SESSION_USER_ID'),message_id='read-'+uuid.uuid4().hex,text='/story read '+args['artifact']))
        return json.dumps(result,ensure_ascii=False)
    except Exception:
        return json.dumps({'error':'Visual narrative could not be read; open its Google Doc from the story card.'})


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
        rendered=result['prompt'] if 'prompt' in result else json.dumps(result,ensure_ascii=False)
        if 'prompt' in result: rendered='Initial narrative prompt v'+str(result['version'])+'\n'+rendered
        async def send_result():
            adapter=gateway.adapters.get(source.platform)
            if adapter:
                for offset in range(0,len(rendered),1800):
                    await adapter.send(source.chat_id,rendered[offset:offset+1800],metadata={'thread_id':source.thread_id})
        asyncio.get_running_loop().create_task(send_result())
    except StoriesRejection as exc:
        reply('Stories operation was not accepted: '+str(exc))
    except Exception:
        # Consume the explicit command even when the service is unavailable: no LLM fallback.
        reply('Stories operation was not accepted. Check progress on the dashboard and use /story help for the supported commands.')
    return {'action':'skip','reason':'explicit Stories command handled'}
