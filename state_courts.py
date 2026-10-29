"""Official Florida, Texas and California listing adapters, without editorial gates."""
import json
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from collectors import _date


class Node:
    def __init__(self, tag='', attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or []), []

    def text(self):
        return ' '.join(' '.join(c.text() if isinstance(c, Node) else c for c in self.children).split())

    def find(self, tag=None, cls=None):
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (cls is None or cls in child.attrs.get('class', '').split()):
                    yield child
                yield from child.find(tag, cls)


class Tree(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root=Node();self.stack=[self.root];self.feed(body)
    def handle_starttag(self, tag, attrs):
        n=Node(tag,attrs);self.stack[-1].children.append(n)
        if tag not in self.VOID:self.stack.append(n)
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                del self.stack[i:];break
    def handle_data(self, text):self.stack[-1].children.append(text)


def _get(url, config, fetcher):
    body,typ,resolved=fetcher(url,source_config=config,timeout=30,max_bytes=8_000_000)
    if urlsplit(resolved).hostname not in config['official_hosts']:
        raise ValueError('Listing redirected outside source host allowlist')
    return body.decode('utf8',errors='replace'),resolved


def florida_page(body, base):
    data=json.loads(body)
    if not isinstance(data.get('searchResults'),list) or not isinstance(data.get('totalCount'),int):
        raise ValueError('Florida opinion API schema changed')
    result=[]
    for row in data['searchResults']:
        fields=row['content']['fields'];opinion=fields.get('opinion')
        if not opinion or not opinion.get('uri'):
            raise ValueError('Florida record missing opinion document; review required')
        date=fields['disposition_date']['date']['date'].split(' ')[0]
        title=fields.get('case_style') or fields['case_number']
        result.append(dict(title=f"{fields['case_number']}: {title}",url=urljoin(base,opinion['uri']),date=date))
    return result,data['totalCount'],len(data['searchResults'])


def california_page(body, base):
    root=Tree(body).root;rows=[]
    for card in root.find('div','result-excerpt'):
        headings=list(card.find('h2'));dates=list(card.find('span','result-excerpt__brow-secondary'))
        pdfs=[a for a in card.find('a') if re.search(r'/opinions/documents/[^/]+\.pdf$',a.attrs.get('href',''),re.I)]
        if not headings or not dates or not pdfs:raise ValueError('California opinion card schema changed')
        rows.append(dict(title=headings[0].text(),date=_date(dates[0].text()),url=urljoin(base,pdfs[0].attrs['href'])))
    if not rows and 'No results' not in root.text():raise ValueError('California opinion listing not recognized')
    next_links=[a.attrs.get('href') for a in root.find('a') if 'next' in a.attrs.get('rel','').split() or 'pager__link--next' in a.attrs.get('class','').split() or a.attrs.get('title','').lower()=='go to next page']
    return rows,list(dict.fromkeys(urljoin(base,u) for u in next_links if u))


def texas_dates(body, base, court, now):
    root=Tree(body).root;found=[]
    for a in root.find('a'):
        u=urljoin(base,a.attrs.get('href',''));p=urlsplit(u)
        q={k.lower():v for k,v in parse_qs(p.query).items()}
        if p.path.lower() not in ['/docket.aspx','/handdown.aspx']:continue
        if q.get('coa')!=[court] or not q.get('fulldate'):continue
        day=datetime.strptime(q['fulldate'][0],'%m/%d/%Y').date()
        if now.date()-timedelta(days=30)<=day<=now.date():found.append((u,day.isoformat()))
    if 'Released Orders/Opinions' not in root.text() and not found:
        raise ValueError('Texas release calendar not recognized')
    return list(dict.fromkeys(found))


def texas_day(body, base, court, day):
    root=Tree(body).root;rows=[]
    for row in list(root.find('tr'))+list(root.find('div','rowx')):
        if not {'rgRow','rgAltRow','rowx'}.intersection(row.attrs.get('class','').split()):continue
        links=[]
        for a in row.find('a'):
            u=urljoin(base,a.attrs.get('href',''));p=urlsplit(u);q=parse_qs(p.query)
            if p.path.lower()!='/searchmedia.aspx' or q.get('DT',[''])[0].lower()!='opinion':continue
            # The official template emits a literal server variable for coa.
            # Replace only that parameter; preserve both document identifiers.
            if not q.get('MediaVersionID') or not q.get('MediaID'):raise ValueError('Texas opinion document missing identifiers')
            links.append(urlunsplit((p.scheme,p.netloc,p.path,urlencode(dict(MediaVersionID=q['MediaVersionID'][0],MediaID=q['MediaID'][0],coa=court,DT='Opinion')),'')))
        if not links:continue
        case_styles=list(row.find('td','caseStyle'))
        if court=='coscca':case_styles=[c for c in row.children if isinstance(c,Node) and 'col2' in c.attrs.get('class','').split()][:1]
        dockets=[a.text() for a in row.find('a') if 'Case.aspx?' in a.attrs.get('href','')]
        title=(case_styles[0].text() if case_styles else row.text())
        if dockets:title=dockets[0]+': '+title
        for u in links:rows.append(dict(title=title,url=u,date=day))
    if not rows and 'Released Orders' not in root.text() and 'Hand Down' not in root.text() and 'No records' not in root.text():
        raise ValueError('Texas daily release not recognized')
    return rows


def fetch_listing(source, fetcher, now=None):
    config=json.loads(source['collector_config']);adapter=config.get('listing_adapter')
    if not adapter:return fetcher(source['poll_url'],source_config=config,timeout=30,max_bytes=8_000_000)
    now=now or datetime.now(ZoneInfo('America/New_York'));base=source['poll_url'];results=[]
    if adapter=='florida':
        offset=0
        for _ in range(config.get('max_pages',40)):
            params=dict(limit=100,offset=offset,query='',searchtype='opinions',siteaccess=config['siteaccess'],
                        startdate=str(now.date()-timedelta(days=30)),enddate=str(now.date()))
            params['scopes[]']=config['scope']
            body,resolved=_get(base+'?'+urlencode(params),config,fetcher)
            rows,total,count=florida_page(body,resolved);results.extend(rows);offset+=count
            if offset>=total:break
            if not count:raise ValueError('Florida pagination ended before totalCount')
        else:raise ValueError('Florida page limit reached; source incomplete')
    elif adapter=='california':
        url=base;seen=set()
        for _ in range(config.get('max_pages',40)):
            if url in seen:raise ValueError('California pagination loop')
            seen.add(url);body,resolved=_get(url,config,fetcher);rows,nexts=california_page(body,resolved);results.extend(rows)
            if not nexts:break
            if len(nexts)!=1:raise ValueError('Ambiguous California next page')
            url=nexts[0]
            if urlsplit(url).hostname not in config['official_hosts'] or urlsplit(url).path!=urlsplit(base).path:
                raise ValueError('California next page outside listing')
        else:raise ValueError('California page limit reached; source incomplete')
    elif adapter=='texas':
        body,resolved=_get(base,config,fetcher)
        dates=texas_dates(body,resolved,config['texas_court'],now)
        for url,day in dates:
            body,resolved=_get(url,config,fetcher);results.extend(texas_day(body,resolved,config['texas_court'],day))
    else:raise ValueError('Unknown state listing adapter')
    unique={r['url']:r for r in results}
    return json.dumps(list(unique.values())).encode(),'application/json',base
