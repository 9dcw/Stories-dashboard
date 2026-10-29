#!/usr/bin/env python3
"""Audit each official source independently, optionally register/activate validated sources."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from court_pilot import source_record,install
from court_pipeline import court_candidates,extract_document
from source_fetch import fetch_resource
from state_courts import fetch_listing
from state_court_sources import sources
from story_store import StoryStore,utc_now


def definition_hash(spec):
    return hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()


def audit_source(spec,sample_count=1):
    source=source_record(spec)
    result=dict(definition_hash=definition_hash(spec),name=spec['name'],court_id=spec['court_id'],url=spec['url'],scope=spec['scope'],checked_at=utc_now())
    try:
        body,_,_=fetch_listing(source,fetch_resource)
        candidates=court_candidates(source,body.decode('utf8'))
        result['documents_found']=len(candidates)
        if not candidates:raise ValueError('No documents available for full-text validation; source remains pending')
        samples=[];failures=[]
        for candidate in candidates[:min(len(candidates),max(sample_count,3))]:
            try:
                doc=extract_document(candidate.url,source['collector_config'])
                samples.append(dict(url=candidate.url,headline=candidate.headline,pages=len(doc['pages']),content_hash=doc['content_hash']))
                if len(samples)>=sample_count:break
            except Exception as exc:failures.append(dict(url=candidate.url,error=str(exc)))
        if not samples:raise ValueError('Full-text samples failed: '+json.dumps(failures))
        result.update(status='validated',samples=samples,sample_failures=failures)
    except Exception as exc:result.update(status='blocked',error=str(exc))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path);p.add_argument('--activate',action='store_true')
    p.add_argument('--state',choices=['FL','TX','CA'],action='append')
    p.add_argument('--court',action='append')
    p.add_argument('--audit-output',required=True,type=Path)
    p.add_argument('--reuse-audit',type=Path,help='Reuse a same-day audit; still verify source definition matches')
    a=p.parse_args()
    if a.activate and not a.db:p.error('--activate requires --db')
    specs=[s for s in sources() if (not a.state or s['jurisdiction'][3:] in a.state) and (not a.court or s['court_id'] in a.court)]
    if not specs:p.error('No matching official sources')
    prior=json.loads(a.reuse_audit.read_text()) if a.reuse_audit else []
    audits=[];installed=[]
    for spec in specs:
        previous=next((r for r in prior if r['name']==spec['name'] and r['url']==spec['url'] and r.get('definition_hash')==definition_hash(spec) and r['checked_at'][:10]==utc_now()[:10]),None)
        result=previous or audit_source(spec)
        audits.append(result)
        a.audit_output.write_text(json.dumps(audits,indent=2)+'\n')
        print(json.dumps(result),flush=True)
        if a.db and result['status']=='validated':installed.extend(install(StoryStore(a.db),[spec],activate=a.activate))
    print(json.dumps(dict(validated=sum(r['status']=='validated' for r in audits),total=len(audits),installed=installed)))
    if any(r['status']!='validated' for r in audits):sys.exit(1)
if __name__=='__main__':main()
