"""Evidence-backed court links from appellate and supreme-court captions.

This is deliberately a partial extractor, not a search of cited authorities.
Unrecognized captions remain unknown. Court IDs are internal, not feed IDs.
"""
from __future__ import annotations

import json
import re

VERSION = 'origin-caption-v1'
STATES = ('Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
          'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|'
          'Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|'
          'Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|'
          'North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|'
          'South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|'
          'West Virginia|Wisconsin|Wyoming|Puerto Rico|Guam|Virgin Islands|Northern Mariana Islands')
COUNTY = r"[A-Za-z][A-Za-z '-]{0,45}?"
ORDINAL = r'(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|Twelfth|Thirteenth|Fourteenth|Fifteenth|Sixteenth|Seventeenth|Eighteenth|Nineteenth|Twentieth|Twenty-First|Twenty-Second)'


def extract_courts(pages, jurisdiction=''):
    found = {}

    def add(name, level, quote, page, roles, region):
        name = ' '.join(name.split()).title()
        key = region + ':' + re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')
        record = found.setdefault(key, dict(court_id=key, name=name, jurisdiction=region,
            level=level, relationships=[], evidence=[]))
        record['relationships'] = sorted(set(record['relationships']) | set(roles))
        evidence = dict(page=page, quote=quote.strip())
        if evidence not in record['evidence']:record['evidence'].append(evidence)

    # Origin statements normally occur on the cover or following caption page.
    # Anchoring to a caption line prevents matching a citation in narrative text.
    for page, raw in enumerate(pages[:3], 1):
        text = raw[:7000]
        for m in re.finditer(r'(?im)^\s*(?:(?:On\s+)?Appeals? from|On (?:a )?writ of certiorari to|On petition for (?:a writ of certiorari|review) (?:to|from))\b', text):
            block = re.split(r'\n[ \t]*\n', text[m.start():m.start()+550].strip(), maxsplit=1)[0]
            flat = ' '.join(block.split())
            flat = re.split(r'(?i)\b(?:Before|Judge|Judges|Presiding|Counsel|We hold|We affirm)\b', flat)[0]
            federal = re.search(r'United States (District|Bankruptcy) Court\s+(?:for|of)\s+the\s+((?:(?:Northern|Southern|Eastern|Western|Middle|Central)\s+)?District of\s+(?:'+STATES+r')|District of Columbia)\b', flat, re.I)
            if federal:
                name = f'United States {federal[1]} Court for the {federal[2]}'
                # Quote the original line wrapping, not reconstructed text.
                end = re.search(re.escape(federal[0]).replace(r'\ ', r'\s+'), block, re.I)
                add(name, 'trial', block[:end.end()] if end else block, page,
                    ['reviewed_court', 'trial_court'], 'US')
            circuit = re.search(r'United States Court of Appeals for the ('+ORDINAL+r'|District of Columbia|Federal) Circuit', flat, re.I)
            if circuit:
                add(circuit[0], 'appellate', block, page, ['reviewed_court'], 'US')
            if jurisdiction == 'US-FL':
                fl = re.search(r'(Circuit|County) Court\s+(?:for|of|in and for|,)\s*('+COUNTY+r') County\b', flat, re.I)
                if fl:add(f'{fl[2]} County {fl[1]} Court', 'trial', block, page, ['reviewed_court','trial_court'], jurisdiction)
            if jurisdiction == 'US-CA':
                ca = re.search(r'Superior Court of\s+('+COUNTY+r') County\b', flat, re.I)
                if ca:add(f'{ca[1]} County Superior Court', 'trial', block, page, ['reviewed_court','trial_court'], jurisdiction)
            if jurisdiction == 'US-TX':
                tx = re.search(r'(\d+(?:st|nd|rd|th)) (District Court|Judicial District Court)\s+(?:of\s+)?('+COUNTY+r') County', flat, re.I)
                if tx:add(f'{tx[3]} County {tx[1]} District Court', 'trial', block, page, ['reviewed_court','trial_court'], jurisdiction)
                probate = re.search(r'(Probate Court No\.\s*\d+|County Court at Law No\.\s*\d+)\s+(?:of\s+)?('+COUNTY+r') County', flat, re.I)
                if probate:add(f'{probate[2]} County {probate[1]}', 'trial', block, page, ['reviewed_court','trial_court'], jurisdiction)
                txa = re.search(r'Court of Appeals for the ('+ORDINAL+r') District of Texas', flat, re.I)
                if txa:add(f'Texas {txa[1]} District Court of Appeals', 'appellate', block, page, ['reviewed_court'], jurisdiction)
        if jurisdiction == 'US-CA' and page == 1:
            # Supreme Court captions can list both the intermediate appeal and
            # the original superior court without an "Appeal from" sentence.
            caption = re.split(r'(?im)^\s*(?:Justice\b|Chief Justice\b|OPINION\b|APPEAL from\b)', text)[0]
            for m in re.finditer(r'(?im)^\s*('+COUNTY+r') County Superior Court\s*$', caption):
                add(f'{m[1]} County Superior Court', 'trial', m[0], page, ['trial_court'], jurisdiction)
            supreme_caption = re.search(r'IN THE SUPREME COURT OF CALIFORNIA', ' '.join(caption[:500].upper().split()))
            for m in (re.finditer(r'(?im)^\s*('+ORDINAL+r') Appellate District(?:,\s*Division (One|Two|Three|Four|Five|Six|Seven|Eight))?\s*$', caption) if supreme_caption else []):
                name = f'California {m[1]} Appellate District' + (f', Division {m[2]}' if m[2] else '')
                add(name, 'appellate', m[0], page, ['reviewed_court'], jurisdiction)
    if jurisdiction == 'US-FL':
        # Florida Supreme Court places its review-origin block after the
        # opinion/dissents, sometimes many pages after the cover.
        for page, text in enumerate(pages, 1):
            for m in re.finditer(r'(?im)^Application for Review of the Decision of the District Court of Appeal\b', text):
                block = text[m.start():m.start()+450]
                district = re.search(r'('+ORDINAL+r') District\s*[-–—]\s*Case No\.', block, re.I)
                if district:
                    add(f'Florida {district[1]} District Court of Appeal', 'appellate', block,
                        page, ['reviewed_court'], jurisdiction)
    return dict(version=VERSION, status='identified' if found else 'unknown', courts=list(found.values()))


