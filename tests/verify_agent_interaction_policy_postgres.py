"""Policy smoke checks in a verified, disposable PostgreSQL database.

Run ``python -m tests.verify_agent_interaction_policy_postgres``. The configured
connection is used only to create/drop a fresh test database. Application state
is written only after verifying that database, with every model/read mocked.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, urlunsplit
import uuid

import psycopg
from psycopg import sql

from ctn_data import agent_interaction_policy as policy, agent_notifications, agent_relationships
from ctn_data.agent_control import AgentController
from ctn_data.db import connect, initialize, utc_now


PREFIX = 'ctn_agent_interaction_test_'


def run(target, database_name, report):
    assert re.fullmatch(PREFIX + r'[0-9a-f]{32}', database_name), 'unexpected application database name'
    with closing(connect(target)) as conn:
        assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'application database mismatch'
    initialize(target)
    checks = report['checks']
    sdk = Mock(side_effect=AssertionError('real social SDK access is forbidden in this smoke check'))
    provider = {'id': 'fixture-policy', 'protocol': 'openai_chat', 'model': 'fixture-model', 'max_output_tokens': 1800}
    now = time.time()
    controllers = []
    model_calls = 0
    read_calls = 0
    with tempfile.TemporaryDirectory(prefix='ctn-interaction-policy-') as directory:
        def controller():
            value = AgentController(target, Path(directory) / ('keys-' + str(len(controllers))),
                provider_store=SimpleNamespace(get=lambda _: provider), sdk_factory=sdk, clock=lambda: now)
            controllers.append(value)
            original_change = value.store.change
            def change(callback):
                def verified(data, conn):
                    assert conn.execute('SELECT current_database()').fetchone()[0] == database_name, 'write escaped the test database'
                    conn.execute("SET LOCAL statement_timeout='5000ms'")
                    conn.execute("SET LOCAL lock_timeout='4000ms'")
                    return callback(data, conn)
                return original_change(verified)
            value.store.change = change
            return value

        try:
            current = controller()
            assert current.snapshot()['settings']['block_agent_interactions'] is True, 'default policy is not enabled'
            checks['real_postgres_controller_defaults_to_enabled'] = True
            current.settings({'block_agent_interactions': False})
            reopened = controller()
            reopened.settings({'enabled': False})
            assert reopened.snapshot()['settings']['block_agent_interactions'] is False, 'setting did not survive restart/partial save'
            current.settings({'block_agent_interactions': True})
            assert reopened.store.read()['settings']['block_agent_interactions'] is True, 'independent controller did not observe saved policy'
            checks['policy_setting_persists_across_independent_controllers'] = True

            current.save({'name': 'Fixture Writer', 'username': 'fixture_writer', 'provider_id': provider['id']})
            current.save({'name': 'Paused Peer', 'username': 'peer_original', 'enabled': False})
            actors = current.store.read()['agents']
            actor_id = next(aid for aid, row in actors.items() if row['username'] == 'fixture_writer')
            peer_id = next(aid for aid, row in actors.items() if row['username'] == 'peer_original')

            def bind(data, conn):
                data['agents'][actor_id].update(account_id=11, account_state='ready')
                data['agents'][peer_id].update(account_id=22, account_state='ready', enabled=False)
            current.store.change(bind)
            current.store.change(lambda data, conn: data['agents'][peer_id].update(account_id=23, username='peer_rebound'))
            persisted = reopened.store.read()
            registry = persisted['rate']['managed_agent_accounts']
            assert 'peer_original' in registry['22']['usernames'], 'old binding was lost'
            assert 'peer_rebound' in registry['23']['usernames'], 'new binding was not retained'
            assert policy.blocked_account_ids(None, persisted, persisted['agents'][actor_id]) == {22, 23}, 'paused/history membership differs on PostgreSQL'
            checks['paused_and_rebound_identities_and_aliases_are_durable'] = True

            stamp = utc_now()
            def seed(data, conn):
                for account_id, username in ((11, 'fixture_writer'), (22, 'peer_renamed_old'),
                                             (23, 'peer_latest'), (99, 'human_teammate')):
                    conn.execute('INSERT INTO accounts(account_id,username,first_seen,last_seen) VALUES(?,?,?,?)',
                                 (account_id, username, stamp, stamp))
                conn.execute('INSERT INTO team_accounts(account_id,team_label,is_own,evidence,updated_at) VALUES(?,?,?,?,?)',
                             (99, 'fixture-team', 1, 'known human teammate', stamp))
                for post_id, author_id in ((700, 22), (701, 99)):
                    conn.execute('INSERT INTO posts(post_id,author_id,kind,body,visibility,created_at,first_seen,last_seen) '
                                 'VALUES(?,?,?,?,?,?,?,?)',
                                 (post_id, author_id, 'post', 'Isolated public discussion', 'public', stamp, stamp, stamp))
                for kind, target in (('follow', 22), ('follow', 99), ('like', 700), ('like', 701)):
                    agent_relationships.observe(data, 11, kind, target, True, stamp, 'isolated fixture observation')
            current.store.change(seed)

            data = current.store.read()
            actor = data['agents'][actor_id]
            with closing(connect(target)) as conn:
                blocked = policy.blocked_account_ids(conn, data, actor)
                names = policy.blocked_usernames(conn, data, actor)
                assert blocked == {22, 23} and 99 not in blocked, 'human teammate was incorrectly treated as an agent'
                assert {'peer_original', 'peer_rebound', 'peer_renamed_old', 'peer_latest'} <= names, 'SQL/current aliases were not resolved'
                for name in ('peer_original', 'PEER_LATEST'):
                    try:
                        policy.check_body(conn, data, actor, 'Hello @' + name)
                    except policy.PolicyBlocked:
                        pass
                    else:
                        raise AssertionError('managed mention was accepted')
            checks['sql_username_lookup_preserves_aliases_without_blocking_human_teammates'] = True

            sources = [{'id': 'post:' + str(post_id), 'kind': 'post', 'post_kind': 'post',
                        'post_id': post_id, 'author_id': author_id, 'text': 'Public fixture discussion',
                        'version': 'fixture-v1', 'notification_ids': []} for post_id, author_id in ((700, 22), (701, 99))]

            def decide(action, *, target=None, account_target=None, body=''):
                nonlocal model_calls, read_calls
                before = set(current.store.read()['drafts'])
                output = {'action': action, 'body': body, 'target_post_id': target,
                          'target_account_id': account_target,
                          'source_ids': ['post:701' if target == 701 else 'post:700'],
                          'notification_ids': [], 'reason': 'Isolated evidence-based fixture', 'memory': ''}
                with patch.object(current, '_read_sources', return_value=sources) as mock_read_sources, \
                        patch('ctn_data.agent_budget.model_call', return_value={
                            'text': json.dumps(output), 'model': 'fixture-model', 'usage': {}}) as mock_model, \
                        patch('ctn_data.ai_providers.complete', side_effect=AssertionError('real model access is forbidden')):
                    current.run(actor_id)
                assert mock_read_sources.call_count == 1 and mock_model.call_count == 1, 'fixture bypassed the intended decision path'
                model_calls += mock_model.call_count
                read_calls += mock_read_sources.call_count
                added = set(current.store.read()['drafts']) - before
                assert len(added) == 1, 'decision did not create exactly one durable action'
                return current.store.read()['drafts'][added.pop()]

            for action, fields in (('reply', {'target': 700, 'body': 'A fixture reply'}),
                                   ('like', {'target': 700}), ('repost', {'target': 700}),
                                   ('unlike', {'target': 700}), ('follow', {'account_target': 22}),
                                   ('unfollow', {'account_target': 22}),
                                   ('post', {'body': 'Hello @peer_original'})):
                draft = decide(action, **fields)
                assert draft['state'] == 'skipped' and draft.get('policy_blocked') and draft.get('policy_reason'), 'internal model action was not terminally blocked'
            checks['generated_internal_actions_and_mentions_are_skipped_with_policy_flags'] = True
            external = decide('like', target=701)
            assert external['state'] == 'pending' and not external.get('policy_blocked'), 'external action was blocked'
            checks['generated_external_action_remains_pending_for_review'] = True

            def notifications(data, conn):
                own = data['memory'].setdefault(actor_id, {})
                own['inbox'] = [
                    {'id': 1, 'kind': 'like', 'actor_account_id': 22, 'post_id': 701, 'status': 'unresolved'},
                    {'id': 2, 'kind': 'like', 'actor_account_id': 99, 'post_id': 700, 'status': 'unresolved'},
                    {'id': 3, 'kind': 'reply', 'post_id': 700, 'status': 'unresolved'},
                    {'id': 4, 'kind': 'mention', 'actor_username': 'PEER_LATEST', 'status': 'unresolved'}]
                agent_notifications.apply_interaction_policy(data, actor_id, conn)
            current.store.change(notifications)
            notices = current.store.read()['memory'][actor_id]['inbox']
            assert [row['policy_ignored'] for row in notices] == [True, False, True, True], 'notification actor policy differs on PostgreSQL'
            assert all(row['status'] == 'unresolved' for row in notices), 'local ignoring acknowledged a remote notification'
            checks['internal_notifications_are_ignored_locally_with_sql_actor_fallback'] = True

            data = current.store.read()
            with closing(connect(target)) as conn:
                # Omit source author IDs to exercise the actual PG posts query.
                sql_sources = [{'id': 'post:' + str(post_id), 'kind': 'post', 'post_id': post_id} for post_id in (700, 701)]
                marked, relations = agent_relationships.with_interaction_policy(conn, data, data['agents'][actor_id],
                    sql_sources, agent_relationships.snapshot_for(data, data['agents'][actor_id]))
                evidence = {'action': 'unlike', 'sources': marked, 'relationship_context': relations}
                assert marked[0].get('interaction_blocked') and not marked[1].get('interaction_blocked'), 'SQL author lookup did not annotate the proper post'
                assert agent_relationships.allowed_post_targets(evidence) == {701}, 'internal post leaked through relation targets'
                assert agent_relationships.allowed_account_targets(evidence) == {99}, 'internal account leaked through relation targets'
            checks['postgres_sql_author_lookup_filters_post_and_relationship_targets'] = True

            current.settings({'block_agent_interactions': False})
            old_pending = decide('reply', target=700, body='A pending internal reply from before activation')
            assert old_pending['state'] == 'pending', 'disabled policy did not allow review preparation'
            current.settings({'block_agent_interactions': True})
            retired = reopened.store.read()['drafts'][old_pending['id']]
            assert retired['state'] == 'skipped' and retired.get('policy_blocked'), 'activation did not retire an old internal pending action'
            assert reopened.store.read()['drafts'][external['id']]['state'] == 'pending', 'activation retired an external pending action'
            current.settings({'block_agent_interactions': False})
            assert current.store.read()['drafts'][old_pending['id']]['state'] == 'skipped', 'disabling policy resurrected a retired action'
            checks['activation_retires_old_internal_pending_without_reviving_on_disable'] = True
            sdk.assert_not_called()
            report['external_http_calls'] = 0
            report['real_model_calls'] = 0
            report['mock_model_calls'] = model_calls
            report['mock_read_sources_calls'] = read_calls
            report['checks']['no_real_sdk_or_model_access'] = True
        finally:
            for value in controllers:
                value.stop()


def main(*, run_checks=None, database_prefix=PREFIX,
         default_report='reports/generated/agent-interaction-policy-postgres.json'):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-file', type=Path, default=Path('data/secrets/postgres_url'))
    parser.add_argument('--report', type=Path, default=Path(default_report))
    args = parser.parse_args()
    report = {'checked_at': utc_now(), 'status': 'failed', 'checks': {},
              'isolation': 'random temporary database; no production table writes',
              'temporary_database_removed': False}
    admin, created, failure = None, False, None
    if not re.fullmatch(r'ctn_[a-z_]+_test_', database_prefix) or len(database_prefix) > 31:
        raise AssertionError('unexpected isolated database prefix')
    name = database_prefix + uuid.uuid4().hex
    source_database = None
    started = time.monotonic()
    try:
        from ctn_data.secret_files import read_secret
        source = read_secret(args.source_file).strip()
        parts = urlsplit(source)
        if parts.scheme not in ('postgres', 'postgresql') or not parts.hostname:
            raise ValueError('PostgreSQL source must be a connection URI')
        target = urlunsplit(parts._replace(path='/' + name))
        if target == source or not re.fullmatch(database_prefix + r'[0-9a-f]{32}', name):
            raise AssertionError('temporary database isolation check failed')
        admin = psycopg.connect(source, autocommit=True, connect_timeout=10)
        source_database = admin.execute('SELECT current_database()').fetchone()[0]
        assert source_database != name, 'temporary database matches source'
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created = True
        with psycopg.connect(target, autocommit=True, connect_timeout=10) as isolated:
            actual = isolated.execute('SELECT current_database()').fetchone()[0]
            assert actual == name and actual != source_database, 'target did not resolve to the temporary database'
        report['checks']['target_database_verified_distinct_from_source'] = True
        report['temporary_database'] = name
        (run_checks or run)(target, name, report)
        report['status'] = 'passed'
    except BaseException as exc:
        failure = exc
        report['error_type'] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report['error'] = str(exc)
    finally:
        try:
            if created:
                assert re.fullmatch(database_prefix + r'[0-9a-f]{32}', name) and name != source_database, 'refusing unexpected cleanup target'
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                report['temporary_database_removed'] = True
        except BaseException as exc:
            report['status'] = 'failed'
            report['cleanup_error_type'] = type(exc).__name__
            failure = failure or exc
        finally:
            if admin is not None:
                admin.close()
        report['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
        report['finished_at'] = utc_now()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if failure is not None:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
