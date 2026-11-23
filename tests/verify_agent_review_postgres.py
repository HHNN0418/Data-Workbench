"""Exercise reviews and portable backups in a verified disposable PostgreSQL DB.

The shared harness reads the configured connection without displaying it, creates
one random test database, verifies its identity, and removes only that database.
All model and social operations are mocked; production tables are never written.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from ctn_data import agent_review as review
from ctn_data.agent_control import AgentController, AgentError
from ctn_data.db import SCHEMA_VERSION, backup, begin_write, connect, initialize, utc_now
from ctn_data.migrate_postgres import _columns, _digest, schema_tables
from tests.verify_agent_interaction_policy_postgres import main as isolated_main


PREFIX = 'ctn_agent_review_test_'


def run(target, database_name, report):
    assert re.fullmatch(PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected review test database'
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'test database mismatch'
    initialize(target)
    checks = report['checks']
    provider = {'id': 'review-fixture', 'name': 'Isolated review model',
        'protocol': 'openai_chat', 'model': 'mock-review', 'enabled': True,
        'has_key': True, 'base_url': 'https://review.example.test'}
    providers = SimpleNamespace(get=lambda selected: dict(provider),
                                list_public=lambda: {'providers': [dict(provider)]})
    sdk = Mock(side_effect=AssertionError('real social operations are forbidden'))
    controllers = []

    with tempfile.TemporaryDirectory(prefix='ctn-review-verify-') as temporary:
        directory = Path(temporary)

        def controller():
            value = AgentController(target, directory / ('keys-' + str(len(controllers))),
                                    provider_store=providers, sdk_factory=sdk)
            controllers.append(value)
            original = value.store.change
            def change(callback):
                def verified(data, conn):
                    assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'write escaped test DB'
                    conn.execute("SET LOCAL statement_timeout='5000ms'")
                    conn.execute("SET LOCAL lock_timeout='4000ms'")
                    return callback(data, conn)
                return original(verified)
            value.store.change = change
            return value

        def denied(function):
            try:
                function()
            except AgentError:
                return
            raise AssertionError('expected review refusal was not enforced')

        def answer(*args, **kwargs):
            # Taking the same state lock from an independent connection proves
            # the model is not called inside the review's state transaction.
            with closing(connect(target)) as conn:
                assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'model test DB mismatch'
                with conn:
                    conn.execute('BEGIN')
                    conn.execute("SET LOCAL lock_timeout='1000ms'")
                    begin_write(conn, 'agent_control')
            values = {rule['id']: 'pass' for rule in review.RULES if rule['id'] != 'U01'}
            return {'text': json.dumps({'language': {'status': 'pass', 'reason': 'English confirmed',
                                                   'evidence': []}, 'checks': values}),
                    'model': 'mock-review', 'usage': {}}

        try:
            current = controller()
            current.save({'name': 'Review fixture', 'username': 'review_fixture'})
            actor_id = next(iter(current.store.read()['agents']))
            current.store.change(lambda data, conn: data['agents'][actor_id].update(account_id=17, account_state='ready'))

            def draft(body='Hello world 🙂', action='post', **extra):
                did = str(uuid.uuid4())
                row = {'id': did, 'agent_id': actor_id, 'account_id': 17,
                    'persona_version': 1, 'persona_revision': 1, 'body': body, 'action': action,
                    'state': 'pending', 'version': 1, 'created_at': utc_now(), 'sources': [], **extra}
                current.store.change(lambda data, conn: data['drafts'].update({did: row}))
                return did

            def gate(did, body=None, remote=None):
                return current.store.change(lambda data, conn: review.check(
                    conn, data, data['drafts'][did], body, target_result=remote))

            with patch('ctn_data.agent_budget.model_call', side_effect=answer) as model, \
                    patch('ctn_data.ai_providers.complete', side_effect=AssertionError('real AI forbidden')) as cloud:
                initial = review.settings(current)
                assert initial['settings']['english_only'] and not initial['settings']['enabled'], 'review defaults differ on PostgreSQL'
                first = draft()
                review.ensure(current, first, 'Hello world 🙂')
                gate(first)
                for body in ('Bonjour tout le monde', 'Hello world https://example.test/photo.png 🙂',
                             r'¯\_(ツ)_/¯ → ∑ √∞'):
                    bypassed = draft(body)
                    review.ensure(current, bypassed, body)
                    gate(bypassed)
                    row = current.store.read()['drafts'][bypassed]
                    assert not row.get('_review_approval') and not row.get('review') and not row.get('requires_review'), 'disabled review persisted approval or hold'
                local_blocked = draft('Hello，中文 🙂')
                denied(lambda: review.ensure(current, local_blocked, 'Hello，中文 🙂'))
                denied(lambda: gate(local_blocked))
                row = current.store.read()['drafts'][local_blocked]
                assert not row.get('review') and not row.get('requires_review'), 'local language failure created AI review hold'
                assert model.call_count == 0, 'disabled review unexpectedly used a model'
                assert not review.settings(current)['records'], 'disabled review persisted a history record'
                checks['disabled_review_has_no_ai_approval_history_or_unknown_language_media_hold'] = True
                checks['disabled_review_keeps_readonly_local_nonenglish_guard'] = True

                saved = review.settings(current, {'enabled': True, 'provider_id': provider['id'],
                    'platform_policy': 'hold', 'expected_revision': initial['settings']['revision']})
                denied(lambda: gate(first))
                denied(lambda: review.settings(current, {'enabled': False, 'provider_id': provider['id'],
                    'platform_policy': 'hold', 'expected_revision': initial['settings']['revision']}))
                checks['configuration_revision_invalidates_old_approval_and_stale_saves'] = True

                second = draft()
                result = review.ensure(current, second, 'Hello world 🙂')
                assert result['status'] == 'pass' and len(result['findings']) == 29, 'full review did not persist every rule'
                gate(second)
                checks['semantic_review_runs_outside_transaction_and_checks_all_rules'] = True

                third = draft('', 'repost', target_post_id=7)
                remote = {'post_id': 7, 'author_id': 19, 'body': 'Hello world'}
                review.ensure(current, third, '', target_result=remote)
                gate(third, '', remote)
                denied(lambda: gate(third, '', {**remote, 'body': 'Hello World'}))
                checks['changed_target_is_refused_at_final_gate'] = True

                blocked = draft('Hello，中文 🙂')
                denied(lambda: review.ensure(current, blocked, 'Hello，中文 🙂'))
                assert current.store.read()['drafts'][blocked]['requires_review'], 'language refusal did not hold draft'
                checks['nonenglish_remains_blocked_with_reviewer_enabled'] = True

                reopened = controller()
                restored_view = review.settings(reopened)
                assert restored_view['settings']['revision'] == saved['settings']['revision'], 'saved review revision was lost'
                assert restored_view['settings']['english_only'], 'mandatory language policy was lost'
                assert len(restored_view['records']) == 3, 'durable review history was lost on reopen'
                checks['independent_controller_observes_settings_and_all_review_records'] = True
                calls_before_disable = model.call_count
                disabled = review.settings(current, {'enabled': False, 'provider_id': 'removed-review-model',
                    'platform_policy': 'hold', 'expected_revision': saved['settings']['revision']})
                review.ensure(current, first, 'Bonjour tout le monde')
                gate(first, 'Bonjour tout le monde')
                assert not current.store.read()['drafts'][blocked].get('requires_review'), 'disabling did not release AI review hold'
                denied(lambda: review.ensure(current, blocked, 'Hello，中文 🙂'))
                assert not current.store.read()['drafts'][blocked].get('requires_review'), 'disabled local guard recreated AI review hold'
                assert len(disabled['records']) == 3 and len(review.settings(current)['records']) == 3, 'disabling added review history'
                assert model.call_count == calls_before_disable, 'disabled review called model'
                assert review.settings(reopened)['settings']['revision'] == disabled['settings']['revision'], 'disabled policy was not durable'
                checks['disabling_releases_only_review_hold_and_skips_models_without_connection'] = True
                legacy = draft('Hello world')
                original_hold = draft('Hello world')
                def old_state(data, conn):
                    data['drafts'][legacy].update(requires_review=True, review_hold_reason='old reviewer hold',
                        review={'rule_version': review.RULE_VERSION, 'status': 'blocked'})
                    data['drafts'][original_hold].update(requires_review=True, review_reason='original manual confirmation')
                current.store.change(old_state)
                migrated = controller().store.read()['drafts']
                assert not migrated[legacy].get('requires_review'), 'disabled legacy AI hold survived reopening'
                assert migrated[original_hold].get('requires_review'), 'disabled reviewer removed original workflow hold'
                assert len(review.settings(current)['records']) == 3, 'legacy migration added reviewer history'
                checks['reopening_releases_legacy_ai_hold_and_preserves_original_workflow_hold'] = True
                assert not cloud.called and not sdk.called, 'unexpected external IO'
                report['mock_model_calls'] = model.call_count

            assert schema_tables().count('agent_reviews') == 1, 'review table missing from portable enumeration'
            portable = backup(target, directory / 'portable')
            with closing(connect(target)) as source, closing(connect(portable)) as snapshot:
                assert source.execute('SELECT current_database()').fetchone()[0] == database_name, 'backup source identity changed'
                assert source.execute('SELECT version FROM _ctn_schema').fetchone()[0] == SCHEMA_VERSION, 'core schema version mismatch'
                columns = _columns(snapshot, 'agent_reviews')
                assert columns == ['id', 'draft_id', 'agent_id', 'created_at', 'input_hash', 'status', 'record_json'], 'portable review columns incomplete'
                assert _digest(source, 'agent_reviews', columns) == _digest(snapshot, 'agent_reviews', columns), 'portable review records differ'
                assert snapshot.execute('SELECT COUNT(*) FROM agent_reviews').fetchone()[0] == 3, 'portable review history incomplete'
                assert snapshot.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'portable integrity check failed'
            checks['real_postgres_to_sqlite_backup_retains_complete_review_columns_and_records'] = True
            report['real_model_calls'] = 0
            report['external_http_calls'] = 0
        finally:
            for value in controllers:
                value.stop()


if __name__ == '__main__':
    isolated_main(run_checks=run, database_prefix=PREFIX,
                  default_report='reports/generated/agent-review-postgres.json')
