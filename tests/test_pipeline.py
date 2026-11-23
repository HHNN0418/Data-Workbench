from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ctn_data import annotations, pipeline
from ctn_data.db import connect, initialize
from ctn_data.imports import add_score_snapshot
from ctn_data.server import create_app
from tests.auth_helpers import login


def iso(dt):
    return dt.isoformat(timespec='microseconds').replace('+00:00', 'Z')


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.path)
        self.seed()

    def tearDown(self):
        self.temp.cleanup()

    def seed(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        recent = iso(self.now - timedelta(minutes=20))
        with closing(connect(self.path)) as conn, conn:
            for post_id, author, body in ((1, 1, 'Not sure about the vaccine'), (2, 2, 'Vote yes'), (3, 1, 'I am voting no')):
                conn.execute('INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind) VALUES(?,?,?,?,?,?,?)',
                             (post_id, author, body, recent, recent, recent, 'post'))
            conn.execute("INSERT INTO feed_observations(post_id,source,observed_at) VALUES(1,'latest',?)",
                         (iso(self.now - timedelta(minutes=2)),))
            conn.execute('INSERT INTO counter_snapshots(post_id,observed_at,like_count) VALUES(1,?,1)',
                         (iso(self.now - timedelta(minutes=3)),))
            for index, action in enumerate(['reply'] * 9 + ['post']):
                conn.execute('''INSERT INTO post_attribution(post_id,agent_id,published_at,action,fact_ids_json,captured_at)
                    VALUES(?,?,?,?,?,?)''', (100 + index, 'a1', iso(self.now - timedelta(minutes=index)), action, '[]', iso(self.now)))
            for kind, minutes in (('generation_completed', 10), ('published', 10), ('generation_failed', 30)):
                conn.execute("INSERT INTO agent_audit_events(id,agent_id,at,kind,message,details_json) VALUES(?,'a1',?,?,?,'{}')",
                             ('e-' + kind, iso(self.now - timedelta(minutes=minutes)), kind, 'boom' if kind == 'generation_failed' else kind))
            conn.execute('''INSERT INTO llm_calls(id,started_at,finished_at,purpose,status,duration_ms)
                VALUES('c1',?,?,'decide','ok',1200)''', (recent, recent))
            conn.execute('''INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,track,points_decimal,parse_status)
                VALUES(?,'SCORE_NARRATIVE',?,'f1','red','43','parsed')''',
                (recent, '+43 points: NPC engagement: 11 views, 2 likes, 1 reply and 1 contribution to a change of mind towards your side'))
            # Our post 10 gets replies from a citizen (30), a sequential-name account (31) and a bot (2).
            conn.execute("INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(5554,'Agent Zero',1,'t',?)",
                         (recent,))
            conn.executemany('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             [(5554, 'maya', recent, recent), (30, 'kai_beach', recent, recent),
                              (31, 'swan_150_012', recent, recent), (2, 'botty', recent, recent)])
            for post_id, author, reply_to in ((10, 5554, None), (11, 30, 10), (12, 31, 10), (13, 2, 10)):
                conn.execute('''INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind,reply_to_post_id)
                    VALUES(?,?,?,?,?,?,?,?)''', (post_id, author, 'reply text', recent, recent, recent,
                                                'reply' if reply_to else 'post', reply_to))
            briefs = {'briefs': [{'kind': 'concern_radar', 'generated_at': iso(self.now - timedelta(hours=1))}]}
            conn.execute("INSERT INTO checkpoints(key,value,updated_at) VALUES('agent_content_research_v1',?,?)",
                         (json.dumps(briefs), iso(self.now)))
        add_score_snapshot(self.path, '4000', iso(self.now - timedelta(hours=12)))
        add_score_snapshot(self.path, '4120', iso(self.now - timedelta(minutes=1)))
        annotations.refresh_posts(self.path)
        with patch('ctn_data.agent_targeting.behavior_bots', return_value=frozenset({2})), \
                patch('ctn_data.agent_targeting.duplicate_authors', return_value=frozenset()):
            annotations.refresh_accounts(self.path)

    def stages(self, **kwargs):
        result = pipeline.build(self.path, now=iso(self.now), **kwargs)
        return result, {stage['key']: stage for stage in result['stages']}

    def test_six_stages_with_statuses_and_metrics(self):
        nav = SimpleNamespace(get=lambda: {'agents': {'team_enabled': True, 'pending_actions': 2, 'inbox_unresolved': 5},
                                           'facts': {'extraction_enabled': False}})
        result, stages = self.stages(nav=nav)
        self.assertEqual([stage['key'] for stage in result['stages']], list(pipeline.STAGES))
        for stage in result['stages']:
            self.assertIn(stage['status'], ('ok', 'warn', 'bad'), stage)
            self.assertTrue(stage['headline'])
        collect = stages['collect']
        self.assertEqual((collect['status'], collect['metrics']['posts_last_hour']), ('ok', 7))
        self.assertEqual(stages['process']['metrics']['annotation_backlog'], 0)
        self.assertEqual(stages['process']['metrics']['bot_accounts'], 1)
        analyze = stages['analyze']['metrics']
        self.assertEqual((analyze['undecided_posts_6h'], analyze['citizen_posts_6h']), (1, 4))  # authors 2 (bot) and 5554 (ours) are left out
        self.assertEqual(analyze['radar_age_hours'], 1.0)
        strategy = stages['strategy']
        self.assertEqual((strategy['metrics']['post_share'], strategy['status']), (0.1, 'warn'))
        execute = stages['execute']['metrics']
        self.assertEqual((execute['published_last_hour'], execute['failure_rate_6h'], execute['pending_actions']), (1, 0.5, 2))
        self.assertEqual(execute['step_median_ms'], {'decide': 1200})
        self.assertEqual((execute['posts_6h'], execute['replies_6h'], execute['post_reply_ratio_6h']), (1, 9, 0.11))
        # The 2:1 rule was cancelled (D52): 1:9 is shown but not judged; a 50% failure rate is a warning.
        self.assertEqual(stages['execute']['status'], 'warn')
        outcome = stages['outcome']
        self.assertEqual((outcome['metrics']['changes_towards_6h'], outcome['metrics']['views_per_hour_6h']), (1, 1.8))
        self.assertEqual(outcome['status'], 'bad')  # 10 points/hour cannot reach 7,000 in time
        metrics = outcome['metrics']
        # Shared rule (agent_targeting.citizen_accounts): the bot is excluded; a lone
        # numbered name is not a batch (that needs >= 5 such accounts), so it counts.
        self.assertEqual((metrics['citizen_replies_6h'], metrics['replies_to_us_6h']), (2, 3))
        self.assertEqual((metrics['towards_share_6h'], metrics['changes_towards_6h']), (1.0, 1))
        today = pipeline._parse(iso(self.now - timedelta(minutes=20))) >= pipeline._sydney_day_start(self.now)
        self.assertEqual(metrics['views_today'], 11 if today else 0)
        self.assertEqual(metrics['view_cap'], 500)
        self.assertEqual(result['overall'], 'bad')

    def test_execute_alerts_on_invalid_model_output_and_missing_originals(self):
        # 23 Oct 12:00-14:19: half the decisions came back as invalid JSON and originals stopped.
        with closing(connect(self.path)) as conn, conn:
            rows = [('failed', 'generation_failed', '生成任务失败：AI 返回的分段结果' + pipeline.INVALID_OUTPUT_MARK + '；未创建发布动作', '{}')] * 12
            rows += [('done', 'generation_completed', 'ok', '{}')] * 8
            rows += [('post', 'published', 'published', '{"action":"post"}')] * 3
            for index, (name, kind, message, details) in enumerate(rows):
                conn.execute("INSERT INTO agent_audit_events(id,agent_id,at,kind,message,details_json) VALUES(?,'a1',?,?,?,?)",
                             (f'x-{name}-{index}', iso(self.now - timedelta(minutes=5 + index % 15)), kind, message, details))
        _, stages = self.stages()
        execute = stages['execute']
        metrics = execute['metrics']
        self.assertEqual((metrics['invalid_output_30m'], metrics['decisions_30m'], metrics['invalid_output_share_30m']),
                         (12, 22, 0.545))
        self.assertEqual((metrics['posts_last_hour'], metrics['attempts_last_hour']), (3, 22))
        self.assertEqual(execute['status'], 'bad')
        self.assertEqual(len(metrics['alerts']), 2)
        self.assertTrue(execute['headline'].startswith('⚠ 近 30 分钟 12/22 次决策的模型输出不是有效 JSON'))
        self.assertIn('⚠ 近 1 小时尝试 22 次，只发了 3 条原创帖', execute['headline'])

    def test_execute_alerts_stay_quiet_with_few_decisions(self):
        _, stages = self.stages()
        metrics = stages['execute']['metrics']
        # Two decisions in 30 minutes are not judged; a quiet hour is not "missing originals".
        self.assertIsNone(metrics['invalid_output_share_30m'])
        self.assertEqual(metrics['alerts'], [])
        self.assertFalse(stages['execute']['headline'].startswith('⚠'))

    def test_failures_and_staleness(self):
        with closing(connect(self.path)) as conn, conn:
            conn.execute('DELETE FROM feed_observations')
            conn.execute("UPDATE post_attribution SET action='reply'")
        with patch('ctn_data.pipeline._analyze', side_effect=RuntimeError('down')):
            result, stages = self.stages()
        self.assertEqual(stages['collect']['status'], 'bad')
        self.assertEqual(stages['strategy']['status'], 'bad')
        self.assertEqual((stages['analyze']['status'], stages['analyze']['error']), ('unknown', 'RuntimeError'))
        self.assertEqual(result['overall'], 'bad')

    def test_outcome_counts_views_by_sydney_day_and_change_shares(self):
        # 03:00 UTC on 22 Oct 2026 is 14:00 AEDT, so the Sydney day began at 13:00 UTC on the 21st.
        now = '2026-10-22T03:00:00.000000Z'
        rows = (('2026-10-22T02:00:00.000000Z', 'red', '40 views, 1 like and 1 contribution to a change of mind towards your side'),
                ('2026-10-21T14:00:00.000000Z', 'red', '30 views and 1 contribution to a change of mind away from your side'),
                ('2026-10-21T12:00:00.000000Z', 'red', '200 views and 2 contributions to changes of mind towards neither side'),
                ('2026-10-20T12:00:00.000000Z', 'red', '100 views and 5 contributions to changes of mind towards your side'),
                ('2026-10-22T02:30:00.000000Z', 'blue', '90 views'))
        with closing(connect(self.path)) as conn, conn:
            conn.execute('DELETE FROM official_activity')
            for index, (at, track, message) in enumerate(rows):
                conn.execute('''INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,track,points_decimal,parse_status)
                    VALUES(?,'SCORE_NARRATIVE',?,?,?,'1','parsed')''', (at, '+1 points: NPC engagement: ' + message, f'sydney-{index}', track))
        outcome = {stage['key']: stage for stage in pipeline.build(self.path, now=now)['stages']}['outcome']
        self.assertNotEqual(outcome['status'], 'unknown', outcome)
        metrics = outcome['metrics']
        # Today (Sydney) holds the 02:00 and 14:00 UTC rows; 12:00 UTC was 23:00 the day before; Blue never counts.
        self.assertEqual((metrics['views_today'], metrics['view_cap'], metrics['view_cap_used']), (70, 500, 0.14))
        self.assertEqual((metrics['changes_towards_6h'], metrics['changes_away_6h'], metrics['towards_share_6h']), (1, 0, 1.0))
        self.assertEqual(metrics['towards_share_24h'], 0.25)  # 1 towards of 4 changes; the 39 h old row is outside
        self.assertEqual(metrics['views_per_hour_6h'], 6.7)

    def test_citizen_replies_exclude_heavy_ring_and_batch_named_accounts(self):
        from ctn_data import agent_targeting
        with agent_targeting._cache_lock:
            agent_targeting._cache.clear()  # the PG run reuses one DSN across tests
        recent = iso(self.now - timedelta(minutes=15))
        rows, next_id = [], iter(range(1000, 2000))

        def add(author, kind='post', reply=None, repost=None):
            rows.append((next(next_id), author, 'text', recent, recent, recent, kind, reply, repost))
        for _ in range(24):
            add(40)                   # 25 actions in all: over the 24-action citizen limit
        add(40, 'reply', 10)
        for _ in range(4):
            add(41, 'repost', None, 1)  # 8 actions, half of them reposts: a repost ring
        for _ in range(3):
            add(41)
        add(41, 'reply', 10)
        batch = list(range(3001, 3006))  # five batch-numbered names created during the competition
        for author in batch:
            add(author, 'reply', 10)
        with closing(connect(self.path)) as conn, conn:
            conn.executemany('''INSERT INTO posts(post_id,author_id,body,created_at,first_seen,last_seen,kind,
                reply_to_post_id,repost_of_post_id) VALUES(?,?,?,?,?,?,?,?,?)''', rows)
            conn.executemany('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                             [(40, 'heavy_poster', recent, recent), (41, 'ring_booster', recent, recent)]
                             + [(author, f'heron_7_{author - 3000:02d}', recent, recent) for author in batch])
        metrics = self.stages()[1]['outcome']['metrics']
        # Still only 30 and the lone numbered name 31; the bot and the seven new authors are left out.
        self.assertEqual((metrics['citizen_replies_6h'], metrics['replies_to_us_6h']), (2, 10))

    def test_citizen_replies_are_unknown_when_the_citizen_check_fails(self):
        from ctn_data import agent_targeting
        with agent_targeting._cache_lock:
            agent_targeting._cache.clear()
        with patch('ctn_data.agent_targeting.style_score', side_effect=RuntimeError('check failed')), \
                self.assertLogs('ctn_data.pipeline', 'WARNING'):
            outcome = self.stages()[1]['outcome']
        # Unknown, never "all 3 replies came from citizens"; the total is still known.
        self.assertEqual((outcome['metrics']['citizen_replies_6h'], outcome['metrics']['replies_to_us_6h']), (None, 3))
        self.assertIn('6 小时市民回复 — 条', outcome['headline'])

    def test_post_reply_ratio(self):
        self.assertIsNone(pipeline.post_reply_ratio(0, 0))
        self.assertEqual(pipeline.post_reply_ratio(3, 0), 3.0)
        self.assertEqual(pipeline.post_reply_ratio(4, 2), 2.0)
        self.assertEqual(pipeline.post_reply_ratio(1, 9), 0.11)

    def test_levels(self):
        self.assertEqual(pipeline._level(None, (1, 2)), 'ok')
        self.assertEqual(pipeline._level(1.5, (1, 2)), 'warn')
        self.assertEqual(pipeline._level(3, (1, 2)), 'bad')
        self.assertEqual(pipeline._level(0.05, pipeline.POST_SHARE, lower_is_worse=True), 'bad')
        self.assertEqual(pipeline._level(0.5, pipeline.POST_SHARE, lower_is_worse=True), 'ok')


