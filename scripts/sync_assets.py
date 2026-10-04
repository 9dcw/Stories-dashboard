#!/usr/bin/env python3
"""Convert the published Dashboard Assets Google Sheet tab to dashboard JSON."""
import csv, io, json, os, sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

SHEET_ID = os.environ.get('DASHBOARD_SHEET_ID', '11EsVxXvsVnr5y73pml5UPcVdAhkT1VZBAxsIV_kjbow')
SHEET_GID = os.environ.get('DASHBOARD_SHEET_GID', '1429231739')
CSV_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&gid={SHEET_GID}'
OUT = os.path.join(os.path.dirname(__file__), '..', 'data', 'assets.json')

req = Request(CSV_URL, headers={'User-Agent': 'stories-dashboard-sync/1.0'})
with urlopen(req, timeout=30) as response:
    raw = response.read().decode('utf-8-sig')

rows = list(csv.DictReader(io.StringIO(raw)))
required = {'id', 'title', 'category', 'type', 'description', 'url', 'tags', 'favorite', 'enabled'}
if not rows or not required.issubset(rows[0].keys()):
    raise SystemExit('Dashboard Assets CSV is unavailable or has the wrong headers. Publish the correct tab and preserve its header row.')

assets = []
for row in rows:
    if row.get('enabled', '').strip().upper() not in {'TRUE', 'YES', '1', 'Y'}:
        continue
    if not row.get('id', '').strip() or not row.get('title', '').strip() or not row.get('url', '').strip():
        continue
    assets.append({
        'id': row['id'].strip(),
        'title': row['title'].strip(),
        'category': row['category'].strip() or 'Uncategorized',
        'type': row['type'].strip() or 'Link',
        'description': row['description'].strip(),
        'url': row['url'].strip(),
        'tags': [x.strip() for x in row['tags'].split(',') if x.strip()],
        'favorite': row['favorite'].strip().upper() in {'TRUE', 'YES', '1', 'Y'},
    })

if not assets:
    raise SystemExit('No enabled dashboard assets found; refusing to overwrite the dashboard with an empty catalog.')

payload = {
    'updated': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    'source': 'Google Sheets — Dashboard Assets',
    'assets': assets,
}
with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(payload, f, indent=2, ensure_ascii=False)
    f.write('\n')
print(f'Wrote {len(assets)} enabled assets to {OUT}')
