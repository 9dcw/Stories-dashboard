#!/usr/bin/env python3
"""Install audited court endpoints without enabling sources or other lanes."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from story_store import StoryStore
# Exact document paths are structural link validation, not editorial inclusion.
SOURCES={
 'Ninth Circuit opinions':('rss','https://www.ca9.uscourts.gov/opinions/index.xml',r'/datastore/opinions/.+\.pdf',{}),
 'First Circuit opinions':('html_list','https://www.ca1.uscourts.gov/opinions',r'/opnfiles/[^/]+\.pdf',{}),
 'Second Circuit opinions':('html_list','https://ww3.ca2.uscourts.gov/dtSearch/dtisapi6.dll',r'/decisions/OPN/[^/]+\.pdf',{'ca2_date_search':True}),
 'Third Circuit precedential opinions':('rss','https://www2.ca3.uscourts.gov/recentop/week/recopinions.xml',r'/opinarch/[^/]+\.pdf',{}),
 'Fourth Circuit opinions':('html_list','https://www.ca4.uscourts.gov/opinions/recent-opinions',r'/opinions/\d[^/]*\.pdf',{}),
 'Fifth Circuit opinions':('rss','https://www.ca5.uscourts.gov/rss.aspx?Feed=Opinions&Style=Detail&Which=All',r'/opinions/(?:pub|unpub)/.+\.pdf',{}),
 'Sixth Circuit opinions':('html_list','https://www.opn.ca6.uscourts.gov/opinions/',r'/opinions\.pdf/[^/]+\.pdf',{}),
 'Seventh Circuit opinions':('html_list','https://media.ca7.uscourts.gov/cgi-bin/OpinionsWeb/processWebInputExternal.pl?Time=month&Author=any&Case=any&OpsOnly=yes&Submit=Submit',r'/cgi-bin/OpinionsWeb/.*[?&]Submit=Display',{}),
 'Eighth Circuit opinions':('rss','https://ecf.ca8.uscourts.gov/rss/ca8opns_rss.xml',r'/opndir/[^/]+/[^/]+/[^/]+\.pdf',{}),
 'Tenth Circuit opinions':('rss','https://www.ca10.uscourts.gov/opinions/new/daily_decisions.rss',r'/(?:sites/ca10/files|opinions)/.+\.pdf',{}),
 'Eleventh Circuit published opinions':('rss','https://media.ca11.uscourts.gov/opinions/rss/pubopnsfeed.php',r'/opinions/pub/files/[^/]+\.pdf',{}),
 'D.C. Circuit opinions':('rss','https://media.cadc.uscourts.gov/opinions/rss.xml',r'/opinions/[^?]+\.pdf',{}),
 'Federal Circuit opinions and orders':('rss','https://www.cafc.uscourts.gov/category/opinion-order/feed',r'uscourts\.gov/[^/]+-(?:opinion|order)-[^/]+/?$',{}),
}
def configure(store):
    changes=[]
    with store._connect() as c:
        for source in store.list_sources():
            if source['name'] not in SOURCES:continue
            typ,url,pattern,extra=SOURCES[source['name']]
            cfg=json.loads(source['collector_config'] or '{}')
            cfg.update(allowed_path_prefixes=[],opinion_url_pattern=pattern,fetch={'max_attempts':2,'min_interval':.3},**extra)
            c.execute('UPDATE sources SET collector_type=?,poll_url=?,collector_config=? WHERE source_id=?',(typ,url,json.dumps(cfg),source['source_id']))
            changes.append({'source_id':source['source_id'],'name':source['name'],'url':url})
    return changes
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--db',required=True,type=Path);a=p.parse_args()
    print(json.dumps(configure(StoryStore(a.db)),indent=2))