class CacheTests(unittest.TestCase):
    def test_result_is_shared_for_sixty_seconds(self):
        calls = []
        with patch('ctn_data.pipeline.build', side_effect=lambda *a, **k: calls.append(1) or {'n': len(calls)}):
            status = pipeline.PipelineStatus('unused')
            self.assertEqual((status.get(), status.get()), ({'n': 1}, {'n': 1}))
            status._cached = (status._cached[0] - pipeline.CACHE_SECONDS - 1, status._cached[1])
            self.assertEqual(status.get(), {'n': 2})
        self.assertEqual(len(calls), 2)


class EndpointTests(unittest.TestCase):
    def test_endpoint_returns_six_stages_on_empty_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / 'test.sqlite3'
            initialize(db)
            web = Path(temp) / 'dist'
            web.mkdir()
            (web / 'index.html').write_text('<html></html>')
            client = TestClient(create_app(db, backup_dir=Path(temp) / 'backups', web_root=web), base_url='http://127.0.0.1:8765')
            login(client)
            response = client.get('/api/pipeline/status')
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(len(data['stages']), 6)
            self.assertNotIn('unknown', [stage['status'] for stage in data['stages']], data)
            # Missing values read as a dash or a sentence, never Python's None.
            self.assertFalse([stage['headline'] for stage in data['stages'] if 'None' in stage['headline']])


if __name__ == '__main__':
    unittest.main()
