"""Bounded public-source GET tool for installations with search-only web backends."""
from __future__ import annotations

import hashlib
from datetime import datetime,timezone
from html.parser import HTMLParser
import ipaddress
import json
import socket
from urllib.parse import urljoin,urlsplit,parse_qs

import requests


class NoAmbientAuth(requests.auth.AuthBase):
    def __call__(self,request):
        return request  # Disable .netrc authentication while retaining environment proxy policy.


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[]
        self.links=[]
        self.skip=0

    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','noscript'}:
            self.skip+=1
        if tag=='a' and not self.skip:
            href=dict(attrs).get('href')
            if href:self.links.append(href)

    def handle_endtag(self,tag):
        if tag in {'script','style','noscript'}:
            self.skip=max(0,self.skip-1)

    def handle_data(self,data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def public_url(url):
    parsed=urlsplit(url)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None,80,443}:
        raise ValueError('Only public HTTP(S) URLs on standard ports are supported')
    if any(k.lower() in {'token','api_key','key','access_token','signature'} for k in parse_qs(parsed.query)):
        raise ValueError('Credential-bearing source URLs are not supported')
    addresses=socket.getaddrinfo(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Source resolves to a private or reserved address')
    # Respect installed Hermes website policy in addition to the unconditional public-IP rule.
    try:
        from tools.website_policy import check_website_access
    except ImportError:
        return url
    if check_website_access(url):
        raise ValueError('Source is blocked by website policy')
    return url


def fetch(url):
    current=url
    for _ in range(6):
        public_url(current)
        with requests.get(current,headers={'User-Agent':'StoriesDashboard/Phase2Research','Accept':'text/html,application/pdf,text/plain,application/json'},timeout=(5,20),stream=True,allow_redirects=False,auth=NoAmbientAuth()) as response:
            if response.status_code in {301,302,303,307,308}:
                current=urljoin(current,response.headers.get('Location',''))
                continue
            response.raise_for_status()
            mime=response.headers.get('Content-Type','').split(';')[0].lower()
            if mime not in {'text/html','application/xhtml+xml','text/plain','application/json','application/pdf'}:
                raise ValueError('Unsupported source content type')
            chunks=[];size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>2_000_000:
                    raise ValueError('Source exceeds the 2 MB retrieval limit')
                chunks.append(chunk)
            body=b''.join(chunks)
            links=[]
            page_limited=False
            if mime=='application/pdf':
                import fitz
                with fitz.open(stream=body,filetype='pdf') as pdf:
                    page_limited=len(pdf)>30
                    text='\n'.join(f'Page {i+1}\n'+pdf[i].get_text() for i in range(min(len(pdf),30)))
            elif mime in {'text/html','application/xhtml+xml'}:
                parser=ArticleText();parser.feed(body.decode(response.encoding or 'utf-8',errors='replace'))
                text='\n'.join(parser.parts)
                links=list(dict.fromkeys(urljoin(current,l) for l in parser.links if urlsplit(urljoin(current,l)).scheme in {'http','https'}))[:100]
            else:
                text=body.decode('utf-8',errors='replace')
            return dict(url=current,requested_url=url,content_type=mime,sha256=hashlib.sha256(body).hexdigest(),retrieved_at=datetime.now(timezone.utc).isoformat(),text=text[:32000],truncated=len(text)>32000 or page_limited,links=links)
    raise ValueError('Source exceeded the redirect limit')


def register(snapshots):
    from tools.registry import registry
    attempts=0
    def handler(args,**kwargs):
        nonlocal attempts
        try:
            if attempts>=12:
                raise ValueError('Per-step source retrieval limit reached')
            attempts+=1
            value=fetch(args['url'])
            snapshots.append(value)
            return json.dumps(dict(success=True,**value),ensure_ascii=False)
        except Exception as exc:
            return json.dumps({'success':False,'error':'Source retrieval failed ('+type(exc).__name__+'); record the access gap without inventing evidence.'})
    registry.register(name='stories_fetch_source',toolset='stories_research',schema={'name':'stories_fetch_source','description':'Read one public article or primary record by HTTP GET. Returns bounded text, links, content hash and truncation status. No authentication, private addresses, external writes or shell execution.','parameters':{'type':'object','properties':{'url':{'type':'string'}},'required':['url'],'additionalProperties':False}},handler=handler)
