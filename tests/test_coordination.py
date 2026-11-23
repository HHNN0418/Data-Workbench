import io
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from ctn_data.coordination import permit, extend_cooldown
from ctn_data.db import connect
from ctn_data.ingest import save_posts, save_counters
from ctn_data.transport import ReadOnlyTransport, RequestDeferred
from tests.test_collection import BaseDatabase, FakeClock, Opener, Response


class CoordinationTests(BaseDatabase):
    def test_post_lease_blocks_different_actor_without_blocking_other_posts(self):
        self.assertTrue(permit(self.conn,'/api/posts/1/counters','browser',1000)['allowed'])
        blocked=permit(self.conn,'/api/posts/1/counters','backend',1001)
        self.assertEqual(blocked['reason'],'post_in_flight')
        self.assertTrue(permit(self.conn,'/api/posts/2/counters','backend',1001)['allowed'])
        self.assertTrue(permit(self.conn,'/api/posts/1/counters','proxy:one',1031)['allowed'])

    def test_recent_real_observation_is_reused_across_all_workers(self):
        save_posts(self.conn,[{'post_id':1,'body':'one'}],'test')
        save_counters(self.conn,1,{'like_count':2},'1970-01-01T00:16:40Z')
        for actor in ('browser','backend','proxy:one'):
            result=permit(self.conn,'/api/posts/1/counters',actor,1020)
            self.assertFalse(result['allowed'])
            self.assertEqual(result['reason'],'recent_sample')
        self.assertTrue(permit(self.conn,'/api/posts/1/counters','backend',1061)['allowed'])

    def test_simultaneous_counter_workers_share_post_ownership(self):
        def request(actor):
            conn=connect(self.path)
            try:return permit(conn,'/api/posts/1/counters',actor,1000)
            finally:conn.close()
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(request,['browser','proxy:one']))
        self.assertEqual(sum(result['allowed'] for result in results),1)

    def test_simultaneous_connections_share_one_feed_slot(self):
        def request(actor):
            conn=connect(self.path)
            try:return permit(conn,'/api/posts?sort=recent',actor,1000)
            finally:conn.close()
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(request,['backend','browser']))
        self.assertEqual(sum(r['allowed'] for r in results),1)
        self.assertTrue(permit(self.conn,'/api/posts/1/counters','browser',1000)['allowed'])
        self.assertFalse(permit(self.conn,'/api/posts','browser',1005)['allowed'])
        self.assertTrue(permit(self.conn,'/api/posts','browser',1006)['allowed'])

    def test_cooldown_survives_reconnect_and_never_shortens(self):
        extend_cooldown(self.conn,'feeds',2000)
        extend_cooldown(self.conn,'feeds',1200)
        other=connect(self.path)
        try:
            r=permit(other,'/api/posts','browser',1500)
            self.assertFalse(r['allowed'])
            self.assertEqual(r['reason'],'cooldown')
            self.assertTrue(permit(other,'/api/posts','browser',2001)['allowed'])
        finally:other.close()

    def test_backend_waits_for_browser_without_extra_remote_calls(self):
        clock=FakeClock()
        opener=Opener([Response({'items':[]})])
        transport=ReadOnlyTransport(self.conn,opener=opener,clock=clock,sleeper=clock.sleep)
        permit(self.conn,'/api/posts','browser',transport.epoch_base)
        transport.get_json('social','/api/posts')
        self.assertEqual(len(opener.calls),1)
        self.assertGreaterEqual(clock.now,6)

    def test_feed_cooldown_does_not_prevent_counter_sampling(self):
        extend_cooldown(self.conn,'feeds',time.time()+300)
        opener=Opener([Response({'like_count':1},url='https://social.legitreal.com/api/posts/1/counters')])
        transport=ReadOnlyTransport(self.conn,opener=opener,max_wait_seconds=0)
        with self.assertRaises(RequestDeferred):transport.get_json('social','/api/posts')
        self.assertEqual(transport.get_json('social','/api/posts/1/counters')['like_count'],1)
        self.assertEqual(len(opener.calls),1)

    def test_429_does_not_immediately_retry_and_blocks_browser(self):
        error=urllib.error.HTTPError('https://social.legitreal.com/api/posts',429,'limit',{'Retry-After':'60'},io.BytesIO())
        opener=Opener([error])
        transport=ReadOnlyTransport(self.conn,opener=opener)
        with self.assertRaises(RequestDeferred):transport.get_json('social','/api/posts')
        self.assertEqual(len(opener.calls),1)
        result=permit(self.conn,'/api/posts','browser')
        self.assertEqual(result['reason'],'cooldown')
        self.assertGreater(result['retry_after_ms'],59000)
