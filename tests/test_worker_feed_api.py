import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ctn_data import coordination,task_queue
from ctn_data.db import connect,initialize,utc_now
from ctn_data.ingest import save_posts
from ctn_data.worker_api import register_worker_routes


class FeedWorkerAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.temp.name)
        self.db=self.root/'worker.db'
        self.secret=self.root/'token'
        self.secret.write_text('x'*48)
        initialize(self.db)
        self.conn=connect(self.db)
        task_queue.ensure_schema(self.conn)
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('auto_collection',?)",('{"enabled":true}',))
            task_queue.enqueue(self.conn,'periodic:latest','latest',due_at=1)
        app=FastAPI()
        @app.exception_handler(ValueError)
        async def invalid(request,exc):return JSONResponse({'error':str(exc)},status_code=400)
        register_worker_routes(app,self.db,self.secret,{'page_limit':200,'interval_seconds':60})
        self.client=TestClient(app)

    def tearDown(self):
        self.client.close()
        self.conn.close()
        self.temp.cleanup()

    def post(self,action,body=None,worker='feed-test'):
        body={'worker':worker,**(body or {})}
        return self.client.post('/api/worker/'+action,content=json.dumps(body,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+'x'*48,'Content-Type':'application/json'})

    def claim(self,worker='feed-test'):
        response=self.post('claim',{'capabilities':['counter','feed']},worker)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['task']

    def result(self,task,*,payload=None,status=200):
        return {**task,'observation_id':hashlib.sha256((task['task_key']+':'+task['lease_token']).encode()).hexdigest(),
                'observed_at':utc_now(),'status':status,'retry_after':120,
                'payload':payload if payload is not None else {'items':[{'post_id':10,'body':'正文','created_at':utc_now()}],'next_cursor':'page2'}}

    def state(self):
        row=self.conn.execute("SELECT value FROM checkpoints WHERE key='feed:latest:live'").fetchone()
        return json.loads(row['value']) if row else None

    def seed_counter(self):
        with self.conn:
            save_posts(self.conn,[{'post_id':1,'body':'counter'}],'test')
            task_queue.enqueue(self.conn,'counter:1','counter',{'post_id':1},due_at=1)

    def test_feed_commit_data_cursor_receipt_and_retry_once(self):
        task=self.claim()
        self.assertEqual(task['kind'],'latest')
        self.assertIn('limit=100',task['path'])
        self.assertEqual(self.claim(),task)
        result=self.result(task)
        first=self.post('result',result)
        self.assertEqual(first.status_code,200,first.text)
        self.assertTrue(self.post('result',result).json()['duplicate'])
        self.assertEqual(self.conn.execute('SELECT body FROM posts WHERE post_id=10').fetchone()[0],'正文')
        self.assertEqual(self.state()['cursor'],'page2')
        self.assertEqual(self.state()['pages_completed'],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM feed_observations').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM api_call_logs').fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkpoints WHERE key LIKE 'worker_result:%'").fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM raw_records WHERE source='proxy:feed-test:latest'").fetchone()[0],1)
        due=self.conn.execute("SELECT due_at FROM collection_tasks WHERE task_key='periodic:latest'").fetchone()[0]
        self.assertLess(due-time.time(),10)

    def test_last_page_schedules_head_interval_after_cursor_cleared(self):
        task=self.claim()
        self.assertEqual(self.post('result',self.result(task)).status_code,200)
        with self.conn:self.conn.execute("UPDATE collection_tasks SET due_at=1 WHERE task_key='periodic:latest'")
        task=self.claim()
        self.assertIn('cursor=page2',task['path'])
        result=self.result(task,payload={'items':[],'next_cursor':None})
        self.assertEqual(self.post('result',result).status_code,200)
        self.assertIsNone(self.state()['cursor'])
        due=self.conn.execute("SELECT due_at FROM collection_tasks WHERE task_key='periodic:latest'").fetchone()[0]
        self.assertGreater(due-time.time(),50)

    def test_stale_plan_cannot_write_posts_or_cursor_and_retries_soon(self):
        task=self.claim()
        newer={'cursor':'advanced','pages_completed':3}
        with self.conn:
            self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',('feed:latest:live',json.dumps(newer),utc_now()))
        self.assertEqual(self.post('result',self.result(task)).status_code,200)
        self.assertEqual(self.state(),newer)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)
        row=self.conn.execute("SELECT due_at,result_json FROM collection_tasks WHERE task_key='periodic:latest'").fetchone()
        self.assertEqual(json.loads(row['result_json'])['status'],'stale_plan')
        self.assertLess(row['due_at']-time.time(),3)

    def test_expired_and_reassigned_lease_cannot_write(self):
        task=self.claim()
        with self.conn:self.conn.execute("UPDATE collection_tasks SET lease_until=0 WHERE task_key='periodic:latest'")
        replacement=self.claim(worker='replacement')
        self.assertNotEqual(task['lease_token'],replacement['lease_token'])
        self.assertEqual(self.post('result',self.result(task)).status_code,409)
        self.assertIsNone(self.state())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_feed_429_does_not_block_counters_and_worker_chooses_counter(self):
        self.seed_counter()
        feed=self.claim()
        self.assertEqual(feed['kind'],'latest')
        self.assertEqual(self.post('result',self.result(feed,status=429)).status_code,200)
        self.assertFalse(coordination.permit(self.conn,'/api/posts?sort=recent','backend')['allowed'])
        self.assertTrue(coordination.permit(self.conn,'/api/posts/1/counters','backend')['allowed'])
        counter=self.claim()
        self.assertEqual(counter['kind'],'counter')
        result=self.result(counter,payload={'like_count':4,'repost_count':1})
        self.assertEqual(self.post('result',result).status_code,200)
        self.assertEqual(self.conn.execute('SELECT like_count FROM counter_snapshots WHERE post_id=1').fetchone()[0],4)

    def test_legacy_worker_and_changed_capabilities_never_receive_feed(self):
        self.seed_counter()
        feed=self.claim()
        counter=self.post('claim').json()['task']
        self.assertEqual(counter['kind'],'counter')
        row=self.conn.execute('SELECT state FROM collection_tasks WHERE task_key=?',(feed['task_key'],)).fetchone()
        self.assertEqual(row['state'],'pending')
        for caps in (None,'feed',{'feed':True},[1],['unknown']):
            self.assertEqual(self.post('claim',{'capabilities':caps},worker='invalid').status_code,400)

    def test_large_chinese_page_commits_without_ascii_expansion(self):
        task=self.claim()
        body='中文正文'*160000
        result=self.result(task,payload={'items':[{'post_id':10,'body':body}],'next_cursor':None})
        response=self.post('result',result)
        self.assertEqual(response.status_code,200,response.text[:200])
        self.assertEqual(self.conn.execute('SELECT body FROM posts WHERE post_id=10').fetchone()[0],body)

    def test_invalid_page_rolls_back_data_cursor_receipt_and_lease(self):
        task=self.claim()
        result=self.result(task,payload={'items':[{'post_id':10,'body':'valid'},{'post_id':-1}],'next_cursor':'page2'})
        self.assertEqual(self.post('result',result).status_code,400)
        self.assertIsNone(self.state())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM api_call_logs').fetchone()[0],0)
        self.assertTrue(task_queue.owned(self.conn,task['task_key'],task['lease_token']))


if __name__=='__main__':unittest.main()
