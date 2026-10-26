"""Caption-based appellate identity; documents and editorial history stay intact."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

VERSION = 'caption-v1'
NUMBER = r'(?:\d{4}|\d{2})-\d{3,5}'


def extract_identity(url, pages):
    host = urlsplit(url).hostname or ''
    match = re.search(r'(?:^|\.)(ca(?:[1-9]|1[0-1]|dc|fc))\.uscourts\.gov$', host)
    if not match:
        return None
    court = match[1]
    evidence, numbers = [], set()
    # Captions can span several pages. Stop before procedural history/citations.
    for page, text in enumerate(pages[:12], 1):
        stop = re.search(r'(?im)^\s*(?:Appeals? from|APPEALS? FROM|Petition for review|Before\b|SUMMARY\b|COUNSEL\b)', text)
        caption = text[:stop.start()] if stop else text
        for line in caption.splitlines():
            marker = re.search(r'\bNos?\.\s*(' + NUMBER + r')', line)
            if marker and not re.search(r'(?:D\.?C\.?|Docket|District|Board)\s*$', line[:marker.start()], re.I):
                tail = line[marker.start():]
                found = re.findall(NUMBER, tail)
                numbers.update(found)
                evidence.append({'page': page, 'quote': line.strip()})
            # Federal Circuit uses a standalone four-digit-year docket.
            if court == 'cafc' and re.fullmatch(r'\s*' + NUMBER + r'\s*', line):
                numbers.add(line.strip())
                evidence.append({'page': page, 'quote': line.strip()})
        # Wrapped list continuation: only a bare docket line, before caption ends.
        if numbers or re.search(r'\bNos?\.', caption):
            for line in caption.splitlines():
                continuation = re.search(r'\s{2,}(' + NUMBER + r')(?=\s|$)', line)
                if continuation and not re.search(r'\b(?:D\.?C\.?|Docket|District)\s*$', line[:continuation.start()], re.I):
                    numbers.add(continuation[1])
                    evidence.append({'page': page, 'quote': line.strip()})
        if stop:
            break
    if not numbers:
        return None
    def normalize(n):
        year, sequence = n.split('-')
        return year + '-' + str(int(sequence))
    return {'court': court, 'dockets': sorted({normalize(n) for n in numbers}),
            'evidence': evidence, 'version': VERSION}


def save_identity(connection, item_id, url, pages):
    identity = extract_identity(url, pages)
    connection.execute('INSERT OR REPLACE INTO court_appeal_identity(item_id,identity_json) VALUES(?,?)',
                       (item_id, json.dumps(identity)))
    return identity


def load_identities(store):
    """Read existing cached documents too, so rollout needs no destructive migration."""
    with store._connect() as c:
        result = {r['item_id']: json.loads(r['identity_json']) for r in c.execute(
            'SELECT * FROM court_appeal_identity')}
        # Cached-document fallback covers historical rows before the backfill runs.
        for r in c.execute('''SELECT d.item_id,i.raw_url,d.document_json FROM court_documents d
                JOIN items i USING(item_id) LEFT JOIN court_appeal_identity a USING(item_id)
                WHERE a.item_id IS NULL'''):
            result[r['item_id']] = extract_identity(r['raw_url'], json.loads(r['document_json'])['pages'])
        return result


def group_items(rows, identities, promoted_ids):
    """Use fixed representative docket sets, avoiding transitive overlap chains.

    Explicit shared dockets suffice even with 'et al.'. Unknown dockets remain
    separate. A new appellate docket creates a new group after remand.
    """
    groups = []
    for row in sorted(rows, key=lambda r: r['item_id']):
        identity = identities.get(row['item_id'])
        group = None
        if identity:
            group = next((g for g in groups if g['identity'] and
                g['identity']['court'] == identity['court'] and
                set(g['identity']['dockets']) & set(identity['dockets'])), None)
        if group is None:
            group = {'identity': identity, 'members': []}
            groups.append(group)
        group['members'].append(row)
    output = []
    for group in groups:
        members = group['members']
        if any(r['item_id'] in promoted_ids for r in members):
            continue
        # Keep editorial decisions sticky when another document for an appeal arrives.
        dismissed = [r for r in members if r['status'] == 'DISMISSED']
        available = dismissed or [r for r in members if r['status'] != 'IGNORED']
        if not available:
            continue
        # An earlier retained opinion must not disappear because a later
        # companion document was excluded by the screening rules.
        screened = [r for r in available if r.get('screening_decision') == 'PASS']
        latest = max(screened or available, key=lambda r: (r['published_at'] or r['first_seen_at'], r['item_id']))
        row = dict(latest)
        row['appeal_identity'] = group['identity']
        row['appeal_documents'] = [{'item_id': r['item_id'], 'url': r['raw_url'],
            'headline': r['headline'], 'date': r['published_at'] or r['first_seen_at']}
            for r in sorted(members, key=lambda r: (r['published_at'] or r['first_seen_at'], r['item_id']), reverse=True)]
        output.append(row)
    return output
