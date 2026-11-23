import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from ctn_data.db import initialize,connect
from ctn_data.ingest import save_posts,save_counters
from ctn_data.queries import post_detail,operations
from ctn_data.server import create_app,DEFAULT_CONFIG
from ctn_data.browser_capture import capture
from tests.auth_helpers import login

class CoverageBrowserTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'test.db';initialize(self.db)
  self.conn=connect(self.db)
 def tearDown(self): self.conn.close();self.tmp.cleanup()
 def post(self,pid=1,body='new'):
  return dict(post_id=pid,body=body,visibility='public',kind='post',created_at='2026-10-19T00:00:00Z')
 def test_coverage_and_observed_deltas_do_not_invent_zero_baseline(self):
  with self.conn:
   save_posts(self.conn,[self.post(),self.post(2)],'latest')
   save_counters(self.conn,1,dict(like_count=4,reply_count=0,repost_count=0,view_count=10),'2026-10-19T01:00:00Z')
   save_counters(self.conn,1,dict(like_count=3,reply_count=None,repost_count=0,view_count=20),'2026-10-19T01:10:00Z')
  detail=post_detail(self.db,1)['counters']
  self.assertIsNone(detail[0]['deltas']['like_count'])
  self.assertEqual(detail[1]['deltas']['like_count'],-1)
  self.assertIsNone(detail[1]['deltas']['reply_count'])
  self.assertEqual(detail[1]['elapsed_minutes'],10)
  self.assertTrue(detail[1]['has_negative_delta'])
  coverage=operations(self.db,DEFAULT_CONFIG)['coverage']['counters']
  self.assertEqual((coverage['total'],coverage['measured'],coverage['missing'],coverage['partial'],coverage['repeated']),(2,1,1,1,1))
 def test_browser_retry_dedup_and_old_content_cannot_replace_new(self):
  with self.conn: save_posts(self.conn,[self.post()],'latest','2026-10-19T01:00:00Z')
  r=dict(id='capture-123',kind='post',observed_at='2026-10-19T00:30:00Z',data=self.post(body='old'))
  for _ in range(2):self.assertEqual(capture(self.db,[r])['accepted'],['capture-123'])
  self.assertEqual(post_detail(self.db,1)['post']['body'],'new')
  self.assertEqual(self.conn.execute("select count(*) from feed_observations where source='browser_detail'").fetchone()[0],1)
 def test_extension_origin_pairing_and_route_restriction(self):
  app=create_app(self.db);origin='chrome-extension://'+'a'*32
  with TestClient(app,base_url="http://127.0.0.1:8765") as client:
   login(client)
   self.assertEqual(client.get('/api/bootstrap',headers={'Origin':origin}).status_code,403)
   token=client.get('/api/bootstrap').json()['csrf_token'];headers={'X-CTN-Token':token}
   response=client.post('/api/extension-pair',json={'extension_id':'a'*32,'team_id':46},headers=headers)
   self.assertEqual(response.status_code,200)
   ext={**headers,'Origin':origin}
   self.assertEqual(client.post('/api/browser-capture',json={'records':[]},headers=ext).status_code,200)
   heartbeat={'extension_id':'a'*32,'version':'1.1.0','auto_enabled':True,'queue_count':3,'pages':[{'role':'latest','tab_id':42,'enabled':True,'cookie':'must-not-store'}]}
   self.assertEqual(client.post('/api/extension-status',json=heartbeat,headers=ext).status_code,200)
   status=client.get('/api/extension-status').json()
   self.assertEqual(status['heartbeat']['pages'][0],{'role':'latest','tab_id':42,'enabled':True,'pause_until':None,'bucket':None,'reason':''})
   self.assertEqual(status['heartbeat']['queue_count'],3)
   heartbeat['pages'].append({'role':'latest','tab_id':43})
   self.assertEqual(client.post('/api/extension-status',json=heartbeat,headers=ext).status_code,400)
   self.assertEqual(client.post('/api/extension-status',json=heartbeat,headers={'Origin':origin}).status_code,403)
   self.assertEqual(client.post('/api/score-snapshots',json={'total':999},headers=ext).status_code,403)
   self.assertEqual(client.post('/api/browser-capture',json={'records':[]},headers={**headers,'Origin':'https://evil.test'}).status_code,403)
 def test_portal_scope_dedup_and_access_code_filter(self):
  self.conn.execute("insert into meta(key,value) values('browser_team_id','46')");self.conn.commit()
  record=dict(id='portal-123',kind='portal',observed_at='2026-10-19T01:00:00Z',data=dict(team_id=46,total='10.25',activity_csv='When (UTC),Type,Message\n2026-10-19T00:00:00Z,SCORE,awarded 10.25 points\n2026-10-19T00:01:00Z,TEAMCODE,secret-code\n'))
  for _ in range(2):self.assertFalse(capture(self.db,[record])['errors'])
  self.assertEqual(self.conn.execute('select count(*) from score_snapshots').fetchone()[0],1)
  self.assertEqual(self.conn.execute('select count(*) from official_activity').fetchone()[0],1)
  self.assertEqual(self.conn.execute("select count(*) from raw_records where payload_json like '%secret-code%'").fetchone()[0],0)
  record['id']='portal-456';record['observed_at']='2026-10-19T01:02:00Z'
  self.assertFalse(capture(self.db,[record])['errors'])
  self.assertEqual(self.conn.execute('select count(*) from score_snapshots').fetchone()[0],2)
  self.assertEqual(self.conn.execute('select count(*) from official_activity').fetchone()[0],1)
  with TestClient(create_app(self.db),base_url='http://127.0.0.1:8765') as client:
   login(client)
   status=client.get('/api/extension-status').json()
   self.assertEqual(status['observations']['portal']['count'],2)
   self.assertEqual(status['last_portal']['activity_rows'],1)
   self.assertEqual(status['last_portal']['activity_inserted'],0)
   self.assertEqual(status['last_portal']['activity_duplicates'],1)
  record['id']='portal-789';record['data']['team_id']=47
  self.assertTrue(capture(self.db,[record])['errors'])

 def test_portal_status_and_rate_limit_do_not_pause_public_collectors(self):
  import time
  from ctn_data.coordination import cooldowns
  with TestClient(create_app(self.db),base_url='http://127.0.0.1:8765') as client:
   login(client)
   self.assertFalse(client.get('/api/score').json()['automatic_collection']['enabled'])
   headers={'X-CTN-Token':client.get('/api/bootstrap').json()['csrf_token']}
   client.post('/api/extension-pair',json={'extension_id':'a'*32,'team_id':46},headers=headers)
   heartbeat={'extension_id':'a'*32,'version':'1.3.0','auto_enabled':True,'pages':[{'role':'portal','tab_id':42,'enabled':True}]}
   self.assertEqual(client.post('/api/extension-status',json=heartbeat,headers=headers).status_code,200)
   self.assertFalse(client.get('/api/score').json()['automatic_collection']['enabled'])
   self.assertEqual(client.get('/api/score').json()['automatic_collection']['state'],'starting')
   from ctn_data.db import utc_now
   record=dict(id='portal-live-status',kind='portal',observed_at=utc_now(),data=dict(team_id=46,total='5',activity_csv='When (UTC),Type,Message\n'))
   self.assertFalse(capture(self.db,[record])['errors'])
   self.assertTrue(client.get('/api/score').json()['automatic_collection']['enabled'])
   heartbeat['pages'][0].update(enabled=False,pause_until=int((time.time()+300)*1000),reason='HTTP 429')
   self.assertEqual(client.post('/api/extension-status',json=heartbeat,headers=headers).status_code,200)
   self.assertEqual(client.get('/api/score').json()['automatic_collection']['state'],'cooldown')
   self.assertEqual(cooldowns(self.conn),{'feeds':0,'counters':0})


 def test_recent_baselines_reserved_without_starving_history(self):
  from ctn_data.collector import _counter_schedule,_config
  from ctn_data.db import utc_now
  with self.conn:
   save_posts(self.conn,[self.post(i) for i in range(1,21)],'latest','2026-10-18T00:00:00Z')
   fresh=self.post(100);fresh['created_at']=utc_now()
   save_posts(self.conn,[fresh],'latest')
  selected=_counter_schedule(self.conn,_config(dict(counter_limit=10,counter_new_share=1,counter_recent_share=.25)))
  ids=[r['post_id'] for r in selected]
  self.assertIn(100,ids);self.assertEqual(len(ids),10);self.assertEqual(len(set(ids)),10)
 def test_all_three_capture_paths_and_interaction_history(self):
  self.conn.execute("insert into meta(key,value) values('browser_team_id','46')");self.conn.commit()
  stamp='2026-10-19T01:00:00Z'
  records=[dict(id='post-e2e-001',kind='post',observed_at=stamp,source='browser_latest',data=self.post()),dict(id='counter-e2e-001',kind='counter',observed_at=stamp,data=dict(post_id=1,like_count=2,reply_count=1,repost_count=0,view_count=10)),dict(id='portal-e2e-001',kind='portal',observed_at=stamp,data=dict(team_id=46,total='5',activity_csv='When (UTC),Type,Message\n2026-10-19T00:00:00Z,SCORE,awarded 5 points\n'))]
  with TestClient(create_app(self.db),base_url='http://127.0.0.1:8765') as client:
   login(client)
   headers={'X-CTN-Token':client.get('/api/bootstrap').json()['csrf_token']}
   first=client.post('/api/browser-capture',json={'records':records},headers=headers).json()
   self.assertEqual(first['errors'],[]);self.assertEqual(len(first['accepted']),3)
   self.assertEqual(client.post('/api/browser-capture',json={'records':records},headers=headers).json(),first)
   records[1].update(id='counter-e2e-002',observed_at='2026-10-19T01:02:00Z');records[1]['data']['like_count']=4
   self.assertEqual(client.post('/api/browser-capture',json={'records':[records[1]]},headers=headers).json()['errors'],[])
   status=client.get('/api/extension-status').json()
   self.assertEqual({k:v['count'] for k,v in status['observations'].items()},{'post':1,'counter':2,'portal':1})
   self.assertEqual(status['last_portal']['activity_inserted'],1)
  snapshots=post_detail(self.db,1)['counters']
  self.assertEqual(len(snapshots),2);self.assertEqual(snapshots[1]['deltas']['like_count'],2)
