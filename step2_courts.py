"""Official Illinois First District and Pennsylvania Superior Court listings."""
import json
import re
from datetime import timedelta
from urllib.parse import quote, urlencode, urljoin, urlsplit

from collectors import _date
from state_courts import Tree


def specs():
    return [dict(name='Illinois First District Appellate Court opinions', court_id='illappct1',
        jurisdiction='US-IL', url='https://www.illinoiscourts.gov/top-level-opinions?d=1&t=appellate',
        collector_type='json', hosts=['www.illinoiscourts.gov','ilcourtsaudio.blob.core.windows.net'],
        pattern=r'/resources/[a-f0-9-]+/file(?:\?|$)',
        scope='First District only; opinions and Rule 23 orders, rolling 30 days, descending filing dates. Rule 23 status retained for existing nonprecedential exclusion.',
        config=dict(listing_adapter='illinois_first',max_pages=40)),
        dict(name='Pennsylvania Superior Court opinions', court_id='pasuperct', jurisdiction='US-PA',
        url='https://www.pacourts.us/api/opinion', collector_type='json', hosts=['www.pacourts.us','wwwsecure.pacourts.us'],
        pattern=r'/assets/opinions/Superior/out/[^/?]+\.pdf(?:\?|$)',
        scope='Superior Court only; rolling 30 days, every API page. Individual opinion/memorandum/order documents including separate opinions; aggregate judgment lists and reargument tables excluded. Publication status retained for existing exclusions.',
        config=dict(listing_adapter='pennsylvania_superior',max_pages=80))]


def illinois_page(body, base):
    root=Tree(body).root
    tables=[n for n in root.find('table') if n.attrs.get('id')=='ctl04_gvDecisions']
    if len(tables)!=1: raise ValueError('Illinois opinion table not recognized')
    rows=[]
    for row in tables[0].find('tr'):
        spans={n.attrs.get('id','').split('_')[-2]:n.text() for n in row.find('span') if '_lbl' in n.attrs.get('id','')}
        links=[a for a in row.find('a') if re.fullmatch(r'/resources/[a-f0-9-]+/file',a.attrs.get('href',''),re.I)]
        if not spans and not links: continue
        if not links and re.search(r'\bWithdrawn \d{2}/\d{2}/\d{2}',spans.get('lblNotes','')): continue
        if len(links)!=1 or spans.get('lblCourt')!='First District Appellate Court':
            raise ValueError('Illinois row missing document or outside First District')
        date=_date(spans.get('lblFilingDate'))
        kind=spans.get('lblType')
        if not date or kind not in {'Opinion','Rule 23'} or not spans.get('lblCitationNum'):
            raise ValueError('Illinois date, citation or publication type missing')
        title=spans['lblCitationNum']+': '+links[0].text()
        if kind=='Rule 23': title+=' [Rule 23], Nonprecedential'
        rows.append(dict(title=title,url=urljoin(base,links[0].attrs['href']),date=date))
    if not rows: raise ValueError('Illinois table has no auditable opinions')
    if [r['date'] for r in rows]!=sorted((r['date'] for r in rows),reverse=True):
        raise ValueError('Illinois filing-date order changed')
    pages=[int(m.group(1)) for a in root.find('a') if (m:=re.search(r"Page\$(\d+)",a.attrs.get('href',''))) ]
    fields={n.attrs['name']:n.attrs.get('value','') for n in root.find('input') if n.attrs.get('name') and n.attrs.get('type','').lower()=='hidden'}
    for n in root.find('select'):
        options=list(n.find('option')); selected=[o for o in options if 'selected' in o.attrs]
        if n.attrs.get('name') and options: fields[n.attrs['name']]=(selected or options)[0].attrs.get('value','')
    fields.update({'ctl00$ctl04$hdnSortField':'FilingDate','ctl00$ctl04$hdnSortDirection':'DESC'})
    return rows,pages,fields


