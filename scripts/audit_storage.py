"""Read-only local storage audit; never prints payloads, cookies or credentials."""
import json
import sqlite3
import statistics
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
db = ROOT / 'data/ctn.sqlite3'
conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=15)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA query_only=ON')
now = datetime.now(timezone.utc)
since = (now - timedelta(minutes=15)).isoformat().replace('+00:00', 'Z')
report = {'at': now.isoformat(), 'files_bytes': {p.name:p.stat().st_size for p in db.parent.glob('ctn.sqlite3*') if p.is_file()}}
report['journal_mode'] = conn.execute('PRAGMA journal_mode').fetchone()[0]
report['quick_check'] = [r[0] for r in conn.execute('PRAGMA quick_check')]
tables = ['posts','post_versions','counter_snapshots','raw_records','feed_observations','api_call_logs','score_snapshots','official_activity']
report['rows'] = {t:conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in tables}
report['samples_15m_by_source'] = [dict(r) for r in conn.execute('SELECT r.source,count(*) AS samples,max(c.observed_at) AS last_sample FROM counter_snapshots c LEFT JOIN raw_records r ON c.raw_id=r.id WHERE c.observed_at>=? GROUP BY r.source', (since,))]
report['requests_15m'] = [dict(r) for r in conn.execute("SELECT service,CASE WHEN path LIKE '%/counters' THEN 'counters' WHEN path='/api/posts' THEN 'feed' WHEN path LIKE '%/replies' THEN 'replies' ELSE 'other' END AS bucket,status,count(*) AS count,max(occurred_at) AS last_at FROM api_call_logs WHERE occurred_at>=? GROUP BY service,bucket,status", (since,))]
report['runs'] = []
for r in conn.execute('SELECT * FROM collection_runs ORDER BY id DESC LIMIT 4'):
    m = json.loads(r['metrics_json'] or '{}')
    report['runs'].append({k:r[k] for k in ('id','started_at','finished_at','status')} | {'error_counts': dict(Counter(str(e.get('status')) for e in m.get('errors',[]))), 'metrics':{k:m.get(k) for k in ('posts','counters','accounts','news','feeds')}})
report['recorded_lock_errors'] = conn.execute("SELECT count(*) FROM collection_runs WHERE error LIKE '%locked%' OR metrics_json LIKE '%database is locked%' OR metrics_json LIKE '%SQLITE_BUSY%'").fetchone()[0]
queries = {
    'latest_100': ('SELECT post_id,body,created_at FROM posts ORDER BY created_at DESC LIMIT 100',()),
    'one_post_history': ('SELECT * FROM counter_snapshots WHERE post_id=? ORDER BY observed_at',(7990,)),
    'latest_counts_all_posts': ('SELECT p.post_id,c.like_count,c.reply_count,c.repost_count,c.view_count FROM posts p LEFT JOIN counter_snapshots c ON c.id=(SELECT id FROM counter_snapshots WHERE post_id=p.post_id ORDER BY observed_at DESC,id DESC LIMIT 1)',()),
    'hourly_snapshot_aggregate': ("SELECT substr(observed_at,1,13),count(*),avg(like_count) FROM counter_snapshots GROUP BY substr(observed_at,1,13)",())
}
report['sql_timings'] = {}
for name,(sql,args) in queries.items():
    values=[]
    for _ in range(3):
        start=time.perf_counter(); rows=conn.execute(sql,args).fetchall(); values.append(round((time.perf_counter()-start)*1000,2))
    report['sql_timings'][name]={'ms':values,'median_ms':statistics.median(values),'rows':len(rows),'plan':[r[3] for r in conn.execute('EXPLAIN QUERY PLAN '+sql,args)]}
conn.close()
report['http'] = {}
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for endpoint in ('health','posts','overview','analysis'):
    start=time.perf_counter()
    try:
        with opener.open('http://127.0.0.1:8765/api/'+endpoint,timeout=30) as response:
            body=response.read()
            report['http'][endpoint]={'status':response.status,'ms':round((time.perf_counter()-start)*1000,2),'bytes':len(body)}
    except Exception as exc:
        report['http'][endpoint]={'error':type(exc).__name__,'ms':round((time.perf_counter()-start)*1000,2)}
dest=ROOT/'reports/generated/storage-audit-20261019.json'
dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
