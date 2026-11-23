"""The staged pipeline step by step: counts, median/p90 durations and failure reasons; read-only."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ctn_data import pipeline_steps
from ctn_data.db import connect, initialize

NOW = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class FakeNav:
    def get(self):
        return {'agents': {'pending_actions': 4, 'uncertain_actions': 1, 'inbox_unresolved': 2, 'roles_running': 3,
                           'roles_enabled': 15, 'team_enabled': True, 'maintenance': {'active': False, 'reason': None}}}


class PipelineStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        self.n = 0
        conn = connect(self.db)
        with conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('agent_control_v1:agents',?)",
                         (json.dumps({'r1': {'name': 'Maya Collins'}, 'r2': {'name': 'Ethan Brooks'}}),))
            for purpose, durations, errors in (('reading', [2000, 4000], 0), ('decide', [10000, 20000, 30000, 40000], 1),
                                               ('write', [15000], 2), ('check', [9000, 11000], 0), ('radar', [5000], 0)):
                for value in durations:
                    self.call(conn, purpose, 'ok', value, 'r1', 30)
                for _ in range(errors):
                    self.call(conn, purpose, 'error', 50000, 'r2', 40, 'output_truncated')
            self.call(conn, 'decide', 'ok', 99999, 'r1', 7 * 60)  # outside the window
            self.event(conn, 'r1', 50, 'reading_selection', '规则选读', details={'selection_status': 'rules:selected'})
            self.event(conn, 'r2', 50, 'reading_selection', '模型选读', details={'selection_status': 'selected'})
            self.event(conn, 'r1', 45, 'generation_completed', '阅读与决策任务已完成', details={'elapsed_seconds': 100})
            self.event(conn, 'r1', 25, 'generation_completed', '阅读与决策任务已完成', details={'elapsed_seconds': 200})
            for detail in ('A reader would not move toward Yes (neutral)', 'A reader would not move toward Yes (neutral)',
                           'It concedes a No argument.'):
                self.event(conn, 'r2', 20, 'generation_failed',
                           '生成任务失败：AI 正文两次未通过方向与事实检查，本轮未创建动作：' + detail, details={'phase': 'complete'})
            self.event(conn, 'r2', 15, 'generation_failed', '生成任务失败：请先保存并选择一个 AI 连接',
                       details={'phase': 'reading'})
            self.event(conn, 'r1', 40, 'draft', '已生成一条待处理动作', draft='d1', details={'action': 'post'})
            self.event(conn, 'r1', 39.5, 'published', '赛事操作执行成功', draft='d1', details={'action': 'post'})
            self.event(conn, 'r1', 30, 'draft', '已生成一条待处理动作', draft='d2', details={'action': 'reply'})
            self.event(conn, 'r1', 22, 'published', '赛事操作执行成功', draft='d2', details={'action': 'reply'})
            # Drafted before the window, published inside it.
            self.event(conn, 'r2', 6 * 60 + 30, 'draft', '已生成一条待处理动作', draft='d3', details={'action': 'reply'})
            self.event(conn, 'r2', 6 * 60 - 10, 'published', '赛事操作执行成功', draft='d3', details={'action': 'reply'})
            self.event(conn, 'r1', 8 * 60, 'published', '赛事操作执行成功', draft='d0', details={'action': 'post'})
        conn.close()

    def call(self, conn, purpose, status, duration, agent, minutes_ago, error=None):
        self.n += 1
        conn.execute('INSERT INTO llm_calls(id,started_at,finished_at,purpose,agent_id,status,error_code,duration_ms) '
                     'VALUES(?,?,?,?,?,?,?,?)',
                     (f'c{self.n}', stamp(minutes_ago), stamp(minutes_ago), purpose, agent, status, error, duration))

    def event(self, conn, agent, minutes_ago, kind, message, draft=None, details=None):
        self.n += 1
        conn.execute('INSERT INTO agent_audit_events(id,agent_id,at,kind,message,draft_id,details_json) VALUES(?,?,?,?,?,?,?)',
                     (f'e{self.n}', agent, stamp(minutes_ago), kind, message, draft, json.dumps(details or {})))

    def test_model_steps_count_calls_with_median_and_p90(self):
        result = pipeline_steps.build(self.db, now=NOW)
        steps = {step['key']: step for step in result['steps']}
        self.assertEqual([step['key'] for step in result['steps']], ['reading', 'decide', 'write', 'check', 'publish'])
        decide = steps['decide']
        self.assertEqual((decide['model_calls'], decide['ok'], decide['errors']), (5, 4, 1))
        self.assertEqual((decide['median_ms'], decide['p90_ms']), (25000, 37000))
        self.assertEqual(decide['top_errors'], [{'reason': 'output_truncated', 'count': 1}])
        self.assertEqual(steps['write']['error_rate'], round(2 / 3, 3))
        reading = steps['reading']
        self.assertEqual((reading['runs'], reading['rules_runs'], reading['model_runs'], reading['model_calls']),
                         (2, 1, 1, 2))
        self.assertEqual(result['other_model_calls'], {'radar': 1})

    def test_publish_time_runs_from_draft_to_published(self):
        publish = next(step for step in pipeline_steps.build(self.db, now=NOW)['steps'] if step['key'] == 'publish')
        self.assertEqual(publish['runs'], 3)
        self.assertEqual(publish['actions'], {'post': 1, 'reply': 2})
        self.assertEqual(publish['by_action']['post'], {'count': 1, 'median_ms': 30000, 'p90_ms': 30000})
        self.assertEqual(publish['by_action']['reply']['count'], 2)
        # 8 and 40 minutes; the second draft was created before the window opened.
        self.assertEqual(publish['by_action']['reply']['median_ms'], 1440000)

    def test_failures_are_grouped_by_reason_with_examples(self):
        result = pipeline_steps.build(self.db, now=NOW, nav=FakeNav())
        totals = result['totals']
        self.assertEqual((totals['completed'], totals['failed'], totals['failure_rate']), (2, 4, round(4 / 6, 3)))
        self.assertEqual(totals['round_seconds'], {'median': 150, 'p90': 190})
        first, second = result['failures']
        self.assertEqual((first['reason'], first['count']), ('AI 正文两次未通过方向与事实检查，本轮未创建动作', 3))
        self.assertEqual(first['examples'][0], {'detail': 'A reader would not move toward Yes (neutral)', 'count': 2})
        self.assertEqual((second['reason'], second['count'], second['examples']), ('请先保存并选择一个 AI 连接', 1, []))
        self.assertEqual(result['failure_phases'], {'complete': 3, 'reading': 1})
        self.assertEqual(result['queue']['pending_actions'], 4)
        self.assertTrue(result['read_only'])

    def test_hourly_and_per_role_counts(self):
        result = pipeline_steps.build(self.db, now=NOW)
        self.assertEqual(sum(row['completed'] for row in result['hourly']), 2)
        self.assertEqual(sum(row['post'] + row['reply'] for row in result['hourly']), 3)
        self.assertEqual(result['hourly'][0]['hour'], '2026-10-22T11:00:00.000000Z')
        roles = {row['agent_id']: row for row in result['roles']}
        self.assertEqual(roles['r1']['name'], 'Maya Collins')
        self.assertEqual((roles['r1']['post'], roles['r1']['reply'], roles['r1']['completed']), (1, 1, 2))
        self.assertEqual((roles['r2']['failed'], roles['r2']['model_errors']), (4, 3))
        self.assertEqual(pipeline_steps.failure_reason('生成任务失败：数据库暂时繁忙'), ('数据库暂时繁忙', ''))


if __name__ == '__main__':
    unittest.main()
