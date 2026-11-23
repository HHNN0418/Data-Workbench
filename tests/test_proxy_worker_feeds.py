import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ctn_data.proxy_worker import (
    ProxyWorker, NoRedirect, validate_task, COUNTER_LIMIT, FEED_LIMIT, RESULT_LIMIT,
)


class FeedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.temp.name)
        self.token=self.root/'token'
        self.token.write_text('a'*48)
        self.worker=ProxyWorker('http://backend','http://vpn:8888','test',self.token,self.root/'state')
        self.task={'task_key':'periodic:latest','lease_token':'lease','lease_until':time.time()+120,
                   'kind':'latest','path':'/api/posts?sort=recent&limit=100'}

    def tearDown(self):
        self.temp.cleanup()

    def remote_payload(self,payload=None,raw=None):
        response=Mock()
        response.headers={}
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.read.return_value=raw if raw is not None else json.dumps(payload,ensure_ascii=False).encode()
        self.worker.remote=Mock()
        self.worker.remote.open.return_value=response
        return response

    def test_allowed_feed_kinds_and_cursor(self):
        for kind in ('latest','latest_head','history','trending','trending_head'):
            sort='trending' if kind.startswith('trending') else 'recent'
            task={**self.task,'kind':kind,'path':f'/api/posts?sort={sort}&limit=1&cursor=abc%2Bdef%3D'}
            self.assertEqual(validate_task(task),(kind,FEED_LIMIT))

    def test_rejects_non_allowlisted_url_and_parameters_before_request(self):
        invalid=[
            'https://social.legitreal.com/api/posts?sort=recent&limit=100',
            '//other.example/api/posts?sort=recent&limit=100',
            '/api/posts/1?sort=recent&limit=100',
            '/api/posts?sort=recent&limit=100&limit=1',
            '/api/posts?sort=recent&limit=100&extra=1',
            '/api/posts?sort=recent&limit=100&cursor=a&cursor=b',
            '/api/posts?sort=recent&limit=0',
            '/api/posts?sort=recent&limit=101',
            '/api/posts?sort=recent&limit=01',
            '/api/posts?sort=trending&limit=100',
            '/api/posts?sort=recent',
            '/api/posts?sort=recent&limit=100#',
            '/api/posts?sort=recent&limit=100&cursor=',
            '/api/posts?sort=recent&limit=100&cursor=%0a',
            '/api/posts?sort=recent&limit=100&cursor=%zz',
            '/api/posts?sort=recent&limit=100&cursor=%ff',
        ]
        self.worker.remote=Mock()
        for path in invalid:
            with self.subTest(path=path),self.assertRaises(ValueError):
                self.worker.sample({**self.task,'path':path})
        self.worker.remote.open.assert_not_called()

    def test_feed_uses_larger_bound_and_keeps_stable_observation_id(self):
        payload={'items':[{'post_id':1,'body':'正文'*40000}],'next_cursor':'next'}
        response=self.remote_payload(payload)
        result=self.worker.sample(self.task)
        self.assertEqual(result['status'],200)
        self.assertEqual(result['payload'],payload)
        self.assertEqual(result['observation_id'],hashlib.sha256(b'periodic:latest:lease').hexdigest())
        response.read.assert_called_once_with(FEED_LIMIT+1)

    def test_counter_still_uses_64k_and_strict_post_identity(self):
        task={'task_key':'counter:1','lease_token':'lease','kind':'counter','post_id':1,'path':'/api/posts/1/counters'}
        response=self.remote_payload({'post_id':1,'like_count':3})
        self.assertEqual(self.worker.sample(task)['status'],200)
        response.read.assert_called_once_with(COUNTER_LIMIT+1)
        with self.assertRaises(ValueError):validate_task({**task,'post_id':True})

    def test_malformed_and_oversized_feed_cannot_be_success(self):
        for payload in ([],{}, {'items':{}}, {'items':[1]}, {'items':[{}]*101},
                        {'items':[],'next_cursor':42}, {'items':[],'next_cursor':False}):
            with self.subTest(payload=str(payload)[:80]):
                self.remote_payload(payload)
                self.assertEqual(self.worker.sample(self.task)['status'],0)
        self.remote_payload(raw=b' '*(FEED_LIMIT+1))
        self.assertEqual(self.worker.sample(self.task)['status'],0)
        self.remote_payload({'items':[{}]*100,'next_cursor':None})
        self.assertEqual(self.worker.sample(self.task)['status'],200)

    def test_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None,None,302,'',{},'https://other.example/')
        self.worker.remote=Mock()
        self.worker.remote.open.side_effect=ValueError('Remote redirects are not allowed')
        self.assertEqual(self.worker.sample(self.task)['status'],0)

    def test_claim_announces_capabilities(self):
        with patch.object(self.worker,'api',return_value={'task':None}) as api,patch.object(self.worker,'wait'):
            self.worker.once()
        api.assert_called_once_with('claim',{'capabilities':['counter','feed']})

    def test_result_encoding_preserves_chinese_without_ascii_expansion(self):
        opener=Mock()
        response=io.BytesIO(b'{"accepted":true}')
        opener.open.return_value=response
        self.worker.local=opener
        self.worker.api('result',{'payload':{'items':[{'body':'中'*1000000}]}})
        encoded=opener.open.call_args.args[0].data
        self.assertLess(len(encoded),RESULT_LIMIT)
        self.assertIn('中'.encode(),encoded)
        self.assertNotIn(b'\\u4e2d',encoded)

    def test_oversized_backend_result_never_sends(self):
        self.worker.local=Mock()
        with self.assertRaises(ValueError):
            self.worker.api('result',{'payload':'x'*RESULT_LIMIT})
        self.worker.local.open.assert_not_called()

    def test_pending_feed_survives_restart_with_original_time_and_body(self):
        payload={'items':[{'post_id':1,'body':'中文正文'}],'next_cursor':'next'}
        self.remote_payload(payload)
        def api(action,body):
            if action=='claim':return {'task':self.task}
            if action=='permit':return {'allowed':True}
            raise OSError('backend unavailable')
        with patch.object(self.worker,'api',side_effect=api):
            with self.assertRaises(OSError):self.worker.once()
        original=json.loads(self.worker.pending.read_text(encoding='utf-8'))
        restarted=ProxyWorker('http://backend','http://vpn:8888','test',self.token,self.root/'state')
        with patch.object(restarted,'api',return_value={'accepted':True}) as replay:
            restarted.flush()
            replay.assert_called_once_with('result',original)
        self.assertEqual(original['payload'],payload)
        self.assertFalse(restarted.pending.exists())


if __name__=='__main__':unittest.main()
