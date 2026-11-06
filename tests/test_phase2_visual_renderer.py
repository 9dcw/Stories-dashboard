import json
import sys
from types import SimpleNamespace

import pytest

from phase2 import visual_renderer as renderer


@pytest.fixture
def providers(monkeypatch,tmp_path):
    state=dict(selected=None,fal=False)
    backends={}
    monkeypatch.delenv('PHASE2_IMAGE_PROVIDER',raising=False)
    monkeypatch.setitem(sys.modules,'tools.image_generation_tool',SimpleNamespace(
        _read_configured_image_provider=lambda:state['selected'],
        _read_configured_image_model=lambda:None,
        check_fal_api_key=lambda:state['fal']))
    monkeypatch.setitem(sys.modules,'hermes_cli.plugins',SimpleNamespace(_ensure_plugins_discovered=lambda:None))
    monkeypatch.setitem(sys.modules,'agent.image_gen_registry',SimpleNamespace(get_provider=backends.get))
    monkeypatch.setitem(sys.modules,'hermes_cli.config',SimpleNamespace(get_hermes_home=lambda:tmp_path))
    monkeypatch.setitem(sys.modules,'dotenv',SimpleNamespace(load_dotenv=lambda *a,**kw:None))
    return state,backends


def test_preflight_selects_available_provider_instead_of_unconfigured_fal(providers):
    state,backends=providers
    codex=SimpleNamespace(is_available=lambda:True,name='openai-codex')
    backends.update({'openai-codex':codex,'openai':SimpleNamespace(is_available=lambda:True)})
    assert renderer.select_provider() is codex


@pytest.mark.parametrize('selected',['fal','unknown','openai'])
def test_explicit_unavailable_selection_does_not_fall_back(providers,selected):
    state,backends=providers;state['selected']=selected
    backends['openai-codex']=SimpleNamespace(is_available=lambda:True)
    with pytest.raises(renderer.RenderFailure) as caught: renderer.select_provider()
    assert caught.value.retry_safe


def test_configured_fal_is_retained(providers):
    state,_=providers;state['fal']=True
    assert renderer.select_provider() is None


def test_provider_response_survives_failed_download_and_is_reused(providers,tmp_path,monkeypatch):
    _,backends=providers;calls=[]
    def generate(**kwargs):
        calls.append(kwargs)
        return dict(success=True,image='https://images.example.test/output.png',provider='openai-codex',model='test')
    backends['openai-codex']=SimpleNamespace(name='openai-codex',is_available=lambda:True,generate=generate)
    request=tmp_path/'request.json';result=tmp_path/'result.json'
    request.write_text(json.dumps(dict(source=str(tmp_path),repository=str(tmp_path),host_model='supported-host',plan=dict(kind='generate',prompt='A diagram',aspect_ratio='landscape'))))
    import phase2.source_reader
    monkeypatch.setattr(phase2.source_reader,'fetch_asset',lambda url:(_ for _ in ()).throw(ConnectionError('download failed')))
    with pytest.raises(ConnectionError): renderer.child(str(request),str(result))
    assert len(calls)==1
    saved=json.loads((tmp_path/'provider-response.json').read_text())
    assert saved['image']=='https://images.example.test/output.png'
    from io import BytesIO
    from PIL import Image
    stream=BytesIO();Image.new('RGB',(4,4)).save(stream,format='PNG')
    monkeypatch.setattr(phase2.source_reader,'fetch_asset',lambda url:(stream.getvalue(),'image/png',url))
    renderer.child(str(request),str(result))
    assert len(calls)==1 and result.exists()
    assert (tmp_path/'asset').read_bytes()==stream.getvalue()


def test_safe_failure_checkpoint_does_not_survive_a_new_unknown_request(providers,tmp_path):
    _,backends=providers
    def unknown(**kwargs): raise ConnectionError('Unknown request outcome')
    backends['openai-codex']=SimpleNamespace(name='openai-codex',is_available=lambda:True,generate=unknown)
    request=tmp_path/'request.json';result=tmp_path/'result.json'
    request.write_text(json.dumps(dict(source=str(tmp_path),repository=str(tmp_path),host_model='supported-host',plan=dict(kind='generate',prompt='A diagram',aspect_ratio='landscape'))))
    renderer.checkpoint(tmp_path/'failure.json',dict(retry_safe=True,error_type='auth_required'))
    with pytest.raises(ConnectionError): renderer.child(str(request),str(result))
    assert not (tmp_path/'failure.json').exists()
    assert (tmp_path/'provider-request.json').exists()


def test_successful_provider_reference_is_recoverable(tmp_path):
    instance=renderer.Renderer('python','source',tmp_path)
    folder=tmp_path/'marker';folder.mkdir()
    renderer.checkpoint(folder/'provider-response.json',dict(success=True,image='/cache/saved.png'))
    assert instance.recoverable('marker') and not instance.cached('marker')


def test_codex_uses_workflow_host_instead_of_obsolete_adapter_default(providers,tmp_path,monkeypatch):
    _,backends=providers
    module=sys.modules[__name__]
    monkeypatch.setattr(module,'_CODEX_CHAT_MODEL','obsolete-host',raising=False)
    class Provider:
        name='openai-codex'
        def is_available(self): return True
        def generate(self,**kwargs):
            assert module._CODEX_CHAT_MODEL=='supported-host'
            return dict(success=False,error_type='auth_required')
    backends['openai-codex']=Provider()
    request=tmp_path/'request.json';result=tmp_path/'result.json'
    request.write_text(json.dumps(dict(source=str(tmp_path),repository=str(tmp_path),host_model='supported-host',plan=dict(kind='generate',prompt='A diagram',aspect_ratio='landscape'))))
    with pytest.raises(renderer.RenderFailure) as caught: renderer.child(str(request),str(result))
    assert caught.value.retry_safe


def test_missing_codex_host_fails_before_provider_request(providers,tmp_path):
    _,backends=providers
    backends['openai-codex']=SimpleNamespace(name='openai-codex',is_available=lambda:True)
    request=tmp_path/'request.json';result=tmp_path/'result.json'
    request.write_text(json.dumps(dict(source=str(tmp_path),repository=str(tmp_path),plan=dict(kind='generate',prompt='A diagram',aspect_ratio='landscape'))))
    with pytest.raises(renderer.RenderFailure) as caught: renderer.child(str(request),str(result))
    assert caught.value.retry_safe and not (tmp_path/'provider-request.json').exists()


def test_rejected_api_request_is_safe_and_error_text_is_not_persisted(providers,tmp_path):
    _,backends=providers
    backends['openai-codex']=SimpleNamespace(name='openai-codex',is_available=lambda:True,generate=lambda **kw:dict(success=False,error_type='api_error',error='Error code: 400 model_not_found private-secret'))
    request=tmp_path/'request.json';result=tmp_path/'result.json'
    request.write_text(json.dumps(dict(source=str(tmp_path),repository=str(tmp_path),host_model='supported-host',plan=dict(kind='generate',prompt='A diagram',aspect_ratio='landscape'))))
    with pytest.raises(renderer.RenderFailure) as caught: renderer.child(str(request),str(result))
    assert caught.value.retry_safe
    failure=json.loads((tmp_path/'failure.json').read_text())
    assert failure['http_status']==400 and failure['reason']=='model_not_found'
    assert all('private-secret' not in p.read_text() for p in tmp_path.glob('*.json'))
