"""Opt-in integration check; creates and drops only its own random test DB."""
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql

from ctn_data import task_queue
from ctn_data.collector import plan_counter_tasks, execute_counter_task, collect_stage
from ctn_data.db import connect, initialize, utc_now
from ctn_data.ingest import save_posts


def main():
    from ctn_data.secret_files import read_secret
    source = read_secret(Path('data/secrets/postgres_url')).strip()
    parts = urlsplit(source)
    name = 'ctn_queue_test_' + uuid.uuid4().hex[:12]
    target = urlunsplit(parts._replace(path='/'+name))
    admin = psycopg.connect(source, autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    conn = None
    result = {}
    try:
        initialize(target)
        conn = connect(target)
        task_queue.ensure_schema(conn)
        save_posts(conn,[{'post_id':1,'body':'queue integration','created_at':utc_now()}],'test')
        result['planned'] = plan_counter_tasks(conn, {'counter_limit':10})
        barrier = threading.Barrier(2)
        claims = []
        def claim(worker):
            c=connect(target)
            try:
                barrier.wait()
                claims.append(task_queue.claim(c,worker,['counter']))
            finally: c.close()
        workers=[threading.Thread(target=claim,args=(str(i),)) for i in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        assert sum(bool(t) for t in claims)==1
        task=next(t for t in claims if t)
        class Transport:
            attempts=0
            waited=0
            def __init__(self,*args,**kwargs): pass
            def get_json(self,*args,**kwargs):
                self.attempts+=1
                return {'like_count':3,'reply_count':2,'repost_count':1,'view_count':9}
        with patch('ctn_data.collector.ReadOnlyTransport',Transport):
            observed=execute_counter_task(target,{},task)
        assert observed['counters']==1,observed
        assert conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0]==1
        assert task_queue.claim(conn,'third',['counter']) is None
        result['counter_saved_atomically']=True
        result['recent_sample_not_requeued']=plan_counter_tasks(conn,{'counter_limit':10})==0
        with conn:
            task_queue.enqueue(conn,'crash','counter',{},due_at=100)
        stale=task_queue.claim(conn,'dead',['counter'],now=100,lease_seconds=2)
        recovered=task_queue.claim(conn,'recovered',['counter'],now=103)
        assert recovered and stale['lease_token']!=recovered['lease_token']
        assert not task_queue.complete(conn,'crash',stale['lease_token'],now=104)
        result['expired_lease_fenced']=True
        class FeedTransport(Transport):
            def get_json(self,*args,**kwargs):
                self.attempts+=1
                return {'items':[{'post_id':2,'body':'new feed','created_at':utc_now()}],'next_cursor':None}
        with patch('ctn_data.collector.ReadOnlyTransport',FeedTransport):
            feed=collect_stage(target,{},'latest')
        assert feed['status']=='completed' and feed['posts']==1,feed
        result['latest_stage_saved']=True
        print(json.dumps(result))
    finally:
        if conn is not None: conn.close()
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


if __name__=='__main__': main()
