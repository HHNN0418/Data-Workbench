"""Read-only operational snapshot; emits aggregates, never credentials or cursors."""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ctn_data.db import connect, utc_now
from ctn_data.queries import request_traffic
from ctn_data.coverage import coverage


def observe():
    from ctn_data.secret_files import read_secret
    conn = connect(read_secret(ROOT/'data/secrets/postgres_url').strip())
    try:
        now = datetime.now(timezone.utc)
        since = (now-timedelta(minutes=10)).isoformat().replace('+00:00','Z')
        result = {'at':utc_now(), 'traffic':request_traffic(conn)}
        result['counts'] = {table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
            for table in ('posts','accounts','counter_snapshots','score_snapshots','official_activity')}
        result['coverage'] = coverage(conn, {}, [], now=now)
        result['routes_10m'] = [dict(r) for r in conn.execute('''SELECT service,
            CASE WHEN path='/api/posts' THEN 'feed' WHEN path LIKE '%/counters' THEN 'counter'
            WHEN path LIKE '/api/accounts/%' THEN 'account' ELSE 'other' END kind,
            status,COUNT(*) requests FROM api_call_logs WHERE occurred_at>=?
            GROUP BY service,kind,status ORDER BY service,kind,status''',(since,))]
        result['duplicate_counters_under_60s_10m'] = conn.execute('''WITH samples AS (
            SELECT path,occurred_at,LAG(occurred_at) OVER(PARTITION BY path ORDER BY occurred_at) prev
            FROM api_call_logs WHERE occurred_at>=? AND path LIKE '%/counters' AND status=200)
            SELECT COUNT(*) FROM samples WHERE (julianday(occurred_at)-julianday(prev))*86400<59.9''',(since,)).fetchone()[0]
        result['workers'] = [json.loads(r['value']) for r in conn.execute("SELECT value FROM meta WHERE key LIKE 'worker_status:%'")]
        result['scores'] = [dict(r) for r in conn.execute('SELECT id,observed_at,total_decimal,source FROM score_snapshots ORDER BY id DESC LIMIT 4')]
        result['tasks'] = [dict(r) for r in conn.execute("SELECT kind,state,lease_owner,result_json FROM collection_tasks WHERE kind<>'counter' ORDER BY kind")]
        return result
    finally:
        conn.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=observe()
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('at','counts','traffic','duplicate_counters_under_60s_10m')},ensure_ascii=False))
