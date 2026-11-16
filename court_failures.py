"""Close already-attempted uncertain outcomes without deleting editorial history."""
import json
from court_pipeline import terminal_failure
from story_store import utc_now


def finalize_existing(store, *, apply=False):
    with store._connect() as c:
        rows=[dict(r) for r in c.execute("""SELECT cs.*,COALESCE(n.summary_status,'PENDING') summary_status
            FROM court_screenings cs JOIN items i USING(item_id) JOIN sources s USING(source_id)
            LEFT JOIN candidate_notes n USING(item_id)
            WHERE s.active=1 AND s.lane='B' AND i.status='NEW'""")]
    failed=[r for r in rows if (r['decision']=='ERROR' or r['needs_review']) and not json.loads(r['result_json']).get('terminal_failure')]
    skipped=[r for r in rows if r['decision']=='EXCLUDED' and not r['needs_review'] and r['summary_status']=='PENDING']
    if apply:
        with store._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS court_failure_audit (
                item_id INTEGER NOT NULL, policy TEXT NOT NULL, previous_screening_json TEXT NOT NULL,
                previous_summary_status TEXT NOT NULL, finalized_at TEXT NOT NULL,
                PRIMARY KEY(item_id,policy))''')
            for r in failed:
                c.execute('INSERT OR IGNORE INTO court_failure_audit VALUES(?,?,?,?,?)',
                    (r['item_id'],'terminal_failures_v1',json.dumps(r),r['summary_status'],utc_now()))
                result=terminal_failure(json.loads(r['result_json']))
                c.execute("UPDATE court_screenings SET decision='ERROR',result_json=? WHERE item_id=?",(json.dumps(result),r['item_id']))
                reason=str(result.get('error') or result.get('reason') or r['reason_code'] or 'Screening was unresolved')[:2000]
                c.execute("""INSERT INTO candidate_notes(item_id,summary_status,summary_error) VALUES(?,'FAILED',?)
                    ON CONFLICT(item_id) DO UPDATE SET summary_status='FAILED',summary_error=excluded.summary_error""",(r['item_id'],reason))
            for r in skipped:
                c.execute("""INSERT INTO candidate_notes(item_id,summary_status,summary_error) VALUES(?,'SKIPPED',?)
                    ON CONFLICT(item_id) DO UPDATE SET summary_status='SKIPPED',summary_error=excluded.summary_error""",
                    (r['item_id'],'Summary not required: '+r['reason_code']))
    return {'failed_existing_unresolved':len(failed),'closed_excluded_summaries':len(skipped),'applied':apply}
