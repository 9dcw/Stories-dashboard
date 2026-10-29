#!/usr/bin/env python3
"""Report current coverage and outstanding courts without modifying the database."""
import argparse,json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from court_inventory import coverage_report
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--db',required=True,type=Path)
p.add_argument('--outstanding',action='store_true')
a=p.parse_args()
with sqlite3.connect(a.db.resolve().as_uri()+'?mode=ro',uri=True) as c:
    c.row_factory=sqlite3.Row
    report=coverage_report([dict(r) for r in c.execute('SELECT * FROM sources')])
if a.outstanding:report['courts']=[r for r in report['courts'] if r['status']!='active']
print(json.dumps(report,indent=2))
