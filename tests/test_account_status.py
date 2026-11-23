"""Suspended accounts from the official log: marked on roles, never re-enabled."""
import json
import tempfile
import unittest
from pathlib import Path

from ctn_data import account_status
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import connect, initialize
from ctn_data.npc_calibration import PREVIEW_KEY

NOTICE = ('Moderation: your account @maya_collins (id 5554) has been suspended after 3 of its posts were hidden '
          'as inauthentic. It can no longer log in or post.')


class ParseTests(unittest.TestCase):
    def test_reads_account_and_first_notice(self):
        found = account_status.parse([('2026-10-22T12:49:10Z', NOTICE), ('2026-10-22T13:00:00Z', NOTICE),
                                      ('2026-10-22T12:00:00Z', 'Moderation: your team reported a post as inauthentic')])
        self.assertEqual(list(found), [5554])
        self.assertEqual((found[5554]['at'], found[5554]['username']), ('2026-10-22T12:49:10Z', 'maya_collins'))

    def test_an_unreadable_log_is_none_only_when_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = str(Path(temporary) / 'no-tables.sqlite3')  # no official_activity table: the read fails
            account_status._cache.clear()
            self.addCleanup(account_status._cache.clear)
            self.assertIsNone(account_status.suspended(empty, strict=True))
            self.assertEqual(account_status.suspended(empty), {})  # the cached failure, for ordinary callers
            self.assertIsNone(account_status.suspended(empty, strict=True))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite3'
        initialize(self.db)
        account_status._cache.clear()
        self.addCleanup(account_status._cache.clear)
        self.controller = AgentController(self.db, Path(self.temp.name) / 'secrets')
        self.addCleanup(self.controller.stop)
        self.controller.save({'name': 'Maya', 'username': 'maya_collins'})
        self.agent_id = next(iter(self.controller.store.read()['agents']))
        self.controller.store.change(lambda data, conn: data['agents'][self.agent_id].update(
            account_id=5554, account_state='ready'))

    def notice(self, *, preview=False):
        conn = connect(self.db)
        with conn:
            if preview:
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (PREVIEW_KEY, json.dumps({'rows': [
                    {'occurred_at': '2026-10-22T12:49:10Z', 'event_type': 'MESSAGE_NARRATIVE', 'message': NOTICE}]})))
            else:
                conn.execute('INSERT INTO official_activity(occurred_at,event_type,message,fingerprint,occurrence,'
                             "parse_status) VALUES(?,?,?,?,1,'non_scoring')",
                             ('2026-10-22T12:49:10Z', 'MESSAGE_NARRATIVE', NOTICE, 'suspended-5554'))
        conn.close()
        account_status._cache.clear()

    def test_unsuspended_role_is_unmarked_and_can_be_enabled(self):
        self.assertIsNone(self.controller.snapshot()['agents'][0]['account_suspended'])
        self.controller.save({'id': self.agent_id, 'name': 'Maya', 'enabled': True})
        self.assertTrue(self.controller.store.read()['agents'][self.agent_id]['enabled'])

    def check_suspended(self):
        marked = self.controller.snapshot()['agents'][0]['account_suspended']
        self.assertEqual((marked['at'], marked['username']), ('2026-10-22T12:49:10Z', 'maya_collins'))
        with self.assertRaises(AgentError):
            self.controller.save({'id': self.agent_id, 'name': 'Maya', 'enabled': True})
        # Other edits, including keeping it off, still save.
        self.controller.save({'id': self.agent_id, 'name': 'Maya C', 'enabled': False})

    def test_suspension_in_the_exported_log(self):
        self.notice()
        self.check_suspended()

    def test_suspension_in_the_live_table(self):
        self.notice(preview=True)
        self.check_suspended()


if __name__ == '__main__':
    unittest.main()
