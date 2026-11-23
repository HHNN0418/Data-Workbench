import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from ctn_data.db import connect,initialize,raw_record,utc_now,backup
from fastapi.testclient import TestClient
from ctn_data.server import create_app
from ctn_data.integrations import import_api_logs,import_labels
from tests.auth_helpers import login


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/'test.sqlite3'
        initialize(self.db)
        self.web_root=Path(self.temp.name)/'dist'
        self.web_root.mkdir()
        (self.web_root/'index.html').write_text('<html>CTN frontend</html>')
        (self.web_root/'assets').mkdir()
        (self.web_root/'assets'/'app.js').write_text('console.log("CTN")')
        self.app=create_app(self.db,backup_dir=Path(self.temp.name)/'backups',web_root=self.web_root)
        self.client=TestClient(self.app,base_url='http://127.0.0.1:8765')
        login(self.client)
        self.port=8765

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def request(self,path,method='GET',payload=None,headers=None):
        response=self.client.request(method,path,json=payload,headers=headers or {})
        return response.status_code,response.json() if 'application/json' in response.headers.get('Content-Type','') else response.content

    def token(self):
        return self.request('/api/bootstrap')[1]['csrf_token']

    def test_empty_endpoints_are_real_empty(self):
        for path in ('/api/health','/api/overview','/api/posts','/api/accounts','/api/news','/api/activity','/api/score','/api/analysis','/api/rules','/api/operations'):
            with self.subTest(path=path):
                status,data=self.request(path)
                self.assertEqual(status,200,data)
        self.assertEqual(self.request('/api/posts')[1]['total'],0)
        self.assertEqual(self.request('/api/overview')[1]['counts']['posts'],0)

    def test_overview_keeps_score_summary_without_history(self):
        with closing(connect(self.db)) as connection, connection:
            connection.execute("INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind) VALUES(1,1,'a','2026-10-19T07:00:00Z','2026-10-19T07:00:00Z','2026-10-19T07:00:00Z','post')")
            connection.execute("INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind) VALUES(2,1,'b','2026-10-19T07:00:00Z','2026-10-19T07:00:00Z','2026-10-19T07:00:00Z','post')")
            connection.execute("INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(1,'2026-10-19T07:05:00Z',1)")
        data=self.request('/api/overview')[1]
        self.assertEqual(data['quality']['posts_without_counters'],1)
        self.assertIn('official_total',data['score'])
        self.assertIn('activity',data['score'])
        self.assertNotIn('reconciliation',data['score'])
        self.assertNotIn('snapshots',data['score'])
        self.assertEqual([poll['threshold'] for poll in data['threshold']['polls']][-1:],[7000])

    def test_extension_permit_auth_validation_and_shared_pacing(self):
        path='/api/extension-permit'
        self.assertEqual(self.request(path,'POST',{'path':'/api/posts'})[0],403)
        headers={'X-CTN-Token':self.token()}
        self.assertEqual(self.request(path,'POST',{'path':'/api/auth/login'},headers)[0],400)
        first=self.request(path,'POST',{'path':'/api/posts'},headers)
        self.assertEqual(first[0],200)
        self.assertTrue(first[1]['allowed'])
        self.assertFalse(self.request(path,'POST',{'path':'/api/posts'},headers)[1]['allowed'])

    def test_mutations_require_token_and_same_origin(self):
        self.assertEqual(self.request('/api/team-accounts','POST',{'account_ids':[1],'evidence':'test'})[0],403)
        token=self.token()
        headers={'X-CTN-Token':token,'Origin':'https://untrusted.example'}
        self.assertEqual(self.request('/api/team-accounts','POST',{'account_ids':[1],'evidence':'test'},headers)[0],403)
        headers={'X-CTN-Token':token,'Origin':f'http://127.0.0.1:{self.port}'}
        self.assertEqual(self.request('/api/team-accounts','POST',{'account_ids':[1],'evidence':'team account list'},headers)[0],200)
        self.assertEqual(self.request('/api/team-accounts','POST',{'account_ids':[True],'evidence':'test'},headers)[0],400)

    def test_bad_host_and_path_traversal(self):
        self.assertEqual(self.request('/api/bootstrap',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.request('/api/backups/..%2Fsecret')[0],400)
        self.assertEqual(self.request('/../ctn_data/db.py')[0],404)
        self.assertEqual(self.request('/api/export?table=posts%3BDROP%20TABLE%20posts')[0],400)

    def test_post_search_is_parameterized_and_export_neutralizes_formulas(self):
        connection=connect(self.db)
        try:
            with connection:
                connection.execute('INSERT INTO posts(post_id,author_id,kind,body,created_at,first_seen,last_seen) VALUES(1,3,?,?,?,?,?)',('post','=HYPERLINK("https://x")','2026-10-19T00:00:00Z',utc_now(),utc_now()))
            self.assertEqual(self.request('/api/posts?q=%27%20OR%201%3D1--')[1]['total'],0)
            status,data=self.request('/api/export?table=posts')
            self.assertEqual(status,200)
            self.assertIn(b"'=HYPERLINK",data)
        finally: connection.close()

    def test_backup_is_consistent_and_downloadable(self):
        status,result=self.request('/api/backup','POST',{}, {'X-CTN-Token':self.token()})
        self.assertEqual(status,200,result)
        status,data=self.request(result['url'])
        self.assertEqual(status,200)
        self.assertTrue(data.startswith(b'SQLite format 3'))

    def test_pagination_and_invalid_scenario(self):
        self.assertEqual(self.request('/api/posts?limit=10000')[1]['limit'],200)
        self.assertEqual(self.request('/api/posts?limit=oops')[0],400)
        status,result=self.request('/api/scenario','POST',{'track':'bogus'}, {'X-CTN-Token':self.token()})
        self.assertEqual(status,400,result)

    def test_operations_detects_collector_outside_application(self):
        from ctn_data.collector import collection_lock
        token=self.token()
        self.request('/api/scheduler','POST',{'enabled': True}, {'X-CTN-Token':token})
        with collection_lock(self.db):
            self.assertTrue(self.request('/api/operations')[1]['collector_running'])
            status,result=self.request('/api/collect','POST',{}, {'X-CTN-Token':token})
            self.assertEqual(status,202,result)
            self.assertTrue(result['started'])
            self.assertEqual(result['status'],'queued')
        self.assertFalse(self.request('/api/operations')[1]['collector_running'])

    def test_spa_routes_do_not_mask_missing_api_or_assets(self):
        for path in ('/', '/posts', '/accounts', '/score', '/news', '/analysis', '/ai-settings', '/operations', '/rules'):
            status, data = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn(b'CTN frontend', data)
        self.assertEqual(self.request('/assets/app.js')[0], 200)
        for path in ('/api/missing', '/assets/missing.js', '/unknown', '/assets/..%2F..%2Fsecret'):
            self.assertEqual(self.request(path)[0], 404, path)

    def test_typed_requests_and_openapi_are_available_offline(self):
        schema = self.request('/openapi.json')[1]
        self.assertIn('/api/team-accounts', schema['paths'])
        self.assertIn('TeamAccounts', schema['components']['schemas'])
        docs = self.request('/docs')[1]
        self.assertNotIn(b'<script', docs)
        headers = {'X-CTN-Token': self.token()}
        for payload in ({'page_limit': '50'}, {'collect_news': 'false'}, {'unexpected': True}):
            self.assertEqual(self.request('/api/collect', 'POST', payload, headers)[0], 400)
        self.assertEqual(self.request('/api/team-accounts', 'POST', {'account_ids': [1], 'evidence': '  '}, headers)[0], 400)
        self.assertEqual(self.request('/api/score-snapshots', 'POST', {'total': True}, headers)[0], 400)

    def test_body_limit_media_type_and_security_headers(self):
        token = self.token()
        response = self.client.post('/api/backup', content='{}', headers={'X-CTN-Token': token, 'Content-Length': str(11*1024*1024)})
        self.assertEqual(response.status_code, 413)
        response = self.client.post('/api/backup', content='{}', headers={'X-CTN-Token': token, 'Content-Type': 'text/plain'})
        self.assertEqual(response.status_code, 415)
        response = self.client.post('/api/backup', content='{', headers={'X-CTN-Token': token, 'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 400)
        response = self.client.get('/api/health')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])


class ProvenanceTests(unittest.TestCase):
    def test_raw_dedup_and_telemetry_redaction(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'test.db';initialize(path)
            connection=connect(path)
            try:
                with connection:
                    first=raw_record(connection,'latest','/posts',{'body':'public'})
                    self.assertEqual(first,raw_record(connection,'latest','/posts',{'body':'public'}))
                content=json.dumps({'path':'https://social.legitreal.com/api/posts?token=secret','method':'GET','status':200,'error':'password=abc token=xyz','headers':{'Authorization':'do not store'}})
                self.assertEqual(import_api_logs(path,content)['imported'],1)
                self.assertTrue(import_api_logs(path,content)['duplicate'])
                record=connection.execute('SELECT * FROM api_call_logs').fetchone()
                self.assertNotIn('secret',record['path']);self.assertNotIn('abc',record['error']);self.assertNotIn('xyz',record['error'])
                with connection:
                    connection.execute('INSERT INTO posts(post_id,first_seen,last_seen) VALUES(1,?,?)',(utc_now(),utc_now()))
                labels='post_id,label,value,source,version\n1,topic,transport,manual,v1\n'
                self.assertEqual(import_labels(path,labels)['imported'],1)
                self.assertEqual(import_labels(path,labels)['imported'],0)
                saved=backup(path,Path(temp)/'backup')
                restored=connect(saved)
                try: self.assertEqual(restored.execute('SELECT COUNT(*) FROM posts').fetchone()[0],1)
                finally: restored.close()
            finally: connection.close()


if __name__=='__main__': unittest.main()
