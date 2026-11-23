"""Reproducible synthetic scale check, isolated from the production database."""
import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from ctn_data.db import connect,initialize
from ctn_data.analytics import analyze


def main():
    root=ROOT/'data'/'test-tmp';root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temp:
        database=Path(temp)/'scale.sqlite3';initialize(database)
        connection=connect(database)
        start=datetime(2026,10,19,7,tzinfo=timezone.utc)
        def stamp(value):return value.isoformat(timespec='microseconds').replace('+00:00','Z')
        posts=1000;samples=25
        with connection:
            connection.execute("UPDATE meta SET value='synthetic-validation' WHERE key='mode'")
            connection.executemany('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)',
                                   [(i,i%25+1,'post','SYNTHETIC VALIDATION '+('sample '*(i%40+1)),stamp(start),stamp(start),stamp(start)) for i in range(1,posts+1)])
            connection.executemany('INSERT INTO counter_snapshots(post_id,observed_at,like_count,reply_count,repost_count,view_count) VALUES(?,?,?,?,?,?)',
                                   ((i,stamp(start+timedelta(minutes=j*5)),j*(i%4),j*(i%3),j*(i%2),j*(i%13)) for i in range(1,posts+1) for j in range(samples)))
        connection.close()
        tracemalloc.start();before=time.perf_counter()
        result=analyze(database,{'window_minutes':60,'tolerance_minutes':5,'detail_limit':100})
        duration=time.perf_counter()-before;current,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        assert result['summary']['posts']==posts
        assert result['summary']['counter_snapshots']==posts*samples
        assert result['growth_total']==posts
        assert len(result['growth'])==100 and result['growth_truncated']
        assert result['quality']['max_post_snapshots_buffered']==samples
        report={'fixture':'synthetic-validation, not competition data','posts':posts,'snapshots':posts*samples,
                'duration_seconds':round(duration,3),'peak_python_memory_mib':round(peak/1024/1024,3),
                'buffered_snapshots_max':samples,'growth_all_samples':result['growth_total'],'growth_displayed':len(result['growth']),
                'passed':True,'limit':'This is one local fixture, not a throughput guarantee for all workloads.'}
        target=ROOT/'reports'/'generated'/'scale-validation.json';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
