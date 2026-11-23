import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data.db import connect, initialize
from ctn_data import task_queue
from ctn_data.scheduler import CollectionScheduler


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'tasks.db'
        initialize(self.path)
        self.conn = connect(self.path)
        task_queue.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def seed(self, due=100):
        with self.conn:
            task_queue.enqueue(self.conn, 'counter:1', 'counter', {'post_id':1}, due_at=due)

    def test_two_workers_claim_only_once_and_enqueue_keeps_lease(self):
        self.seed()
        barrier = threading.Barrier(2)
        claims = []
        def worker(name):
            conn = connect(self.path)
            try:
                barrier.wait()
                claims.append(task_queue.claim(conn, name, ['counter'], now=100))
            finally:
                conn.close()
        threads = [threading.Thread(target=worker,args=(str(n),)) for n in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(bool(c) for c in claims), 1)
        held = next(c for c in claims if c)
        self.seed()
        self.assertTrue(task_queue.owned(self.conn,'counter:1',held['lease_token'],now=101))

    def test_expired_lease_reclaimed_and_old_result_cannot_write(self):
        self.seed()
        first = task_queue.claim(self.conn,'lost',['counter'],lease_seconds=5,now=100)
        second = task_queue.claim(self.conn,'replacement',['counter'],lease_seconds=5,now=106)
        self.assertNotEqual(first['lease_token'],second['lease_token'])
        writes = []
        self.assertFalse(task_queue.complete(self.conn,'counter:1',first['lease_token'],
            ingest_callback=lambda c:writes.append(1),now=107))
        self.assertEqual(writes,[])
        self.assertTrue(task_queue.complete(self.conn,'counter:1',second['lease_token'],now=107))

    def test_ingestion_and_completion_rollback_together(self):
        self.seed()
        task = task_queue.claim(self.conn,'worker',['counter'],now=100)
        def broken(conn):
            conn.execute("INSERT INTO meta(key,value) VALUES('atomic-test','bad')")
            raise ValueError('simulated ingest failure')
        with self.assertRaises(ValueError):
            task_queue.complete(self.conn,'counter:1',task['lease_token'],ingest_callback=broken,now=101)
        self.assertIsNone(self.conn.execute("SELECT value FROM meta WHERE key='atomic-test'").fetchone())
        self.assertTrue(task_queue.owned(self.conn,'counter:1',task['lease_token'],now=101))

    def test_due_work_survives_reconnect_and_cannot_starve_other_lane(self):
        self.seed()
        with self.conn:
            task_queue.enqueue(self.conn,'periodic:latest','latest',due_at=90)
        feed = task_queue.claim(self.conn,'feed',['latest'],now=100)
        self.conn.close()
        self.conn = connect(self.path)
        counter = task_queue.claim(self.conn,'counter',['counter'],now=101)
        self.assertIsNotNone(counter)
        self.assertTrue(task_queue.owned(self.conn,feed['task_key'],feed['lease_token'],now=101))

    def test_continuous_high_priority_work_does_not_starve_overdue_task(self):
        with self.conn:
            task_queue.enqueue(self.conn,'trending','feed',priority=1,due_at=90)
            task_queue.enqueue(self.conn,'latest','feed',priority=100,due_at=99)
        self.assertEqual(task_queue.claim(self.conn,'feed',['feed'],now=100)['task_key'],'trending')

    def test_scheduler_counter_lane_progresses_while_feed_is_waiting(self):
        entered,release,sampled=threading.Event(),threading.Event(),threading.Event()
        def stage(db,config,kind,**kwargs):
            if kind=='latest':
                entered.set()
                release.wait(2)
            return {'status':'completed','requests':0,'errors':[]}
        def plan(conn,config):
            with conn:
                task_queue.enqueue(conn,'counter:1','counter',{'post_id':1})
        def counter(db,config,task):
            sampled.set()
            conn=connect(db)
            try: task_queue.complete(conn,task['task_key'],task['lease_token'])
            finally: conn.close()
            return {'status':'completed','requests':1,'counters':1,'errors':[]}
        scheduler=CollectionScheduler(self.path,{},enabled=True)
        with patch('ctn_data.scheduler.collect_stage',stage), patch('ctn_data.scheduler.plan_counter_tasks',plan), patch('ctn_data.scheduler.execute_counter_task',counter):
            scheduler.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(sampled.wait(1))
                self.assertEqual(scheduler.status()['mode'],'durable_lanes')
            finally:
                release.set()
                scheduler.stop()

    def test_collect_now_uses_existing_queue_and_keeps_inflight_lease(self):
        scheduler=CollectionScheduler(self.path,{},enabled=True)
        scheduler._seed(self.conn)
        held=task_queue.claim(self.conn,'active',['latest'])
        with patch('ctn_data.scheduler.plan_counter_tasks',return_value=0):
            result=scheduler.collect_now({})
        self.assertEqual(result['status'],'queued')
        self.assertTrue(task_queue.owned(self.conn,held['task_key'],held['lease_token']))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM collection_tasks WHERE task_key='periodic:latest'").fetchone()[0],1)
        with self.assertRaises(ValueError): scheduler.collect_now({'counter_limit':30})
        scheduler.configure(False,60)
        with self.assertRaises(ValueError): scheduler.collect_now({})
