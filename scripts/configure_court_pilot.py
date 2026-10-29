#!/usr/bin/env python3
"""Validate the pilot listings and full-text samples before optional activation."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from court_pilot import pilot_sources,source_record,install
from court_pipeline import court_candidates,extract_document
from source_fetch import fetch_resource
from story_store import StoryStore
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--db',type=Path)
p.add_argument('--activate',action='store_true')
p.add_argument('--audit-output',required=True,type=Path)
a=p.parse_args()
if a.activate and not a.db:p.error('--activate requires --db')
audit=[];valid=[]
for spec in pilot_sources():
    record=source_record(spec)
    result=dict(name=spec['name'],url=spec['url'],scope=spec['scope'])
    try:
        body,_,resolved=fetch_resource(spec['url'],source_config=record['collector_config'])
        if resolved.split('/')[2] not in spec['hosts']:raise ValueError('Listing redirected outside allowed hosts')
        candidates=court_candidates(record,body.decode('utf8',errors='replace'))
        if not candidates:raise ValueError('No opinion links: defer activation until a full-text sample is available')
        samples=[]
        for candidate in candidates[:3]:
            doc=extract_document(candidate.url,record['collector_config'])
            samples.append(dict(url=candidate.url,pages=len(doc['pages']),content_hash=doc['content_hash']))
        result.update(status='validated',documents_found=len(candidates),samples=samples)
        valid.append(spec)
    except Exception as exc:result.update(status='blocked',error=str(exc))
    audit.append(result)
a.audit_output.write_text(json.dumps(audit,indent=2)+'\n')
print(json.dumps(dict(audit=audit,installed=install(StoryStore(a.db),valid,activate=a.activate) if a.db else []),indent=2))
if len(valid)!=len(audit):sys.exit(1)
