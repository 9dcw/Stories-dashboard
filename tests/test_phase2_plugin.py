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


def test_backend_failure_consumes_command_without_llm_fallback(monkeypatch):
    module=plugin()
    monkeypatch.setenv('PHASE2_ALLOWED_USERS','42')
    monkeypatch.setattr(module,'call_backend',lambda _:(_ for _ in []).throw(ConnectionError()))
    source=SimpleNamespace(platform='telegram',chat_id='-1003984316484',thread_id='3',user_id='42')
    event=SimpleNamespace(source=source,text='/story revise angles A narrower question',message_id='5')
    adapter=SimpleNamespace(send=AsyncMock())
    gateway=SimpleNamespace(adapters={'telegram':adapter},_is_user_authorized=lambda _:True)
    async def invoke():
        result=module.dispatch_event(event,gateway)
        await asyncio.sleep(0)
        return result
    assert asyncio.run(invoke())['action']=='skip'
    adapter.send.assert_awaited_once()
    assert 'not accepted' in adapter.send.call_args.args[1]


def test_untrusted_user_cannot_dispatch(monkeypatch):
    module=plugin()
    monkeypatch.setenv('PHASE2_ALLOWED_USERS','42')
    monkeypatch.setattr(module,'call_backend',lambda _:pytest.fail('Unauthorized dispatch'))
    event=SimpleNamespace(source=SimpleNamespace(platform='telegram',chat_id='-1003984316484',thread_id='3',user_id='99'),text='/story refresh evidence',message_id='5')
    assert module.dispatch_event(event,SimpleNamespace(_is_user_authorized=lambda _:True))['action']=='skip'
