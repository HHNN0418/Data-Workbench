import hashlib
import io
import json
import errno
import socket
from contextlib import redirect_stdout
from http.client import RemoteDisconnected
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError, URLError

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ctn_data.db import initialize, connect, utc_now
from ctn_data.ingest import save_posts
from ctn_data import task_queue, coordination
from ctn_data.worker_api import register_worker_routes
from ctn_data.proxy_worker import ProxyWorker, retry_seconds


class WorkerAPITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.temp.name)
        self.db=self.root/'test.db'
        self.secret=self.root/'token'
        self.secret.write_text('x'*48)
        initialize(self.db)
        self.conn=connect(self.db)
        task_queue.ensure_schema(self.conn)
        with self.conn:
            self.conn.execute("INSERT INTO meta(key,value) VALUES('auto_collection',?)",('{"enabled":true}',))
            save_posts(self.conn,[{'post_id':1,'body':'one'},{'post_id':2,'body':'two'}],'test')
            task_queue.enqueue(self.conn,'counter:1','counter',{'post_id':1})
            task_queue.enqueue(self.conn,'counter:2','counter',{'post_id':2})
        app=FastAPI()
        register_worker_routes(app,self.db,self.secret)
        self.client=TestClient(app)

    def tearDown(self):
        self.client.close()
        self.conn.close()
        self.temp.cleanup()

    def post(self,action,body=None,worker='test-worker',token=None):
        return self.client.post('/api/worker/'+action,json={'worker':worker,**(body or {})},headers={'Authorization':'Bearer '+(token or 'x'*48)})

    def claimed(self):
        return self.post('claim').json()['task']

    def result(self,task,status=200):
        return {**task,'observation_id':hashlib.sha256((task['task_key']+':'+task['lease_token']).encode()).hexdigest(),
                'observed_at':utc_now(),'status':status,'payload':{'like_count':7,'repost_count':3},'retry_after':120}

    def test_auth_and_claim_response_retry(self):
        self.assertEqual(self.post('claim',token='bad').status_code,401)
        task=self.claimed()
        self.assertEqual(self.claimed(),task)
        other=self.post('claim',worker='other').json()['task']
        self.assertNotEqual(task['task_key'],other['task_key'])
        self.assertEqual(self.post('permit',task,worker='other').status_code,409)

    def test_result_retry_is_one_observation_and_log(self):
        task=self.claimed()
        result=self.result(task)
        self.assertEqual(self.post('result',result).status_code,200)
        retry=self.post('result',result).json()
        self.assertTrue(retry['duplicate'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM api_call_logs').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT source FROM raw_records WHERE source LIKE ?',('proxy:%',)).fetchone()[0],'proxy:test-worker')

    def test_expired_lease_cannot_ingest(self):
        task=self.claimed()
        with self.conn:
            self.conn.execute('UPDATE collection_tasks SET lease_until=0 WHERE task_key=?',(task['task_key'],))
        self.assertEqual(self.post('result',self.result(task)).status_code,409)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0],0)

    def test_429_shared_with_direct_and_browser_and_releases_long_wait(self):
        task=self.claimed()
        self.assertEqual(self.post('result',self.result(task,429)).status_code,200)
        direct=coordination.permit(self.conn,'/api/posts/2/counters','backend')
        self.assertFalse(direct['allowed'])
        self.assertEqual(direct['reason'],'cooldown')
        second=self.claimed()
        response=self.post('permit',second).json()
        self.assertTrue(response['released'])
        self.assertFalse(response['allowed'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM counter_snapshots').fetchone()[0],0)

    def test_shared_pacing_and_stable_id_validation(self):
        task=self.claimed()
        self.assertTrue(self.post('permit',task).json()['allowed'])
        self.assertFalse(coordination.permit(self.conn,task['path'],'browser')['allowed'])
        result=self.result(task)
        result['observation_id']='wrong'
        self.assertEqual(self.post('result',result).status_code,400)

    def test_success_with_exhausted_quota_still_shares_cooldown(self):
        task=self.claimed()
        result={**self.result(task),'rate_limit':200,'rate_remaining':0,'rate_reset':60}
        before=time.time()
        self.assertEqual(self.post('result',result).status_code,200)
        after=time.time()
        until=coordination.cooldowns(self.conn)['counters']/1000
        self.assertGreaterEqual(until,before+.299)
        self.assertLessEqual(until,after+.301)
        blocked=coordination.permit(self.conn,'/api/posts/2/counters','backend',now=until-.1)
        self.assertFalse(blocked['allowed'])
        self.assertLessEqual(blocked['retry_after_ms'],101)
        self.assertTrue(coordination.permit(self.conn,'/api/posts/2/counters','backend',now=until+.001)['allowed'])
        self.assertEqual(self.conn.execute('SELECT rate_remaining FROM api_call_logs').fetchone()[0],0)

    def test_429_exhausted_quota_still_obeys_the_full_retry_after(self):
        task=self.claimed()
        result={**self.result(task,429),'rate_limit':300,'rate_remaining':0,'rate_reset':60,'retry_after':7}
        before=time.time()
        self.assertEqual(self.post('result',result).status_code,200)
        after=time.time()
        until=coordination.cooldowns(self.conn)['counters']/1000
        self.assertGreaterEqual(until,before+6.999)
        self.assertLessEqual(until,after+7.001)
        next_due=self.conn.execute('SELECT due_at FROM collection_tasks WHERE task_key=?',(task['task_key'],)).fetchone()[0]
        self.assertGreaterEqual(next_due,before+7)
        self.assertLessEqual(next_due,after+7)

    def test_absolute_rate_reset_retains_timestamp_cooldown(self):
        task=self.claimed()
        reset=int(time.time())+60
        result={**self.result(task),'rate_limit':300,'rate_remaining':0,'rate_reset':reset}
        self.assertEqual(self.post('result',result).status_code,200)
        until=coordination.cooldowns(self.conn)['counters']/1000
        self.assertAlmostEqual(until,reset,delta=.01)

    def test_pause_blocks_new_claim_and_permit_but_accepts_inflight_result(self):
        task=self.claimed()
        other=self.post('claim',worker='other').json()['task']
        with self.conn:
            self.conn.execute("UPDATE meta SET value=? WHERE key='auto_collection'",('{"enabled":false}',))
        self.assertIsNone(self.claimed())
        self.assertEqual(self.post('permit',task).json()['reason'],'paused')
        self.assertEqual(self.post('result',self.result(other),worker='other').status_code,200)

    def test_recent_observation_finishes_task_without_grant(self):
        from ctn_data.ingest import save_counters
        task=self.claimed()
        with self.conn: save_counters(self.conn,task['post_id'],{'like_count':2})
        response=self.post('permit',task).json()
        self.assertTrue(response['released'])
        self.assertFalse(response['allowed'])
        self.assertEqual(self.conn.execute('SELECT state FROM collection_tasks WHERE task_key=?',(task['task_key'],)).fetchone()[0],'done')


class ProxyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.temp.name)
        self.token=self.root/'token'
        self.token.write_text('a'*48)
        self.worker=ProxyWorker('http://backend','http://vpn:8888','test',self.token,self.root/'state')
        self.task={'task_key':'counter:1','lease_token':'lease','lease_until':time.time()+120,'post_id':1,'path':'/api/posts/1/counters'}

    def tearDown(self):
        self.temp.cleanup()

    def test_disconnected_proxy_never_uses_backend_opener_for_remote(self):
        self.worker.remote=Mock()
        self.worker.local=Mock()
        self.worker.remote.open.side_effect=OSError('vpn down')
        self.assertEqual(self.worker.sample(self.task)['status'],0)
        self.worker.local.open.assert_not_called()

    def test_pending_retry_preserves_observation_after_restart(self):
        result={'task_key':'counter:1','lease_token':'lease','observation_id':'stable','observed_at':'2026-10-19T00:00:00Z','status':200,'payload':{'like_count':1}}
        self.worker.pending.write_text(json.dumps(result))
        with patch.object(self.worker,'api',side_effect=OSError):
            with self.assertRaises(OSError): self.worker.flush()
        restarted=ProxyWorker('http://backend','http://vpn:8888','test',self.token,self.root/'state')
        with patch.object(restarted,'api',return_value={'accepted':True,'duplicate':True}) as api:
            restarted.flush()
            self.assertEqual(api.call_args.args,('result',result))
        self.assertFalse(restarted.pending.exists())

    def test_retry_after_date_and_remote_path_guard(self):
        self.assertEqual(retry_seconds('60'),60)
        self.assertEqual(retry_seconds('Thu, 01 Jan 1970 00:02:00 GMT',now=60),60)
        with self.assertRaises(ValueError): self.worker.sample({**self.task,'path':'https://untrusted.example/'})

    def test_invalid_remote_success_becomes_failure(self):
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.read.return_value=b'{"like_count":-1}'
        self.worker.remote=Mock()
        self.worker.remote.open.return_value=response
        self.assertEqual(self.worker.sample(self.task)['status'],0)

    def run_api_attempts(self, action, effects):
        self.worker.local=Mock()
        self.worker.local.open.side_effect=effects
        count=0
        def once():
            nonlocal count
            if count==len(effects):
                raise KeyboardInterrupt
            count+=1
            self.worker.api(action,{})
        output=io.StringIO()
        with patch.object(self.worker,'once',side_effect=once), patch.object(self.worker,'wait') as wait, redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.run()
        return output.getvalue(),wait

    def test_backend_retry_logs_safe_stage_reason_and_codes(self):
        sensitive='PRIVATE_URL_TOKEN_HEADER_BODY_MESSAGE'
        cases=[
            ('claim',URLError(ConnectionRefusedError(errno.ECONNREFUSED,sensitive)),
             ['URLError','reason=ConnectionRefusedError','errno='+str(errno.ECONNREFUSED)]),
            ('permit',URLError(socket.gaierror(socket.EAI_NONAME,sensitive)),
             ['URLError','reason=gaierror','errno='+str(socket.EAI_NONAME)]),
            ('result',TimeoutError(sensitive),['TimeoutError','reason=TimeoutError']),
            ('result',RemoteDisconnected(sensitive),['RemoteDisconnected','reason=RemoteDisconnected']),
            ('claim',HTTPError('http://'+sensitive,503,sensitive,{'Authorization':sensitive},io.BytesIO(sensitive.encode())),
             ['HTTPError','reason=HTTPError','http_status=503']),
            ('permit',URLError('http://'+sensitive),['URLError','reason=str']),
        ]
        for action,error,expected in cases:
            with self.subTest(action=action,error=type(error).__name__):
                self.worker._backend_retry_started=None
                output,wait=self.run_api_attempts(action,[error])
                self.assertIn('action='+action,output)
                self.assertIn('elapsed_ms=',output)
                for field in expected:
                    self.assertIn(field,output)
                self.assertNotIn(sensitive,output)
                self.assertNotIn('http://',output)
                self.assertNotIn(self.token.read_text(),output)
                self.assertEqual(wait.call_count,1)
                self.assertGreaterEqual(wait.call_args.args[0],2)
                self.assertLess(wait.call_args.args[0],3)

    def test_backend_recovery_logs_once_then_resets_retry_backoff(self):
        effects=[URLError(ConnectionRefusedError(errno.ECONNREFUSED,'private')),
                 URLError(TimeoutError('private')),io.BytesIO(b'{}'),io.BytesIO(b'{}'),
                 URLError(ConnectionResetError(errno.ECONNRESET,'private'))]
        output,wait=self.run_api_attempts('claim',effects)
        self.assertEqual(output.count('Worker retry:'),3)
        self.assertEqual(output.count('Worker backend recovered:'),1)
        self.assertIn('retry_elapsed_ms=',output)
        for call,lower in zip(wait.call_args_list,(2,4,2)):
            self.assertGreaterEqual(call.args[0],lower)
            self.assertLess(call.args[0],lower+1)

    def test_backend_diagnostics_preserve_original_exception_and_timeout(self):
        error=URLError(TimeoutError('private'))
        self.worker.local=Mock()
        self.worker.local.open.side_effect=error
        with self.assertRaises(URLError) as caught:
            self.worker.api('result',{})
        self.assertIs(caught.exception,error)
        self.assertEqual(self.worker.local.open.call_args.kwargs['timeout'],15)

    def test_expired_result_does_not_report_outage_or_keep_pending(self):
        self.worker.pending.write_text(json.dumps({'observation_id':'stable'}))
        self.worker.local=Mock()
        self.worker.local.open.side_effect=HTTPError('http://private',409,'private',{},None)
        # A handled expired lease is a completed attempt, with no retry diagnostics.
        output=io.StringIO()
        with redirect_stdout(output):
            self.worker.flush()
        self.assertIn('Result lease expired',output.getvalue())
        self.assertNotIn('Worker retry:',output.getvalue())
        self.assertIsNone(self.worker._backend_retry_started)
        self.assertFalse(self.worker.pending.exists())


if __name__=='__main__': unittest.main()
