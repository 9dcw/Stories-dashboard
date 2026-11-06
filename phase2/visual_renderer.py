"""Isolate the installed Hermes image provider from the workflow worker."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time


class RenderFailure(RuntimeError):
    def __init__(self,message,retry_safe=False):
        super().__init__(message)
        self.retry_safe=retry_safe


def checkpoint(path,value):
    path=Path(path)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value)); temp.chmod(0o600); temp.replace(path)


def select_provider():
    """Resolve the same available backend for preflight and generation."""
    from tools.image_generation_tool import _read_configured_image_provider,check_fal_api_key
    from hermes_cli.plugins import _ensure_plugins_discovered
    from agent.image_gen_registry import get_provider
    selected=os.environ.get('PHASE2_IMAGE_PROVIDER') or _read_configured_image_provider()
    _ensure_plugins_discovered()
    if selected:
        if selected=='fal':
            if not check_fal_api_key(): raise RenderFailure('Selected FAL image provider is not configured',True)
            return None
        provider=get_provider(selected)
        if provider is None or not provider.is_available():
            raise RenderFailure('Selected image provider is unavailable',True)
        return provider
    if check_fal_api_key(): return None
    # Prefer the existing ChatGPT login; never silently fall back after a request.
    for name in ['openai-codex','openai']:
        provider=get_provider(name)
        if provider is not None and provider.is_available(): return provider
    raise RenderFailure('No configured image provider is available',True)


class Renderer:
    def __init__(self,python,source,root,host_model=""):
        self.python,self.source,self.root=str(python),str(source),Path(root)
        self.host_model=host_model

    def render(self,marker,plan,heartbeat):
        folder=self.root/marker
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        request=folder/'request.json'; result=folder/'result.json'
        # A completed local download can survive a crash before the SQLite receipt.
        if result.exists():
            saved=json.loads(result.read_text())
            if Path(saved['path']).is_file(): return saved['path'],saved['mime']
        request.write_text(json.dumps(dict(source=self.source,plan=plan,host_model=os.environ.get('PHASE2_IMAGE_HOST_MODEL') or self.host_model,repository=str(Path(__file__).resolve().parents[1]))))
        request.chmod(0o600)
        process=subprocess.Popen([self.python,str(Path(__file__).resolve()),str(request),str(result)],cwd=folder,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        started=time.monotonic()
        try:
            while process.poll() is None:
                heartbeat()
                if time.monotonic()-started>900: raise TimeoutError('Visual provider timed out')
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: pass
            if process.returncode or not result.exists():
                failure=folder/'failure.json'
                details=json.loads(failure.read_text()) if failure.exists() else {}
                kind=details.get('error_type','unknown')
                if kind not in {'auth_required','missing_dependency','invalid_argument','provider_not_registered','api_error','empty_response','RenderFailure','ValueError','TimeoutError','RuntimeError','ConnectionError','HTTPError'}:
                    kind='provider_error'
                raise RenderFailure('Visual renderer failed ('+kind+'); '+('safe to retry after correcting configuration' if details.get('retry_safe') else 'saved provider response will be reused when available'),bool(details.get('retry_safe')))
            saved=json.loads(result.read_text())
            return saved['path'],saved['mime']
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL); process.wait()

    def cached(self,marker):
        result=self.root/marker/'result.json'
        return result.exists() and Path(json.loads(result.read_text())['path']).is_file()

    def recoverable(self,marker):
        if self.cached(marker): return True
        folder=self.root/marker
        response=folder/'provider-response.json'
        if response.exists() and json.loads(response.read_text()).get('image'): return True
        failure=folder/'failure.json'
        return failure.exists() and bool(json.loads(failure.read_text()).get('retry_safe'))

    def check(self):
        result=subprocess.run([self.python,str(Path(__file__).resolve()),'--check',self.source],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45)
        return result.returncode==0


def child(request_path,result_path):
    request=json.loads(Path(request_path).read_text())
    sys.path.insert(0,request['repository']); sys.path.insert(0,request['source'])
    from dotenv import load_dotenv
    from hermes_cli.config import get_hermes_home
    load_dotenv(get_hermes_home()/'.env',override=False)
    plan=request['plan']
    folder=Path(result_path).parent
    response_path=folder/'provider-response.json'
    response=json.loads(response_path.read_text()) if response_path.exists() else {}
    if plan['kind']=='generate':
        if response.get('success') and response.get('image'):
            source=response['image']
        else:
            provider=select_provider()
            if provider is not None and provider.name=='openai-codex':
                host_model=request.get('host_model')
                if not host_model:
                    raise RenderFailure('Codex images require the workflow host model or PHASE2_IMAGE_HOST_MODEL',True)
                # The pinned adapter hardcodes an obsolete chat host. Override only
                # this isolated process with the workflow's supported Codex model.
                module=sys.modules.get(type(provider).__module__)
                if module is not None and hasattr(module,'_CODEX_CHAT_MODEL'):
                    module._CODEX_CHAT_MODEL=host_model
            (folder/'failure.json').unlink(missing_ok=True)
            checkpoint(folder/'provider-request.json',dict(provider=provider.name if provider else 'fal',phase='requesting'))
            if provider is None:
                from tools.image_generation_tool import image_generate_tool
                response=image_generate_tool(prompt=plan['prompt'],aspect_ratio=plan['aspect_ratio'])
            else:
                from tools.image_generation_tool import _read_configured_image_model
                options=dict(prompt=plan['prompt'],aspect_ratio=plan['aspect_ratio'])
                model=_read_configured_image_model()
                if model: options['model']=model
                response=provider.generate(**options)
            if isinstance(response,str): response=json.loads(response)
            if not isinstance(response,dict): raise RenderFailure('Invalid provider response')
            # Save the generated image reference BEFORE downloading or validating it.
            checkpoint(response_path,{k:response.get(k) for k in ['success','image','provider','model','error_type']})
            if not response.get('success') or not response.get('image'):
                error_type=response.get('error_type','empty_response')
                description=str(response.get('error','')).lower()
                reasons=['insufficient_quota','rate_limit_exceeded','billing_hard_limit_reached','model_not_found','unsupported_model','unsupported_parameter','unknown_parameter','invalid_api_key','account_deactivated']
                reason=next((name for name in reasons if name in description),None)
                if not reason and 'not supported' in description:
                    reason='tool_not_supported' if 'image_generation' in description else 'model_not_supported'
                status=re.search(r'Error code: (400|401|403|404|422|429)\b',str(response.get('error','')))
                safe=error_type in {'auth_required','invalid_argument','missing_dependency','provider_not_registered'} or bool(status)
                checkpoint(folder/'failure.json',dict(error_type=error_type,retry_safe=safe,http_status=int(status.group(1)) if status else None,reason=reason))
                raise RenderFailure('Image provider returned no image',safe)
            source=response['image']
    else:
        source=plan['url']
    from phase2.source_reader import fetch_asset
    if source.startswith(('https://','http://')):
        body,mime,_=fetch_asset(source)
    elif plan['kind']=='generate' and Path(source).is_absolute():
        body=Path(source).read_bytes()
        import mimetypes
        mime=mimetypes.guess_type(source)[0]
    else: raise ValueError('Invalid visual output')
    if len(body)>25*1024*1024: raise ValueError('Visual exceeds download limit')
    allowed={'image/png','image/jpeg','image/webp'} | ({'application/pdf'} if plan['kind']=='retrieve' else set())
    if mime not in allowed: raise ValueError('Unsupported visual media type')
    if mime.startswith('image/'):
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(body)) as image: image.verify()
    output=Path(result_path).parent/'asset'
    output.write_bytes(body); output.chmod(0o600)
    checkpoint(result_path,dict(path=str(output),mime=mime))


if __name__=='__main__':
    if sys.argv[1]=='--check':
        sys.path.insert(0,sys.argv[2])
        from dotenv import load_dotenv
        from hermes_cli.config import get_hermes_home
        load_dotenv(get_hermes_home()/'.env',override=False)
        try: select_provider()
        except RenderFailure: sys.exit(1)
        sys.exit(0)
    try:
        child(*sys.argv[1:])
    except Exception as exc:
        folder=Path(sys.argv[2]).parent
        failure=folder/'failure.json'
        if not failure.exists():
            request=json.loads(Path(sys.argv[1]).read_text())
            safe=getattr(exc,'retry_safe',False) or request['plan']['kind']=='retrieve' or not (folder/'provider-request.json').exists()
            checkpoint(failure,dict(error_type=type(exc).__name__,retry_safe=safe))
        sys.exit(1)
