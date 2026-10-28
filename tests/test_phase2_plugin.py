import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def plugin():
    path=Path(__file__).resolve().parents[1]/'integrations/hermes/stories/__init__.py'
    spec=importlib.util.spec_from_file_location('stories_test_plugin',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_registers_commands_tools_and_guards():
    module=plugin()
    seen={}
    ctx=SimpleNamespace(register_command=lambda name,*a,**kw:seen.setdefault('commands',[]).append(name),register_hook=lambda name,*a:seen.setdefault('hooks',[]).append(name),register_tool=lambda **kw:seen.setdefault('tools',[]).append(kw['name']))
    module.register(ctx)
    assert seen=={'commands':['story'],'hooks':['pre_gateway_dispatch','pre_tool_call'],'tools':['stories_read_artifact']}


def test_discussion_tools_cannot_bypass_operations(monkeypatch):
    module=plugin()
    values={'HERMES_SESSION_PLATFORM':'telegram','HERMES_SESSION_CHAT_ID':'-1003984316484'}
    monkeypatch.setitem(sys.modules,'gateway.session_context',SimpleNamespace(get_session_env=lambda key:values.get(key,'')))
    for tool in ['terminal','execute_code','write_file','browser_click','delegate_task','send_message','cronjob','skill_manage']:
        assert module.guard_discussion_tools(tool)['action']=='block'
    assert module.guard_discussion_tools('stories_read_artifact') is None
    values['HERMES_SESSION_CHAT_ID']='different-group'
    assert module.guard_discussion_tools('terminal') is None


@pytest.mark.parametrize('rejected',[False,True])
def test_backend_failure_consumes_command_without_llm_fallback(monkeypatch,rejected):
    module=plugin()
    monkeypatch.setenv('PHASE2_ALLOWED_USERS','42')
    error=module.StoriesRejection('Finish or cancel: /story cancel run-1') if rejected else ConnectionError()
    monkeypatch.setattr(module,'call_backend',lambda _:(_ for _ in []).throw(error))
    source=SimpleNamespace(platform='telegram',chat_id='-1003984316484',thread_id='3',user_id='42')
    event=SimpleNamespace(source=source,text='/story revise A narrower question',message_id='5')
    adapter=SimpleNamespace(send=AsyncMock())
    gateway=SimpleNamespace(adapters={'telegram':adapter},_is_user_authorized=lambda _:True)
    async def invoke():
        result=module.dispatch_event(event,gateway)
        await asyncio.sleep(0)
        return result
    assert asyncio.run(invoke())['action']=='skip'
    adapter.send.assert_awaited_once()
    assert 'not accepted' in adapter.send.call_args.args[1]
    assert ('/story cancel run-1' in adapter.send.call_args.args[1]) is rejected


def test_untrusted_user_cannot_dispatch(monkeypatch):
    module=plugin()
    monkeypatch.setenv('PHASE2_ALLOWED_USERS','42')
    monkeypatch.setattr(module,'call_backend',lambda _:pytest.fail('Unauthorized dispatch'))
    event=SimpleNamespace(source=SimpleNamespace(platform='telegram',chat_id='-1003984316484',thread_id='3',user_id='99'),text='/story refresh evidence',message_id='5')
    assert module.dispatch_event(event,SimpleNamespace(_is_user_authorized=lambda _:True))['action']=='skip'


@pytest.mark.parametrize('command,expected', [
    ('/story help', 'Visual narrative'),
    ('/story@StoriesBot help', 'Visual narrative'),
    ('/story help research', 'Visual narrative'),
    ('/story help unknown', 'Use /story help'),
])
def test_help_is_readable_and_does_not_call_backend(monkeypatch, command, expected):
    module=plugin()
    monkeypatch.setenv('PHASE2_ALLOWED_USERS','42')
    monkeypatch.setattr(module,'call_backend',lambda _:pytest.fail('Help must not call backend'))
    source=SimpleNamespace(platform='telegram',chat_id='-1003984316484',thread_id='3',user_id='42')
    adapter=SimpleNamespace(send=AsyncMock())
    gateway=SimpleNamespace(adapters={'telegram':adapter},_is_user_authorized=lambda _:True)
    async def invoke():
        result=module.dispatch_event(SimpleNamespace(source=source,text=command,message_id='5'),gateway)
        await asyncio.sleep(0)
        return result
    assert asyncio.run(invoke())['action']=='skip'
    adapter.send.assert_awaited_once()
    sent=adapter.send.call_args
    assert expected in sent.args[1]
    assert len(sent.args[1]) < 4000
    assert sent.kwargs['metadata']=={'thread_id':'3'}


@pytest.mark.parametrize('change', [dict(user_id='99'),dict(chat_id='other'),dict(thread_id=None)])
def test_help_retains_authorization_checks(monkeypatch, change):
    module=plugin()
    monkeypatch.setenv('PHASE2_ALLOWED_USERS','42')
    monkeypatch.setattr(module,'call_backend',lambda _:pytest.fail('Unauthorized help'))
    values=dict(platform='telegram',chat_id='-1003984316484',thread_id='3',user_id='42')
    values.update(change)
    adapter=SimpleNamespace(send=AsyncMock())
    gateway=SimpleNamespace(adapters={'telegram':adapter},_is_user_authorized=lambda _:True)
    event=SimpleNamespace(source=SimpleNamespace(**values),text='/story help',message_id='5')
    assert module.dispatch_event(event,gateway)['action']=='skip'
    adapter.send.assert_not_called()
