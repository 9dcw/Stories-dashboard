"""Shared editorial tables and budgets; full evidence remains in artifact_versions."""
from dataclasses import dataclass
import re

VERSION = "tables.v1"
SPECS = {
    'hub': (500, 60, 5, ['Story', 'Current assessment', 'Progress', 'Blockers', 'Next decision']),
    'angles': (1500, 120, 7, ['Central finding', 'People and stakes', 'What happened', 'Mechanism', 'Outcome', 'Contradictions', 'Research gaps']),
    'plan': (1500, 120, 7, ['Central question', 'Primary records', 'Fact validation', 'Visual research', 'Context', 'Contradictions', 'Next actions']),
    'evidence': (1500, 120, 7, ['Central finding', 'People and stakes', 'What happened', 'Mechanism', 'Outcome', 'Contradictions', 'Research gaps']),
    'treatment': (1500, 150, 7, ['Opening', 'Setup', 'Escalation', 'Turn', 'Explanation', 'Payoff', 'Closing']),
    'visuals': (800, 80, 8, []),
    'review': (700, 80, 6, []),
}


def words(text):
    return len(re.findall(r'https?://\S+|[^\s]+', text))


def instructions(key):
    cap, detail, rows, topics = SPECS[key]
    return (f"Google Docs content policy {VERSION}. Publish only editorial_rows: "
            "topic, short (one sentence, <=25 words), detail (adds evidence/action, not repetition). "
            f"Topic <=8 words; detail <={detail} words; aim for <={rows} rows; whole rendered Doc <={cap} words including navigation. "
            + ("Use these topics in this exact order: " + '; '.join(topics) + '. ' if topics else '') +
            "Keep full structured sources/claims/scenes/rights separately; they are retained in version storage. "
            "The table must retain all material qualifications, contradictions, evidence labels, source URLs and locators, rights restrictions and unresolved blockers. "
            "Do not publish logs, raw JSON, search diaries, proof-of-work, superseded drafts, long excerpts or duplicate summaries. "
            "Do not silently truncate or hide adverse evidence. policy_exception is normally empty; give a specific editorial reason for necessary excess. "
            "Every critical/major review finding issue must appear verbatim in the table, even if the row target is exceeded; write concise issue text. "
            "For reviews, use current_docs (actual Google readbacks). Report each artifact/revision, current policy version, total/managed words, structure/cell/content findings and outcome. "
            "An exception requires human review. Never approve publication. Missing live readback means review incomplete.")


@dataclass
class TableDocument:
    prefix: str
    rows: list
    key: str
    exception: str = ''

    @property
    def grid(self):
        return [['Topic', 'At a glance', 'Detail / evidence']] + [[r['topic'], r['short'], r['detail']] for r in self.rows]

    @property
    def text(self):
        return self.prefix + '\n' + ''.join(cell+'\n' for row in self.grid for cell in row)


def validate_rows(key, output):
    if 'editorial_rows' not in output:
        raise ValueError('Editorial table missing; regenerate this legacy output under '+VERSION)
    rows=output['editorial_rows']
    if not rows: raise ValueError('Editorial table needs at least one row')
    cap, detail, maximum, topics=SPECS[key]
    if topics and [r['topic'] for r in rows] != topics:
        raise ValueError('Editorial table must use the registered topics in order')
    excess=[]
    if len(rows)>maximum: excess.append('row target')
    for r in rows:
        if words(r['topic'])>8 or words(r['short'])>25 or words(r['detail'])>detail:
            excess.append('cell budget')
        if re.search(r'```|<table|\t|(?:^|\n)\s*(?:Tool output|Execution trace|Search diary|Proof of work):',r['detail'],re.I):
            raise ValueError('Raw output, nested tables and execution logs are not reader-facing content')
        if r['short'].strip()==r['detail'].strip():
            raise ValueError('Detail must add evidence or action, not repeat the short version')
    if key=='review':
        published=' '.join(r['short']+' '+r['detail'] for r in rows)
        for finding in output.get('findings',[]):
            if finding['severity'] in {'critical','major'} and finding['issue'] not in published:
                raise ValueError('Every critical/major issue must appear verbatim in the editorial table')
    if excess and not output.get('policy_exception','').strip():
        raise ValueError('Editorial table exceeds '+', '.join(sorted(set(excess)))+'; synthesize or explain a necessary exception')


def render_document(prefix, key, output):
    validate_rows(key,output)
    exception=output.get('policy_exception','').strip()
    prefix += f'\nContent policy: {VERSION}\n'
    if exception:
        prefix += 'Exception requires human review: '+exception+'\n'
    result=TableDocument(prefix,output['editorial_rows'],key,exception)
    if words(result.text)>SPECS[key][0] and not exception:
        raise ValueError('Rendered document exceeds soft cap; synthesize or provide a specific policy_exception')
    return result


def audit(key, document, managed_text='', table_rows=None):
    cap,detail,maximum,topics=SPECS[key]
    issues=[]
    if 'Exception requires human review:' in managed_text: issues.append('Editorial exception requires human review')
    if re.search(r'```|(?:^|\n)\s*(?:Tool output|Execution trace|Search diary|Proof of work):',managed_text,re.I): issues.append('Excluded operational content')
    if words(document)>cap: issues.append('Whole document exceeds soft cap')
    if not table_rows or table_rows[0]!=['Topic','At a glance','Detail / evidence']:
        issues.append('Required three-column table missing')
    else:
        rows=table_rows[1:]
        if len(rows)>maximum: issues.append('Row target exceeded')
        if topics and [r[0] for r in rows]!=topics: issues.append('Required topics missing or out of order')
        for row in rows:
            if len(row)!=3 or any(words(v)>limit for v,limit in zip(row,(8,25,detail))):
                issues.append('Cell budget exceeded')
            if len(row)==3 and row[1].strip()==row[2].strip(): issues.append('Repeated short/detail content')
    return dict(policy_version=VERSION,total_words=words(document),managed_words=words(managed_text),cap=cap,issues=sorted(set(issues)),outcome='revise' if issues else 'pass')
