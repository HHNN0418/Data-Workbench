from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

from ctn_data import feed_tasks, task_queue
from ctn_data.db import connect
from ctn_data.coordination import extend_cooldown, permit
from ctn_data.scheduler import CollectionScheduler, proxy_feed_available
from ctn_data.collector import collect_stage
from tests.test_collection import BaseDatabase


def post(pid):
    return {'post_id':pid,'body':str(pid),'created_at':f'2026-10-19T00:{pid//60:02}:{pid%60:02}Z'}


class FeedTaskTests(BaseDatabase):
    def state(self,key):
        row=self.conn.execute('SELECT value FROM checkpoints WHERE key=?',(key,)).fetchone()
        return json.loads(row['value']) if row else {}

    def put(self,key,value):
        with self.conn:
            self.conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                              (key,json.dumps(value),'2026-10-19T01:00:00Z'))

    def apply(self,plan,ids,cursor):
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            return feed_tasks.apply(self.conn,plan,{'items':[post(pid) for pid in ids],'next_cursor':cursor},'2026-10-19T01:00:00Z','proxy:test')

    def test_gap_and_live_head_keep_original_watermark_until_all_pages_saved(self):
        boundary=list(feed_tasks._position(post(10)))
        self.put('feed:latest',{'watermark':boundary,'reached_end':True})
        first=feed_tasks.prepare(self.conn,'latest')
        self.assertEqual(first['expected_state'],{})
        self.assertEqual(self.state('feed:latest:live'),{})  # prepare cannot commit state
        self.apply(first,range(50,40,-1),'A')
        head=feed_tasks.prepare(self.conn,'latest_head')
        self.assertNotIn('cursor=',head['path'])
        self.apply(head,range(60,50,-1),'head-cursor')
        self.assertEqual(self.state('feed:latest:live')['cursor'],'A')
        self.assertEqual(self.state('feed:latest:live')['watermark'],boundary)
        for high,cursor in ((40,'B'),(30,'C'),(20,'D'),(10,'E')):
            plan=feed_tasks.prepare(self.conn,'latest')
            self.assertIn('cursor=',plan['path'])
            self.apply(plan,range(high,high-10,-1),cursor)
        state=self.state('feed:latest:live')
        self.assertIsNone(state['cursor'])
        self.assertEqual(state['watermark'],list(feed_tasks._position(post(50))))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],60)
        self.assertEqual(self.state('feed:latest')['watermark'],boundary)

    def test_old_plan_cannot_overwrite_new_cursor_or_insert_stale_result(self):
        old=feed_tasks.prepare(self.conn,'latest')
        self.apply(old,[50,49],'A')
        with self.assertRaises(feed_tasks.StaleFeedPlan):
            self.apply(old,[999],'OLD')
        self.assertEqual(self.state('feed:latest:live')['cursor'],'A')
        self.assertIsNone(self.conn.execute('SELECT post_id FROM posts WHERE post_id=999').fetchone())

    def test_apply_rolls_back_with_callers_lease_transaction(self):
        plan=feed_tasks.prepare(self.conn,'latest')
        with self.assertRaises(RuntimeError):
            with self.conn:
                self.conn.execute('BEGIN IMMEDIATE')
                feed_tasks.apply(self.conn,plan,{'items':[post(1)],'next_cursor':None},'2026-10-19T01:00:00Z')
                raise RuntimeError('lease/result transaction failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)
        self.assertEqual(self.state('feed:latest:live'),{})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],0)

    def test_completed_history_and_unneeded_head_do_not_create_network_work(self):
        self.put('feed:latest',{'reached_end':True,'cursor':None})
        self.assertIsNone(feed_tasks.prepare(self.conn,'history'))
        self.assertIsNone(feed_tasks.prepare(self.conn,'latest_head'))
        self.assertIsNone(feed_tasks.prepare(self.conn,'trending_head'))
        self.assertIsNotNone(feed_tasks.prepare(self.conn,'latest'))

    def test_bad_or_repeated_cursor_never_commits_page(self):
        self.apply(feed_tasks.prepare(self.conn,'latest'),[10],'A')
        plan=feed_tasks.prepare(self.conn,'latest')
        for cursor in ('A','https://not-a-cursor'):
            with self.assertRaises((ValueError,RuntimeError)):
                self.apply(plan,[9],cursor)
        self.assertEqual(self.state('feed:latest:live')['cursor'],'A')
        self.assertIsNone(self.conn.execute('SELECT post_id FROM posts WHERE post_id=9').fetchone())

    def test_expired_cursor_reset_is_fenced_by_expected_state(self):
        self.apply(feed_tasks.prepare(self.conn,'latest'),[10],'A')
        stale=feed_tasks.prepare(self.conn,'latest')
        self.apply(stale,[9],'B')
        with self.conn:
            self.assertFalse(feed_tasks.reset_expired(self.conn,stale,410))
            self.assertTrue(feed_tasks.reset_expired(self.conn,feed_tasks.prepare(self.conn,'latest'),410))
        self.assertIsNone(self.state('feed:latest:live')['cursor'])

    def test_next_due_respects_shared_cooldown_and_pending_gap(self):
        self.apply(feed_tasks.prepare(self.conn,'latest'),[10],'A')
        self.assertEqual(feed_tasks.next_due(self.conn,'latest',{'status':'completed'},now=100),108)
        extend_cooldown(self.conn,'feeds',500)
        self.assertEqual(feed_tasks.next_due(self.conn,'latest',{'status':'deferred'},now=100),500)
        self.assertEqual(feed_tasks.next_due(self.conn,'trending',{'status':'completed'},now=100),500)

    def test_proxy_healthy_route_and_sixty_second_offline_fallback(self):
        marker=Path(self.directory.name)/'proxy-enabled'
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('worker_status:a',?)",(json.dumps({'status':'online','last_seen_epoch':100,'supports_feed':True}),))
            self.conn.execute("INSERT INTO meta(key,value) VALUES('worker_status:b',?)",(json.dumps({'status':'waiting','last_seen_epoch':110,'supports_feed':True}),))
        self.assertFalse(proxy_feed_available(self.conn,now=150,marker_path=marker))
        marker.touch()
        self.assertTrue(proxy_feed_available(self.conn,now=150,marker_path=marker))
        self.assertTrue(proxy_feed_available(self.conn,now=170,marker_path=marker))
        self.assertFalse(proxy_feed_available(self.conn,now=171,marker_path=marker))

    def test_old_counter_only_worker_cannot_suppress_direct_feed(self):
        marker=Path(self.directory.name)/'proxy-enabled'
        marker.touch()
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('worker_status:old',?)",(json.dumps({'status':'online','last_seen_epoch':100}),))
        self.assertFalse(proxy_feed_available(self.conn,now=120,marker_path=marker))

    def test_online_proxy_with_broken_network_does_not_suppress_direct_feed(self):
        marker=Path(self.directory.name)/'proxy-enabled'
        marker.touch()
        for code,expected in ((0,False),(400,False),(401,False),(403,False),(404,False),
                              (408,False),(410,False),(500,False),(503,False),(200,True),(429,True)):
            with self.conn:
                self.conn.execute("INSERT INTO meta(key,value) VALUES('worker_status:proxy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({'status':'online','last_seen_epoch':100,'supports_feed':True,'last_http_status':code}),))
            self.assertEqual(proxy_feed_available(self.conn,now=120,marker_path=marker),expected)

    def test_failed_proxy_heartbeat_cannot_restore_health_without_a_success(self):
        from ctn_data.worker_api import _heartbeat
        marker=Path(self.directory.name)/'proxy-enabled'
        marker.touch()
        with patch('ctn_data.worker_api.time.time',return_value=100):
            with self.conn:
                _heartbeat(self.conn,'proxy','online',force=True,supports_feed=True,
                           last_http_status=403,last_feed_http_status=403)
        with patch('ctn_data.worker_api.time.time',return_value=200):
            with self.conn:
                _heartbeat(self.conn,'proxy','idle',force=True)
        self.assertFalse(proxy_feed_available(self.conn,now=200,marker_path=marker))
        with patch('ctn_data.worker_api.time.time',return_value=201):
            with self.conn:
                _heartbeat(self.conn,'proxy','online',force=True,last_http_status=200)
        self.assertFalse(proxy_feed_available(self.conn,now=201,marker_path=marker))
        with patch('ctn_data.worker_api.time.time',return_value=202):
            with self.conn:
                _heartbeat(self.conn,'proxy','online',force=True,last_http_status=200,last_feed_http_status=200)
        self.assertTrue(proxy_feed_available(self.conn,now=202,marker_path=marker))

    def test_direct_feed_expired_lease_cannot_save_page_or_checkpoint(self):
        with self.conn:
            task_queue.enqueue(self.conn,'periodic:latest','latest')
        task=task_queue.claim(self.conn,'direct',('latest',))
        owner=self
        class Transport:
            attempts=1
            waited=0
            def __init__(self,*args,**kwargs): pass
            def get_json(self,*args):
                with owner.conn:
                    owner.conn.execute('UPDATE collection_tasks SET lease_until=0 WHERE task_key=?',(task['task_key'],))
                return {'items':[post(1)],'next_cursor':'A'}
        with patch('ctn_data.collector.ReadOnlyTransport',Transport):
            result=collect_stage(self.path,{},'latest',task=task)
        self.assertEqual(result['status'],'stale_lease')
        self.assertEqual(self.state('feed:latest:live'),{})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_direct_feed_task_completion_failure_rolls_back_page(self):
        with self.conn:
            task_queue.enqueue(self.conn,'periodic:latest','latest')
        task=task_queue.claim(self.conn,'direct',('latest',))
        class Transport:
            attempts=1
            waited=0
            def __init__(self,*args,**kwargs): pass
            def get_json(self,*args): return {'items':[post(1)],'next_cursor':'A'}
        with patch('ctn_data.collector.ReadOnlyTransport',Transport),patch('ctn_data.task_queue.finish',side_effect=RuntimeError('transaction failure')):
            with self.assertRaises(RuntimeError):
                collect_stage(self.path,{},'latest',task=task)
        self.assertEqual(self.state('feed:latest:live'),{})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0],0)

    def test_direct_lane_does_not_claim_feeds_until_proxy_offline(self):
        claimed=threading.Event()
        proxy={'healthy':True}
        scheduler=CollectionScheduler(self.path,{},enabled=True)
        task_queue.ensure_schema(self.conn)
        scheduler._seed(self.conn)
        def run(*args,**kwargs):
            claimed.set()
            scheduler.stopping=True
            return {'status':'completed','errors':[],'requests':0}
        with patch('ctn_data.scheduler.proxy_feed_available',side_effect=lambda conn:proxy['healthy']),patch('ctn_data.scheduler.collect_stage',run):
            thread=threading.Thread(target=scheduler._worker_loop,args=('feeds',('latest',)))
            thread.start()
            try:
                self.assertFalse(claimed.wait(.2))
                self.assertEqual(self.conn.execute("SELECT state FROM collection_tasks WHERE task_key='periodic:latest'").fetchone()[0],'pending')
                proxy['healthy']=False
                self.assertTrue(claimed.wait(2))
            finally:
                scheduler.stopping=True
                thread.join(3)
        self.assertEqual(scheduler.feed_route,'direct')

    def test_auxiliary_and_news_use_explicit_public_egress(self):
        class Transport:
            attempts=0
            waited=0
            def __init__(self,*args,**kwargs): self.kwargs=kwargs
            def get_json(self,*args,**kwargs):
                self.attempts+=1
                return {'sites':[]}
        expected={'proxy_url':'http://127.0.0.1:18881','actor':'proxy:docker-au-a'}
        for kind in ('auxiliary','news'):
            with patch('ctn_data.egress.choose_public_egress',return_value=expected) as choose,patch('ctn_data.collector.ReadOnlyTransport',side_effect=Transport) as create,patch('ctn_data.collector._refresh'):
                result=collect_stage(self.path,{},kind)
            self.assertEqual(result['status'],'completed')
            choose.assert_called_once()
            self.assertEqual(create.call_args.kwargs['actor'],expected['actor'])
            self.assertEqual(create.call_args.kwargs['proxy_url'],expected['proxy_url'])
