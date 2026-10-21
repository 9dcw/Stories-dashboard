import socket

import pytest

from phase2.source_reader import ArticleText,public_url,fetch


@pytest.mark.parametrize('url',['file:///etc/passwd','http://user:password@example.org','http://example.org:8000','https://example.org/?access_token=secret'])
def test_nonpublic_or_credential_urls_rejected_before_network(url):
    with pytest.raises(ValueError):
        public_url(url)


@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','10.0.0.1','::1','192.168.1.1'])
def test_resolved_private_addresses_are_blocked(monkeypatch,address):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_a,**_k:[(None,None,None,None,(address,443))])
    with pytest.raises(ValueError,match='private'):
        public_url('https://source.example.org')


class Response:
    encoding='utf-8'
    def __init__(self,status=200,headers=None,chunks=None):
        self.status_code=status;self.headers=headers or {'Content-Type':'text/html'}
        self.chunks=chunks or [b'<title>Public record</title><script>ignore me</script><p>Established finding</p><a href="/filing.pdf">Underlying filing</a>']
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def raise_for_status(self):pass
    def iter_content(self,*_):return iter(self.chunks)


def test_fetch_retains_source_hash_links_and_text(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_a,**_k:[(None,None,None,None,('93.184.216.34',443))])
    calls=[]
    def get(url,**kwargs):
        calls.append(kwargs);return Response()
    monkeypatch.setattr('phase2.source_reader.requests.get',get)
    value=fetch('https://source.example.org/article')
    assert len(value['sha256'])==64
    assert value['links']==['https://source.example.org/filing.pdf']
    assert 'Established finding' in value['text'] and 'ignore me' not in value['text']
    assert calls[0]['allow_redirects'] is False
    assert calls[0]['auth']


def test_redirects_cannot_target_private_services(monkeypatch):
    def resolve(host,*_a,**_k):
        return [(None,None,None,None,('127.0.0.1' if host=='localhost' else '93.184.216.34',443))]
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    calls=[]
    def get(url,**kwargs):
        calls.append(url);return Response(302,{'Location':'http://localhost/'})
    monkeypatch.setattr('phase2.source_reader.requests.get',get)
    with pytest.raises(ValueError,match='private'):
        fetch('https://source.example.org/redirect')
    assert len(calls)==1


def test_oversized_sources_are_rejected(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *_a,**_k:[(None,None,None,None,('93.184.216.34',443))])
    monkeypatch.setattr('phase2.source_reader.requests.get',lambda *_a,**_k:Response(chunks=[b'x'*250000]*9))
    with pytest.raises(ValueError,match='2 MB'):
        fetch('https://source.example.org/large')
