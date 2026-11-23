import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ctn_data.db import connect, initialize
from ctn_data.queries import request_traffic


class RequestTrafficTests(unittest.TestCase):
    def test_counts_http_across_workers_not_schedule_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'traffic.sqlite3'
            initialize(path)
            conn=connect(path)
            try:
                with conn:
                    for stamp,source,status in [
                        ('2026-10-19T12:56:00Z','social',200),
                        ('2026-10-19T12:57:00Z','proxy:docker-au-a',200),
                        ('2026-10-19T12:58:00Z','proxy:docker-au-b',429),
                        ('2026-10-19T12:59:00Z','social',404),
                        ('2026-10-19T12:55:00Z','news',200),
                        ('2026-10-19T12:54:59Z','social',200),
                        ('2026-10-19T12:49:59Z','social',429),
                        ('2026-10-19T13:01:00Z','social',429),
                    ]:
                        conn.execute('INSERT INTO api_call_logs(occurred_at,service,method,path,status) VALUES(?,?,\'GET\',\'/api/posts\',?)',(stamp,source,status))
                    conn.execute("INSERT INTO collection_runs(started_at,status,metrics_json) VALUES('2026-10-19T12:59:59Z','completed','{\"requests\":0}')")
                result=request_traffic(conn,now=datetime(2026,10,19,13,tzinfo=timezone.utc))
                self.assertEqual(result['current']['requests'],5)
                self.assertEqual(result['current']['success'],3)
                self.assertEqual(result['current']['rate_limited'],1)
                self.assertEqual(result['current']['failed'],2)
                self.assertEqual(result['current']['per_minute'],1)
                self.assertEqual(result['previous']['requests'],1)
                self.assertEqual(sum(r['requests'] for r in result['by_source']),5)
            finally:
                conn.close()


if __name__=='__main__':unittest.main()
