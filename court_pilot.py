"""Small audited official-source batch; all editorial rules remain in court_pipeline."""
import json
from datetime import datetime


def pilot_sources(now=None):
    now = now or datetime.now()
    term = now.year if now.month >= 10 else now.year - 1
    return [
        dict(name='U.S. Supreme Court opinions', court_id='scotus', jurisdiction='US',
             url=f'https://www.supremecourt.gov/opinions/slipopinion/{term % 100:02}',
             hosts=['www.supremecourt.gov'], pattern=r'/opinions/\d{2}pdf/[^/?]+\.pdf$',
             scope='Opinions of the Court, current October term; order-related and in-chambers opinions not included.'),
        dict(name='Maine Supreme Judicial Court published opinions', court_id='me', jurisdiction='US-ME',
             url='https://www.courts.maine.gov/courts/sjc/opinions.html',
             hosts=['www.courts.maine.gov', 'courts.maine.gov'],
             pattern=r'/courts/sjc/lawcourt/\d{4}/[^/?]+\.pdf$',
             scope='Published opinions exposed by the current listing; no archive crawl.'),
    ]


def source_record(spec):
    fetch = dict(max_attempts=2, min_interval=0.5)
    if spec['court_id'] == 'scotus': fetch['endpoint_adapter'] = 'scotus_current_term'
    return dict(source_id=0, name=spec['name'], collector_type=spec.get('collector_type','html_list'), poll_url=spec['url'],
                collector_config=json.dumps(dict(court_ids=spec.get('court_ids',[spec['court_id']]), official_hosts=spec['hosts'],
                    opinion_url_pattern=spec['pattern'], allowed_path_prefixes=[],
                    fetch=fetch, coverage_scope=spec['scope'], **spec.get('config',{}))))


def install(store, specs, *, activate=False):
    """Idempotent registration; activation is explicit and only accepts audited specs."""
    result=[]
    for spec in specs:
        record=source_record(spec)
        matches=[s for s in store.list_sources() if s['name']==spec['name']]
        if len(matches)>1: raise ValueError(f"Ambiguous source: {spec['name']}")
        if matches:
            sid=matches[0]['source_id']
            with store._connect() as c:
                c.execute('UPDATE sources SET collector_type=?,poll_url=?,collector_config=? WHERE source_id=?',
                          (record['collector_type'],spec['url'],record['collector_config'],sid))
        else:
            sid=store.add_source(spec['name'],'B',spec['jurisdiction'],spec['url'],active=False,
                                 collector_type=record['collector_type'],collector_config=record['collector_config'])
        if activate: store.set_source_active(sid,True)
        result.append(dict(source_id=sid,name=spec['name']))
    return result