def save_courts(connection, item_id, pages, jurisdiction):
    record = extract_courts(pages, jurisdiction)
    connection.execute('INSERT OR REPLACE INTO court_origin_identity(item_id,identity_json) VALUES(?,?)',
                       (item_id, json.dumps(record)))
    return record


def load_courts(store):
    """Read-only fallback exposes cached history before a persisted backfill."""
    with store._connect() as c:
        records = {r['item_id']: json.loads(r['identity_json']) for r in c.execute('SELECT * FROM court_origin_identity')}
        for r in c.execute('''SELECT d.item_id,d.document_json,s.jurisdiction FROM court_documents d
                JOIN items i USING(item_id) JOIN sources s USING(source_id)
                LEFT JOIN court_origin_identity o USING(item_id)
                WHERE o.item_id IS NULL OR json_extract(o.identity_json, '$.version') != ?''', (VERSION,)):
            if records.get(r['item_id'], {}).get('version') != VERSION:
                records[r['item_id']] = extract_courts(json.loads(r['document_json'])['pages'], r['jurisdiction'])
        return records


def activity_report(store, records=None):
    """Rank observed documents, not total filings or inferred trial-case counts."""
    records = load_courts(store) if records is None else records
    groups = {}
    with store._connect() as c:
        rows = list(c.execute('''SELECT i.item_id,i.raw_url,i.published_at,i.first_seen_at,
            CASE WHEN a.item_id IS NULL THEN d.document_json END AS document_json,a.identity_json
            FROM items i JOIN court_documents d USING(item_id)
            LEFT JOIN court_appeal_identity a USING(item_id)'''))
    from appeal_identity import extract_identity
    identified = 0
    for row in rows:
        record = records.get(row['item_id'], {})
        identified += bool(record.get('courts'))
        appeal = (json.loads(row['identity_json']) if row['identity_json'] is not None else
                  extract_identity(row['raw_url'], json.loads(row['document_json'])['pages']))
        # Known appellate docket keys deduplicate companion documents; unknown
        # appellate dockets contribute only to document counts.
        for court in record.get('courts', []):
            g = groups.setdefault(court['court_id'], {k:court[k] for k in ['court_id','name','jurisdiction','level']} |
                dict(document_count=0, relationships=set(), appellate_dockets=set(), documents_without_appellate_identity=0,
                     first_document_date=None, last_document_date=None))
            g['document_count'] += 1
            g['relationships'].update(court['relationships'])
            if appeal:g['appellate_dockets'].update((appeal['court'], d) for d in appeal['dockets'])
            else:g['documents_without_appellate_identity'] += 1
            date = row['published_at'] or row['first_seen_at']
            g['first_document_date'] = min(g['first_document_date'] or date, date)
            g['last_document_date'] = max(g['last_document_date'] or date, date)
    result = []
    for g in groups.values():
        g['relationships'] = sorted(g['relationships'])
        g['identified_appellate_docket_count'] = len(g.pop('appellate_dockets'))
        result.append(g)
    return dict(version=VERSION, metric='Observed appellate/supreme-court documents; not total court filings or unique trial cases',
        cached_documents=len(rows), identified_documents=identified, unknown_documents=len(rows)-identified,
        courts=sorted(result, key=lambda g:(-g['document_count'],g['court_id'])))
