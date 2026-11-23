import tempfile
import unittest
from pathlib import Path
from ctn_data.db import initialize, connect
from ctn_data.ingest import save_posts
from ctn_data.queries import posts, post_detail
from ctn_data.relations import enrich_relations
from ctn_data.transport import TransportError

class RelationsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.db=Path(self.temp.name)/'test.db'
  initialize(self.db); self.conn=connect(self.db)
  with self.conn:
   save_posts(self.conn,[dict(post_id=2,kind='repost',body=None,repost_of_post_id=1)],'latest')
 def tearDown(self):
  self.conn.close(); self.temp.cleanup()
 def test_reference_enrichment_preserves_empty_repost(self):
  self.assertEqual(post_detail(self.db,2)['references']['repost_of_post_id']['status'],'pending')
  class Transport:
   def get_json(self,*a,**kw): return dict(post_id=1,kind='post',body='original')
  enrich_relations(self.conn,Transport(),dict(relation_limit=20,reply_parent_limit=0),{'errors':[]})
  item=posts(self.db,{'errors':[]})['items'][0]
  self.assertIsNone(post_detail(self.db,2)['post']['body'])
  self.assertEqual(post_detail(self.db,2)['references']['repost_of_post_id']['post']['body'],'original')
 def test_inaccessible_reference_cools_down(self):
  class Transport:
   calls=0
   def get_json(self,*a,**kw):
    self.calls+=1; raise TransportError('unavailable',status=404)
  transport=Transport()
  for _ in range(2): enrich_relations(self.conn,transport,dict(relation_limit=20,reply_parent_limit=0),{'errors':[]})
  self.assertEqual(transport.calls,1)
  self.assertEqual(post_detail(self.db,2)['references']['repost_of_post_id']['status'],'unavailable')
 def test_reply_pagination_continues(self):
  with self.conn:
   save_posts(self.conn,[dict(post_id=1,kind='post',body='original'),dict(post_id=3,kind='reply',reply_to_post_id=1)],'latest')
  class Transport:
   paths=[]
   def get_json(self,service,path,**kw):
    self.paths.append(path)
    return dict(items=[dict(post_id=4,kind='reply',reply_to_post_id=1)],next_cursor='next' if len(self.paths)==1 else None)
  transport=Transport()
  for _ in range(2): enrich_relations(self.conn,transport,dict(relation_limit=0,reply_parent_limit=1),{'errors':[]})
  self.assertIn('cursor=next',transport.paths[1])
  detail=post_detail(self.db,1)
  self.assertEqual(len(detail['replies']),2)
  self.assertEqual(detail['reply_coverage']['status'],'observed_end')

