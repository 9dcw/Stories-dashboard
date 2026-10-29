"""Court coverage is independent of the number of configured feeds."""
import json
from collections import Counter
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / 'data' / 'court_inventory.json'


def coverage_report(sources, manifest_path=MANIFEST):
    manifest = json.loads(Path(manifest_path).read_text())
    courts = []
    for entry in manifest['courts']:
        linked = []
        for source in sources:
            config = json.loads(source.get('collector_config') or '{}')
            if entry['court_id'] in config.get('court_ids', []) or entry.get('legacy_source_name') == source['name']:
                linked.append(source)
        active = [s for s in linked if s['active']]
        status = 'active' if active else ('disabled' if linked else entry.get('discovery_status', 'outstanding'))
        courts.append({**entry, 'status': status, 'source_ids': [s['source_id'] for s in linked],
                       'active_feeds': len(active),
                       'last_success_at': max((s.get('last_success_at') or '' for s in active), default=''),
                       'errors': [s['last_error'] for s in active if s.get('last_error')]})
    active = sum(c['status'] == 'active' for c in courts)
    expansion = [c for c in courts if c['level'] != 'federal_appeal']
    return {'counting_note': manifest['counting_note'],
            'total': len(courts), 'active': active, 'outstanding': len(courts)-active,
            'expansion_total': len(expansion), 'expansion_active': sum(c['status'] == 'active' for c in expansion),
            'by_status': dict(Counter(c['status'] for c in courts)),
            'structure_audits_outstanding': manifest['jurisdictions_pending_structure_audit'], 'courts': courts}
