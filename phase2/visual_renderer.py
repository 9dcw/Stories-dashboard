"""Isolate the installed Hermes image provider from the workflow worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


class Renderer:
    def __init__(self,python,source,root):
        self.python,self.source,self.root=str(python),str(source),Path(root)

    def render(self,marker,plan,heartbeat):
        folder=self.root/marker
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        request=folder/'request.json'; result=folder/'result.json'
        # A completed local download can survive a crash before the SQLite receipt.
        if result.exists():
            saved=json.loads(result.read_text())
            if Path(saved['path']).is_file(): return saved['path'],saved['mime']
        request.write_text(json.dumps(dict(source=self.source,plan=plan,repository=str(Path(__file__).resolve().parents[1]))))
        request.chmod(0o600)
        process=subprocess.Popen([self.python,str(Path(__file__).resolve()),str(request),str(result)],cwd=folder,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        started=time.monotonic()
        try:
            while process.poll() is None:
                heartbeat()
                if time.monotonic()-started>900: raise TimeoutError('Visual provider timed out')
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: pass
            if process.returncode or not result.exists(): raise RuntimeError('Configured Hermes visual provider failed')
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
    if plan['kind']=='generate':
        from tools.image_generation_tool import _handle_image_generate
        result=_handle_image_generate(dict(prompt=plan['prompt'],aspect_ratio=plan['aspect_ratio']))
        if isinstance(result,str): result=json.loads(result)
        if not result.get('success') or not result.get('image'): raise RuntimeError('Image provider did not return an image')
        source=result['image']
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
    temp=Path(result_path).with_suffix('.tmp')
    temp.write_text(json.dumps(dict(path=str(output),mime=mime))); temp.chmod(0o600)
    temp.replace(result_path)


if __name__=='__main__':
    if sys.argv[1]=='--check':
        sys.path.insert(0,sys.argv[2])
        from dotenv import load_dotenv
        from hermes_cli.config import get_hermes_home
        load_dotenv(get_hermes_home()/'.env',override=False)
        from tools.image_generation_tool import check_image_generation_requirements
        sys.exit(0 if check_image_generation_requirements() else 1)
    child(*sys.argv[1:])
