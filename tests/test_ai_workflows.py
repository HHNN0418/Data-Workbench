import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ctn_data.ai_workflows import reporting_evidence
from ctn_data.db import connect, initialize


AT = '2026-10-19T07:00:00Z'


def account(identifier, score, ids):
    return {'account_id': identifier, 'username': f'user{identifier}', 'score': score,
        'risk_priority_score': score, 'known_post_count': 10,
        'data_observed': {'sufficient': score is not None, 'posts': 10},
        'account_report_eligibility': {'status': 'unknown', 'official_action_count': None},
        'signals': [{'key': 'coordination', 'title': '同步', 'post_ids': ids[:1],
            'measurements': {'peer_post_ids': ids[1:], 'peer_account_ids': [2], 'seconds': 120}}],
        'examples': []}


class ReportingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'report.sqlite3'
        initialize(self.db)
        with closing(connect(self.db)) as conn, conn:
            for identifier, author, visibility, created in [(1, 1, 'public', AT), (2, 2, 'public', AT),
                    (3, 3, 'private', AT), (4, 4, 'public', AT), (5, 5, 'public', '2026-10-18T07:00:00Z')]:
                conn.execute('INSERT INTO posts(post_id,author_id,body,kind,visibility,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)',
                    (identifier, author, '同一段可核对的公开文本', 'post', visibility, created, AT, AT))
            conn.execute('INSERT INTO team_accounts VALUES(?,?,?,?,?)', (4, 'own', 1, 'manual', AT))

    def tearDown(self):
        self.temp.cleanup()

    def test_witnesses_keep_peer_duplicates_and_filter_private_own_and_dates(self):
        screening = {'items': [account(1, 80, [1, 2, 3, 4, 5]), account(2, 0, [2]),
            account(9, None, [1])], 'summary': {'source_post_count': 5}, 'caveats': []}
        with patch('ctn_data.ai_workflows.build_screening', return_value=screening) as scan:
            evidence = reporting_evidence(self.db, {'own': 'own', 'author_id': '4', 'since': AT})
        self.assertEqual(scan.call_args.args[1], {'since': AT})
        self.assertEqual(scan.call_args.kwargs, {'limit': 200, 'min_score': 0, 'include_diagnostics': True})
        sources = evidence['sources']
        self.assertEqual({x['id'] for x in sources}, {'post:1', 'post:2'})
        self.assertEqual(sources[0]['text'], sources[1]['text'])
        accounts = evidence['reporting_context']['accounts']
        self.assertEqual({x['risk_priority_score'] for x in accounts}, {80, 0, None})
        high = next(x for x in accounts if x['account_id'] == 1)
        self.assertEqual(set(high['unavailable_source_ids']), {'post:3', 'post:4', 'post:5'})
        self.assertEqual(high['account_report_eligibility']['status'], 'unknown')
        self.assertEqual(high['signals'][0]['measurements']['seconds'], 120)

    def test_empty_screening_returns_no_invented_sources(self):
        with patch('ctn_data.ai_workflows.build_screening', return_value={'items': [], 'summary': {}, 'caveats': []}):
            evidence = reporting_evidence(self.db)
        self.assertEqual(evidence['sources'], [])
        self.assertEqual(evidence['reporting_context']['accounts'], [])
        self.assertFalse(evidence['reporting_context']['eligibility']['official_counts_available'])