def pennsylvania_page(body, base, page):
    data=json.loads(body)
    if not isinstance(data.get('Items'),list) or data.get('PageNumber')!=page or type(data.get('TotalPages')) is not int:
        raise ValueError('Pennsylvania opinion API schema changed')
    rows=[]
    for item in data['Items']:
        if item.get('CourtType')!=2: raise ValueError('Pennsylvania API returned another court')
        date=_date(item.get('DispositionDate'))
        if not date or not item.get('Caption') or not isinstance(item.get('Postings'),list):
            raise ValueError('Pennsylvania opinion metadata missing')
        for post in item['Postings']:
            kind=int(post['PostingTypeId'])
            if kind>=20 and kind!=32: continue  # Aggregate lists and administrative tables, not individual opinions.
            filename=post.get('FileName','')
            if not filename.lower().endswith('.pdf') or '/' in filename or '\\' in filename:
                raise ValueError('Pennsylvania unsafe or missing opinion filename')
            status=post.get('PublicationType',{}).get('Description')
            if status not in {'Precedential','Non-Precedential'}: raise ValueError('Pennsylvania publication status missing')
            title=' '.join(item['Caption'].split())+' ['+post['PostType']['PostingTypeCode']+']'
            if status=='Non-Precedential': title+=', Nonprecedential'
            url=urljoin(base,'/assets/opinions/Superior/out/'+quote(filename,safe=''))
            # Preserve the official file-version URL so revised documents remain discoverable.
            url+='?'+urlencode({'cb':post.get('FileVersion',1)})
            rows.append(dict(title=title,url=url,date=date))
    return rows,data


def fetch_step2(source, fetcher, now):
    cfg=json.loads(source['collector_config']);base=source['poll_url']
    cutoff=(now.date()-timedelta(days=30)).isoformat();end=now.date().isoformat();results=[]
    def get(url, data=None):
        kwargs=dict(source_config=cfg,timeout=30,max_bytes=8_000_000)
        if data is not None: kwargs['request_data']=urlencode(data).encode()
        body,typ,resolved=fetcher(url,**kwargs)
        if urlsplit(resolved).hostname not in cfg['official_hosts']:raise ValueError('Listing redirected outside official hosts')
        return body.decode('utf8',errors='replace'),resolved
    if cfg['listing_adapter']=='illinois_first':
        page=1;data=None;previous=None;seen=set()
        for _ in range(cfg.get('max_pages',40)):
            body,resolved=get(base,data);rows,pages,fields=illinois_page(body,resolved)
            urls={r['url'] for r in rows}
            if seen.intersection(urls): raise ValueError('Illinois pagination repeated records')
            seen.update(urls)
            if previous and rows[0]['date']>previous: raise ValueError('Illinois pagination order changed')
            results.extend(r for r in rows if cutoff<=r['date'][:10]<=end)
            previous=rows[-1]['date']
            if rows[-1]['date'][:10]<cutoff or not any(p>page for p in pages): break
            page+=1
            fields.update(__EVENTTARGET='ctl00$ctl04$gvDecisions',__EVENTARGUMENT=f'Page${page}')
            data=fields
        else: raise ValueError('Illinois page limit reached; source incomplete')
    else:
        total=None;seen=set()
        for page in range(1,cfg.get('max_pages',80)+1):
            url=base+'?'+urlencode(dict(startDate=cutoff,endDate=end,courtType='SUPERIOR',pageNumber=page))
            body,resolved=get(url);rows,payload=pennsylvania_page(body,resolved,page)
            if total is None: total=payload.get('TotalItems')
            if payload.get('TotalItems')!=total: raise ValueError('Pennsylvania results changed during pagination; retry')
            ids={r['Id'] for r in payload['Items']}
            if seen.intersection(ids): raise ValueError('Pennsylvania pagination repeated records')
            seen.update(ids);results.extend(rows)
            if page>=payload['TotalPages']:
                if len(seen)!=total: raise ValueError('Pennsylvania pagination incomplete')
                break
        else: raise ValueError('Pennsylvania page limit reached; source incomplete')
        results=[r for r in results if cutoff<=r['date'][:10]<=end]
    return json.dumps(list({r['url']:r for r in results}.values())).encode(),'application/json',base
