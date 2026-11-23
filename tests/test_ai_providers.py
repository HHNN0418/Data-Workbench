"""Credential isolation, provider selection, and bounded cloud transport."""
import concurrent.futures
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ctn_data import ai_providers as ai
from ctn_data.db import backup, connect


class ProviderStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'data.sqlite3'
        self.store = ai.ProviderStore(self.db, self.root / 'secrets')

    def tearDown(self):
        self.temp.cleanup()

    def save(self, **extra):
        payload = {'name': 'Cloud', 'base_url': 'https://api.openai.com/v1',
                   'model': 'user-selected-model', 'api_key': 'sk-fake-test-credential'}
        payload.update(extra)
        result = self.store.save(payload)
        return result['providers'][-1]

    def test_key_survives_restart_without_entering_database_or_public_response(self):
        provider = self.save()
        self.assertTrue(provider['has_key'])
        self.assertTrue(provider['is_default'])
        self.assertNotIn('api_key', provider)
        restored = ai.ProviderStore(self.db, self.root / 'secrets')
        self.assertEqual(restored.get()['api_key'], 'sk-fake-test-credential')
        public = json.dumps(restored.list_public())
        self.assertNotIn('sk-fake-test-credential', public)
        conn = connect(self.db)
        try:
            meta = conn.execute('SELECT value FROM meta WHERE key=?', (ai.SETTINGS_KEY,)).fetchone()[0]
        finally:
            conn.close()
        self.assertNotIn('sk-fake-test-credential', meta)
        saved = backup(self.db, self.root / 'backups')
        self.assertNotIn(b'sk-fake-test-credential', saved.read_bytes())
        raw = (self.root / 'secrets' / (provider['id'] + '.key')).read_bytes()
        if os.name == 'nt':
            self.assertTrue(raw.startswith(b'DPAPI1\n'))
            self.assertNotIn(b'sk-fake-test-credential', raw)
        else:
            self.assertEqual((self.root / 'secrets' / (provider['id'] + '.key')).stat().st_mode & 0o777, 0o600)

    def test_cloud_retrieval_roles_persist_keys_separately(self):
        for protocol in ('openai_embeddings', 'rerank'):
            provider = self.save(protocol=protocol, base_url='https://example.com/v1/' + ('embeddings' if protocol == 'openai_embeddings' else 'rerank'))
            self.assertEqual(provider['protocol'], protocol)
            self.assertEqual(provider['base_url'], 'https://example.com/v1')
            self.assertNotIn('api_key', provider)
            self.assertEqual(self.store.get(provider['id'])['api_key'], 'sk-fake-test-credential')

    def test_blank_key_preserves_existing_and_update_returns_no_secret(self):
        provider = self.save()
        self.store.save({'id': provider['id'], 'name': 'Renamed', 'api_key': ''})
        self.assertEqual(self.store.get()['api_key'], 'sk-fake-test-credential')
        self.store.save({'id': provider['id'], 'api_key': 'sk-new-fake-credential'})
        self.assertEqual(self.store.get()['api_key'], 'sk-new-fake-credential')
        self.assertEqual(self.store.list_public()['providers'][0]['name'], 'Renamed')

    def test_connection_setup_timeout_is_public_persistent_and_defaults_for_legacy_settings(self):
        provider = self.save()
        self.assertEqual(provider['connection_timeout_seconds'], 60)
        changed = self.store.save({'id': provider['id'], 'connection_timeout_seconds': 90})
        self.assertEqual(changed['providers'][0]['connection_timeout_seconds'], 90)
        restored = ai.ProviderStore(self.db, self.root / 'secrets')
        self.assertEqual(restored.get()['connection_timeout_seconds'], 90)
        conn = connect(self.db)
        try:
            stored = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (ai.SETTINGS_KEY,)).fetchone()[0])
            stored['providers'][0].pop('connection_timeout_seconds')
            conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(stored), ai.SETTINGS_KEY))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(restored.get()['connection_timeout_seconds'], 60)
        self.assertEqual(restored.list_public()['providers'][0]['connection_timeout_seconds'], 60)

    def test_connection_setup_timeout_rejects_unbounded_or_fractional_values(self):
        provider = self.save()
        for value in (True, False, None, '60', 0, 4, 301, 5.5, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '5–300'):
                self.store.save({'id': provider['id'], 'connection_timeout_seconds': value})
        for value in (5, 20, 90.0, 300):
            with self.subTest(value=value):
                self.store.save({'id': provider['id'], 'connection_timeout_seconds': value})
                self.assertEqual(self.store.get()['connection_timeout_seconds'], value)

    def test_openrouter_provider_order_persists_validates_and_routes(self):
        provider = self.save(base_url='https://openrouter.ai/api/v1')
        self.assertEqual(provider['provider_order'], [])
        self.store.save({'id': provider['id'], 'provider_order': ['morph', 'relace']})
        # An edit that does not send the order keeps it.
        self.store.save({'id': provider['id'], 'name': 'Renamed'})
        self.assertEqual(self.store.list_public()['providers'][0]['provider_order'], ['morph', 'relace'])
        self.assertEqual(self.store.get()['provider_order'], ['morph', 'relace'])
        for value in ('morph', ['Morph'], ['morph', 'morph'], [1], ['a b'], [str(n) for n in range(7)]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'OpenRouter'):
                self.store.save({'id': provider['id'], 'provider_order': value})
        other = self.save()
        with self.assertRaisesRegex(ValueError, '只有 OpenRouter'):
            self.store.save({'id': other['id'], 'provider_order': ['morph']})
        self.store.save({'id': provider['id'], 'provider_order': []})
        self.assertEqual(self.store.list_public()['providers'][0]['provider_order'], [])

        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
                  'additionalProperties': False}
        response = {'choices': [{'message': {'content': '{"ok":true}'}}]}
        for base_url, supplied, expected in (
                ('https://openrouter.ai/api/v1', schema,
                 {'require_parameters': True, 'order': ['morph', 'relace'], 'allow_fallbacks': False}),
                ('https://openrouter.ai/api/v1', None, {'order': ['morph', 'relace'], 'allow_fallbacks': False}),
                ('https://example.com/v1', schema, None)):
            with self.subTest(base_url=base_url, structured=supplied is not None), \
                    patch.object(ai, '_request_json', return_value=response) as request:
                ai.complete({'base_url': base_url, 'model': 'm', 'protocol': 'openai_chat', 'api_key': 'fake-key',
                             'provider_order': ['morph', 'relace']},
                            [{'role': 'user', 'content': 'Return JSON.'}], response_schema=supplied)
                self.assertEqual(json.loads(request.call_args.args[1]).get('provider'), expected)
    def test_select_default_disable_and_delete(self):
        first = self.save()
        second = self.save(name='Second', base_url='https://example.com/v1')
        self.store.save({'default_provider_id': second['id']})
        self.assertEqual(self.store.get()['id'], second['id'])
        self.store.save({'id': second['id'], 'enabled': False})
        self.assertEqual(self.store.get()['id'], first['id'])
        with self.assertRaises(ai.ProviderError):
            self.store.get(second['id'])
        self.store.delete(first['id'])
        self.assertFalse((self.root / 'secrets' / (first['id'] + '.key')).exists())
        self.assertEqual(self.store.list_public()['default_provider_id'], '')
        with self.assertRaises(ai.ProviderError):
            self.store.get()

    def test_missing_key_is_configurable_but_not_callable_except_localhost(self):
        self.save(api_key='')
        with self.assertRaisesRegex(ai.ProviderError, 'API Key'):
            self.store.get()
        local = self.save(name='Local', api_key='', base_url='http://127.0.0.1:11434/v1')
        self.assertEqual(self.store.get(local['id'])['api_key'], '')

    def test_server_environment_key_is_never_saved(self):
        provider = self.save(api_key='')
        name = 'CTN_AI_KEY_' + provider['id'].replace('-', '').upper()
        with patch.dict(os.environ, {name: 'environment-only-fake-key'}):
            self.assertTrue(self.store.list_public()['providers'][0]['has_key'])
            self.assertEqual(self.store.get()['api_key'], 'environment-only-fake-key')
        self.assertFalse((self.root / 'secrets').exists())

    def test_invalid_settings_are_rejected_without_mutation(self):
        self.save()
        before = self.store.list_public()
        for extra in ({'model': ''}, {'enabled': 'true'}, {'protocol': 'unknown'},
                      {'api_key': 'key\nheader'}, {'is_default': True, 'enabled': False},
                      {'id': '../../outside'}, {'token_parameter': 'tools'}, {'thinking_mode': 'unknown'}):
            with self.subTest(extra=extra), self.assertRaises((ValueError, ai.ProviderError)):
                self.save(**extra)
        self.assertEqual(before, self.store.list_public())

    def test_legacy_settings_load_defaults_and_updated_limits_keep_the_existing_key(self):
        provider = self.save()
        conn = connect(self.db)
        try:
            data = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (ai.SETTINGS_KEY,)).fetchone()[0])
            for item in data['providers']:
                item['thinking_mode'] = 'disabled'
                item['max_output_tokens'] = 1800
            conn.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(data), ai.SETTINGS_KEY))
            conn.commit()
        finally:
            conn.close()
        restored = ai.ProviderStore(self.db, self.root / 'secrets')
        legacy = restored.list_public()['providers'][0]
        self.assertEqual(legacy['thinking_mode'], 'auto')
        self.assertIsNone(legacy['max_output_tokens'])
        restored.save({'id': provider['id'], 'thinking_mode': 'enabled', 'max_output_tokens': 4096})
        selected = restored.get(provider['id'])
        self.assertEqual((selected['thinking_mode'], selected['max_output_tokens']), ('enabled', None))
        self.assertEqual(selected['api_key'], 'sk-fake-test-credential')
        conn = connect(self.db)
        try:
            saved = json.loads(conn.execute('SELECT value FROM meta WHERE key=?', (ai.SETTINGS_KEY,)).fetchone()[0])
        finally:
            conn.close()
        self.assertIsNone(saved['providers'][0]['max_output_tokens'])

    def test_new_provider_and_old_client_budgets_never_limit_output(self):
        for old_limit in (None, 1800, 131072, 500000):
            with self.subTest(limit=old_limit):
                provider = self.save(max_output_tokens=old_limit, thinking_mode='disabled')
                self.assertIsNone(provider['max_output_tokens'])
                self.assertEqual(provider['thinking_mode'], 'auto')

    def test_typesafe_uses_native_protocol_and_canonical_endpoint(self):
        provider = self.save(protocol='typesafe', base_url='https://api.typesafe.ai/v1', model='jev-latest')
        self.assertEqual(provider['protocol'], 'typesafe')
        self.assertNotIn('api_key', provider)
        self.assertEqual(self.store.get(provider['id'])['api_key'], 'sk-fake-test-credential')
        with self.assertRaises(ValueError):
            self.save(protocol='typesafe', base_url='https://example.com/v1')

    def test_token_parameter_defaults_and_full_endpoint_normalization(self):
        official = self.save(base_url='https://api.openai.com/v1/chat/completions/')
        self.assertEqual(official['base_url'], 'https://api.openai.com/v1')
        self.assertEqual(official['token_parameter'], 'max_completion_tokens')
        compatible = self.save(base_url='https://example.com/v1')
        self.assertEqual(compatible['token_parameter'], 'max_tokens')

    def test_concurrent_stores_do_not_overwrite_other_providers(self):
        def add(index):
            store = ai.ProviderStore(self.db, self.root / 'secrets')
            store.save({'name': f'Provider {index}', 'base_url': 'https://example.com/v1',
                        'model': 'user-model', 'api_key': ''})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(8)))
        public = self.store.list_public()
        self.assertEqual(len(public['providers']), 8)
        self.assertEqual(sum(provider['is_default'] for provider in public['providers']), 1)

    def test_failed_database_write_restores_previous_credential(self):
        provider = self.save()
        with patch.object(self.store, '_write', side_effect=OSError('private internal path')):
            with self.assertRaises(ai.ProviderError) as error:
                self.store.save({'id': provider['id'], 'api_key': 'replacement-key'})
        self.assertNotIn('private internal path', str(error.exception))
        self.assertEqual(self.store.get()['api_key'], 'sk-fake-test-credential')
        with patch.object(self.store, '_write', side_effect=OSError('private internal path')):
            with self.assertRaises(ai.ProviderError):
                self.store.delete(provider['id'])
        self.assertEqual(self.store.get()['api_key'], 'sk-fake-test-credential')


class ProviderNetworkTests(unittest.TestCase):
    def test_rejects_unsafe_urls_without_resolving_or_connecting(self):
        for url in ('http://example.com/v1', 'https://user:pass@example.com/v1',
                    'https://example.com/v1?key=secret', 'https://example.com/v1#secret',
                    'https://169.254.169.254/latest', 'https://10.0.0.1/v1',
                    'https://192.168.1.1/v1', 'https://100.100.100.200/v1',
                    'https://224.0.0.1/v1',
                    'https://metadata.google.internal/v1', 'https://[fe80::1]/v1',
                    'file:///etc/passwd', 'https://example.com:0/v1',
                    'https://public.example\\@127.0.0.1/v1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ai._validate_url(url)
        self.assertEqual(ai._validate_url('http://[::1]:11434/v1'), 'http://[::1]:11434/v1')

    def test_dns_rebinding_to_private_or_loopback_address_is_rejected(self):
        for address in ('127.0.0.1', '10.0.0.2', '169.254.169.254'):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
            with patch.object(socket, 'getaddrinfo', return_value=answers):
                with self.assertRaises(ai.ProviderError):
                    ai._resolve_address('public.example', 443, time.monotonic() + 2)

    def test_completion_sends_only_text_fields_and_reports_allowlisted_usage(self):
        provider = {'base_url': 'https://api.openai.com/v1', 'model': 'selected-model',
                    'protocol': 'openai_chat', 'api_key': 'sk-fake-test-key'}
        response = {'choices': [{'message': {'content': 'Answer sk-fake-test-key'}}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15,
                              'credential': 'secret', 'details': {'untrusted': 'value'}}}
        with patch.object(ai, '_request_json', return_value=response) as request:
            result = ai.complete(provider, [{'role': 'user', 'content': 'Test', 'tools': 'discard'}])
        request.assert_called_once()
        endpoint, raw, headers = request.call_args.args
        body = json.loads(raw)
        self.assertEqual(endpoint, 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(body, {'model': 'selected-model', 'messages': [{'role': 'user', 'content': 'Test'}],
                                'stream': True})
        self.assertEqual(headers['Authorization'], 'Bearer sk-fake-test-key')
        self.assertEqual(result, {'text': 'Answer [REDACTED]', 'model': 'selected-model',
                                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})
        provider.update(base_url='https://example.com/v1', token_parameter='max_tokens')
        with patch.object(ai, '_request_json', return_value=response) as request:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}], max_output_tokens=42)
        self.assertNotIn('max_tokens', json.loads(request.call_args.args[1]))
        self.assertNotIn('max_completion_tokens', json.loads(request.call_args.args[1]))

    def test_transport_errors_are_sanitized_and_never_retried(self):
        provider = {'base_url': 'https://example.com/v1', 'model': 'model',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        for error in (OSError('Authorization Bearer fake-key'), TimeoutError('fake-key'),
                      ValueError('upstream raw body secret')):
            with patch.object(ai, '_request_json', side_effect=error) as request:
                with self.assertRaises(ai.ProviderError) as caught:
                    ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
            self.assertNotIn('fake-key', str(caught.exception))
            self.assertNotIn('raw body', str(caught.exception))
            request.assert_called_once()

    def test_http_429_preserves_safe_retry_after_without_retrying_or_reading_error_body(self):
        class WireSocket:
            def settimeout(self, value):
                pass
            def sendall(self, value):
                pass
            def makefile(self, mode):
                return io.BytesIO(wire)
            def shutdown(self, how):
                pass
            def close(self):
                pass

        provider = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'test-model',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        cases = (
            ('125', 125), ('12.5', 12.5), ('0', 0),
            ('Wed, 21 Oct 2015 07:28:00 GMT', 120),
            ('Wed, 21 Oct 2015 07:25:00 GMT', 0),
            ('999999999', 604800),
            (None, None), ('invalid date fake-key', None), ('-1', None),
            ('nan', None), ('inf', None), ('1e999', None),
        )
        for raw_delay, expected in cases:
            header = '' if raw_delay is None else 'Retry-After: ' + raw_delay + '\r\n'
            wire = ('HTTP/1.1 429 Too Many Requests\r\n' + header +
                    'Content-Length: 36\r\n\r\nprivate upstream error body fake-key').encode('ascii')
            with self.subTest(retry_after=raw_delay), \
                    patch.object(ai.time, 'time', return_value=1445412360), \
                    patch.object(ai, '_resolve_address', return_value='127.0.0.1'), \
                    patch.object(ai.socket, 'create_connection', return_value=WireSocket()) as connect_socket:
                with self.assertRaises(ai.ProviderError) as caught:
                    ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(getattr(caught.exception, 'retry_after', None), expected)
                self.assertNotIn('fake-key', str(caught.exception))
                self.assertNotIn('private upstream', str(caught.exception))
                self.assertEqual(caught.exception.diagnostics['stage'], 'headers')
                connect_socket.assert_called_once()

    def test_legacy_timeouts_and_output_budgets_cannot_limit_generation(self):
        provider = {'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        response = {'choices': [{'message': {'content': 'Ready'}}]}
        with patch.object(ai, '_request_json', return_value=response) as request:
            for supplied in ({}, {'timeout_seconds': 120}, {'idle_timeout_seconds': 600},
                             {'wait_for_completion': False}, {'max_output_tokens': 1024},
                             {'timeout_seconds': 1, 'idle_timeout_seconds': 1, 'max_output_tokens': 1800}):
                with self.subTest(options=supplied):
                    ai.complete({**provider, 'max_output_tokens': 1800}, [{'role': 'user', 'content': 'Test'}], **supplied)
                    self.assertEqual(request.call_args.kwargs, {'wait_for_completion': True, 'stream_response': True,
                        'connection_timeout_seconds': 60})
                    wire = json.loads(request.call_args.args[1])
                    self.assertNotIn('timeout_seconds', wire)
                    self.assertNotIn('max_tokens', wire)
                    self.assertNotIn('max_completion_tokens', wire)
                    self.assertTrue(wire['stream'])
                    self.assertNotIn('connection_timeout_seconds', wire)

    def test_complete_passes_each_provider_setup_timeout_only_to_transport(self):
        provider = {'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        response = {'choices': [{'message': {'content': 'Ready'}}]}
        for timeout in (20, 90):
            with self.subTest(timeout=timeout), patch.object(ai, '_request_json', return_value=response) as request:
                ai.complete({**provider, 'connection_timeout_seconds': timeout}, [{'role': 'user', 'content': 'Test'}])
                self.assertEqual(request.call_args.kwargs['connection_timeout_seconds'], timeout)
                self.assertNotIn('connection_timeout_seconds', json.loads(request.call_args.args[1]))

    def test_complete_wait_cancel_and_progress_are_transport_options_only(self):
        provider = {'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'protocol': 'openai_chat', 'api_key': 'fake-key', 'thinking_mode': 'auto'}
        response = {'choices': [{'message': {'content': 'Ready'}}],
                    'usage': {'completion_tokens': 4, 'secret': 'private'}}
        cancelled, progress = threading.Event(), []
        with patch.object(ai, '_request_json', return_value=response) as request:
            result = ai.complete(provider, [{'role': 'user', 'content': 'Test'}],
                wait_for_completion=True, cancellation_event=cancelled, on_progress=progress.append)
        self.assertEqual(result['text'], 'Ready')
        options = request.call_args.kwargs
        self.assertTrue(options['wait_for_completion'])
        self.assertIs(options['cancellation_event'], cancelled)
        wire = json.loads(request.call_args.args[1])
        self.assertNotIn('wait_for_completion', wire)
        self.assertNotIn('reasoning_effort', wire)
        self.assertNotIn('thinking', wire)
        self.assertTrue(wire['stream'])
        self.assertNotIn('max_tokens', wire)
        self.assertEqual(progress[-1]['stage'], 'complete')
        self.assertEqual(progress[-1]['completion_tokens'], 4)
        self.assertNotIn('Ready', str(progress))
        self.assertNotIn('private', str(progress))

        cancelled.set()
        with patch.object(ai, '_request_json') as request, self.assertRaises(ai.ProviderError) as caught:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}],
                wait_for_completion=True, cancellation_event=cancelled)
        self.assertEqual(caught.exception.status_code, 499)
        request.assert_not_called()

    def test_thinking_enabled_uses_service_compatible_fields_and_never_disables(self):
        for host in ('api.x.ai', 'us-east-1.api.x.ai', 'api.openai.com', 'openrouter.ai', 'api.deepseek.com', 'example.com'):
            for mode in ('enabled', 'disabled'):
                provider = {'base_url': 'https://' + host + '/v1', 'model': 'configured',
                            'protocol': 'openai_chat', 'api_key': 'fake-key', 'thinking_mode': mode}
                with self.subTest(host=host, mode=mode), patch.object(ai, '_request_json',
                        return_value={'choices': [{'message': {'content': 'Ready'}}]}) as request:
                    ai.complete(provider, [{'role': 'user', 'content': 'Test'}], wait_for_completion=True)
                wire = json.loads(request.call_args.args[1])
                if mode == 'enabled' and host == 'openrouter.ai':
                    self.assertEqual(wire['reasoning'], {'enabled': True})
                elif mode == 'enabled' and host in ('api.deepseek.com', 'example.com'):
                    self.assertEqual(wire['thinking'], {'type': 'enabled'})
                else:
                    self.assertNotIn('thinking', wire)
                    self.assertNotIn('reasoning', wire)
                self.assertNotIn('reasoning_effort', wire)

    def test_all_deepseek_auto_calls_preserve_provider_thinking(self):
        provider = {'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-flash',
                    'protocol': 'openai_chat', 'api_key': 'fake-key', 'thinking_mode': 'auto'}
        response = {'choices': [{'message': {'content': 'Ready'}}]}
        with patch.object(ai, '_request_json', return_value=response) as request:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}], wait_for_completion=True)
            wire = json.loads(request.call_args.args[1])
            self.assertTrue(wire['stream'])
            self.assertNotIn('thinking', wire)
            self.assertNotIn('reasoning_effort', wire)
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
            interactive = json.loads(request.call_args.args[1])
            self.assertTrue(interactive['stream'])
            self.assertNotIn('thinking', interactive)

    def test_timeout_diagnostics_allowlist_and_human_stage_message(self):
        provider = {'base_url': 'https://example.com/v1', 'model': 'model',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        timeout = ai._TransportTimeout({'stage': 'headers', 'elapsed_seconds': 45.02,
            'timeout_seconds': 45, 'request_id': 'req_12', 'raw_body': 'private',
            'Authorization': 'fake-key'})
        with patch.object(ai, '_request_json', side_effect=timeout), self.assertRaises(ai.ProviderError) as caught:
            ai.complete(provider, [{'role': 'user', 'content': 'private prompt'}])
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(caught.exception.diagnostics, {'stage': 'headers', 'elapsed_seconds': 45.02,
            'timeout_seconds': 45, 'request_id': 'req_12'})
        self.assertIn('45.0', str(caught.exception))
        self.assertIn('等待响应', str(caught.exception))
        self.assertNotIn('private', str(caught.exception.diagnostics))
        self.assertNotIn('fake-key', str(caught.exception.diagnostics))

    def test_grok_budget_error_does_not_advise_disabling_unsupported_thinking(self):
        provider = {'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        response = {'choices': [{'finish_reason': 'length', 'message': {'content': ''}}]}
        with patch.object(ai, '_request_json', return_value=response), self.assertRaises(ai.ProviderError) as caught:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
        self.assertIn('不能关闭思考', str(caught.exception))
        self.assertNotIn('高级设置', str(caught.exception))

    def test_waiting_headers_has_no_generation_deadline_and_cancellation_closes_socket(self):
        provider = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'configured',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        cancelled = threading.Event()
        progress = []
        sockets = []

        class FakeSocket:
            def __init__(self):
                self.stopped = threading.Event()
                self.timeout = 0
                self.closed = False
                sockets.append(self)

            def settimeout(self, value):
                self.timeout = value

            def shutdown(self, how):
                self.stopped.set()

            def close(self):
                self.closed = True

        class FakeResponse:
            status = 200
            body = (b'data: {"choices":[{"index":0,"delta":{"content":"Complete answer"},"finish_reason":null}]}\n\n'
                    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n')

            def getheader(self, name):
                return 'req_17' if name == 'x-request-id' else None

            def read1(self, maximum):
                body, self.body = self.body, b''
                return body

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                if cancelled.is_set():
                    raise AssertionError('cancelled before headers')
                # This outlasts the ordinary deadline patched below.
                self.sock.stopped.wait(.15)
                if self.sock.stopped.is_set():
                    raise OSError('private upstream error')
                self_test.assertIsNone(self.sock.timeout)
                return FakeResponse()

            def close(self):
                self.sock.close()

        self_test = self
        with patch.object(ai, '_resolve_address', return_value='127.0.0.1'), \
                patch.object(ai.socket, 'create_connection', side_effect=lambda *a, **k: FakeSocket()), \
                patch.object(ai.http.client, 'HTTPConnection', FakeConnection), \
                patch.object(ai, 'REQUEST_TIMEOUT', .03):
            result = ai.complete(provider, [{'role': 'user', 'content': 'private prompt'}],
                wait_for_completion=True, cancellation_event=cancelled, on_progress=progress.append)
            self.assertEqual(result['text'], 'Complete answer')
            self.assertTrue(sockets[-1].closed)
            self.assertEqual(next(p for p in progress if p['stage'] == 'body')['request_id'], 'req_17')
            self.assertNotIn('private prompt', str(progress))
            self.assertNotIn('fake-key', str(progress))
            cancellation_timer = threading.Timer(.03, cancelled.set)
            cancellation_timer.start()
            try:
                with self.assertRaises(ai.ProviderError) as caught:
                    ai.complete(provider, [{'role': 'user', 'content': 'Test'}],
                        wait_for_completion=True, cancellation_event=cancelled)
                self.assertEqual(caught.exception.status_code, 499)
                self.assertEqual(caught.exception.diagnostics['stage'], 'headers')
                self.assertTrue(sockets[-1].closed)
                self.assertTrue(sockets[-1].stopped.is_set())
            finally:
                cancellation_timer.cancel()

            cancelled.clear()
            def cancel_while_receiving(item):
                if item.get('stage') == 'receiving':
                    cancelled.set()
            with self.assertRaises(ai.ProviderError) as caught:
                ai.complete(provider, [{'role': 'user', 'content': 'Test'}],
                    wait_for_completion=True, cancellation_event=cancelled,
                    on_progress=cancel_while_receiving)
            self.assertEqual(caught.exception.status_code, 499)
            self.assertTrue(sockets[-1].closed)

    def test_sse_handles_utf8_chunks_keepalives_usage_and_discards_reasoning(self):
        secret_reasoning = '不应保存的私有推理'
        events = [': keep-alive\r\n\r\n', '\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'reasoning_content': secret_reasoning}, 'finish_reason': None}]}, ensure_ascii=False) + '\r\n\r\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': '你好，'}, 'finish_reason': None}]}, ensure_ascii=False) + '\n\n',
            'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'Maya。'}, 'finish_reason': None}]}, ensure_ascii=False) + '\n\n',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":8,"total_tokens":18,"private":"discard"}}\n\n',
            'data: [DONE]\n\n']
        stages = []
        parser = ai._SSECompletion(on_stage=stages.append)
        for byte in ''.join(events).encode('utf-8'):
            parser.feed(bytes([byte]))
        result = parser.result()
        self.assertEqual(ai._completion_text(result['choices'][0]), '你好，Maya。')
        self.assertTrue(result['choices'][0]['message']['has_reasoning'])
        self.assertEqual(result['usage'], {'prompt_tokens': 10, 'completion_tokens': 8, 'total_tokens': 18})
        self.assertEqual(stages, ['thinking', 'receiving', 'receiving'])
        self.assertNotIn(secret_reasoning, str(vars(parser)))
        self.assertNotIn('private', str(result))

    def test_stream_generation_can_outlast_both_old_45_and_120_second_limits(self):
        provider = {'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'protocol': 'openai_chat', 'api_key': 'fake-key', 'thinking_mode': 'auto'}
        clock = [1000.0]
        cancelled, progress = threading.Event(), []
        self_test = self

        class FakeSocket:
            timeout = None
            closed = False
            def do_handshake(self):
                pass
            def settimeout(self, value):
                self.timeout = value
            def shutdown(self, how):
                raise AssertionError('generation must not be stopped by a deadline')
            def close(self):
                self.closed = True

        class FakeResponse:
            status = 200
            read = False
            def read1(self, maximum):
                if self.read:
                    return b''
                self.read = True
                clock[0] += 1200
                return (b': keep-alive\n\n'
                    b'data: {"choices":[{"delta":{"content":"Complete"},"finish_reason":"stop"}]}\n\n'
                    b'data: [DONE]\n\n')

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, method, path, body, headers):
                wire = json.loads(body)
                self_test.assertTrue(wire['stream'])
                self_test.assertNotIn('reasoning_effort', wire)
                self_test.assertNotIn('thinking', wire)
                self_test.assertNotIn('max_tokens', wire)
            def getresponse(self):
                self_test.assertIsNone(self.sock.timeout)
                clock[0] += 1200
                return FakeResponse()
            def close(self):
                self.sock.close()

        sock = FakeSocket()
        with patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(ai, '_resolve_address', return_value='93.184.216.34'), \
                patch.object(ai.socket, 'create_connection', return_value=sock), \
                patch.object(ai.ssl, 'create_default_context') as tls, \
                patch.object(ai.http.client, 'HTTPConnection', FakeConnection):
            tls.return_value.wrap_socket.return_value = sock
            result = ai.complete(provider, [{'role': 'user', 'content': 'Test'}],
                cancellation_event=cancelled, on_progress=progress.append,
                timeout_seconds=1, idle_timeout_seconds=600)
        self.assertEqual(result['text'], 'Complete')
        self.assertEqual(progress[-1]['elapsed_seconds'], 2400)
        self.assertNotIn('timeout_seconds', progress[-1])
        self.assertFalse(cancelled.is_set())
        self.assertTrue(sock.closed)

    def test_sse_requires_done_and_finish_and_preserves_rejection_or_truncation(self):
        content = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        finish = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        done = b'data: [DONE]\n\n'
        for wire in (content, content + finish, content + done):
            with self.subTest(wire=wire), self.assertRaises(ai.ProviderError):
                parser = ai._SSECompletion()
                parser.feed(wire)
                parser.result()
        for delta, reason, message in (({'refusal': 'private refusal'}, 'stop', '过滤'),
                ({'tool_calls': [{'function': {'name': 'private'}}]}, 'tool_calls', '工具调用'),
                ({'reasoning_content': 'private reasoning'}, 'length', '输出预算')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ai.ProviderError, message):
                parser = ai._SSECompletion()
                parser.feed(('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]})
                    + '\n\ndata: [DONE]\n\n').encode())
                result = parser.result()
                ai._completion_text(result['choices'][0])
        parser = ai._SSECompletion()
        with self.assertRaises(ai.ProviderError) as caught:
            parser.feed(b'data: {"error":{"message":"private-key-and-upstream-body"}}\n\n')
        self.assertNotIn('private', str(caught.exception))

    def test_long_stream_and_json_completions_are_not_truncated_by_local_limits(self):
        text = 'a' * 2_100_000
        provider = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'configured',
                    'protocol': 'openai_chat', 'api_key': ''}
        for content_type in ('application/json', 'text/event-stream'):
            if content_type == 'application/json':
                body = json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]}).encode()
            else:
                body = ('data: ' + json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]})
                        + '\n\ndata: [DONE]\n\n').encode()
            wire = (b'HTTP/1.1 200 OK\r\nContent-Type: ' + content_type.encode()
                    + b'\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)

            class WireSocket:
                def settimeout(self, timeout):
                    pass
                def sendall(self, value):
                    pass
                def makefile(self, mode):
                    return io.BytesIO(wire)
                def shutdown(self, how):
                    pass
                def close(self):
                    pass

            with self.subTest(content_type=content_type), \
                    patch.object(ai, '_resolve_address', return_value='127.0.0.1'), \
                    patch.object(ai.socket, 'create_connection', return_value=WireSocket()), \
                    patch.object(ai, 'MAX_RESPONSE_BYTES', 64):
                result = ai.complete(provider, [{'role': 'user', 'content': 'Test'}], max_output_tokens=1)
            self.assertEqual(result['text'], text)

    def test_connection_setup_retains_its_separate_deadline(self):
        provider = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'configured',
                    'protocol': 'openai_chat', 'api_key': ''}
        for settings, timeout in (({}, 60), ({'connection_timeout_seconds': 20}, 20),
                                  ({'connection_timeout_seconds': 90}, 90)):
            clock = [1000.0]
            def resolved(host, port, deadline, **options):
                self.assertEqual(deadline - clock[0], timeout)
                clock[0] += timeout + 1
                return '127.0.0.1'

            with self.subTest(settings=settings), \
                    patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(ai, '_resolve_address', side_effect=resolved), \
                    patch.object(ai.socket, 'create_connection') as connected:
                with self.assertRaises(ai.ProviderError) as caught:
                    ai.complete({**provider, **settings}, [{'role': 'user', 'content': 'Test'}])
            self.assertEqual(caught.exception.status_code, 504)
            self.assertEqual(caught.exception.diagnostics['stage'], 'dns')
            self.assertEqual(caught.exception.diagnostics['timeout_seconds'], timeout)
            self.assertIn('连接准备超时', str(caught.exception))
            self.assertIn('解析服务地址已用时', str(caught.exception))
            connected.assert_not_called()

    def test_non_generation_transport_keeps_its_existing_request_deadline(self):
        clock = [1000.0]
        def resolved(host, port, deadline, **options):
            self.assertEqual(deadline - clock[0], 8)
            clock[0] += 9
            return '127.0.0.1'
        with patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(ai, '_resolve_address', side_effect=resolved), \
                patch.object(ai.socket, 'create_connection') as connected:
            with self.assertRaises(ai._TransportTimeout) as caught:
                ai._request_json('http://127.0.0.1:1234/embeddings', b'{}', {},
                    timeout_seconds=8, connection_timeout_seconds=90)
        self.assertEqual(caught.exception.diagnostics['timeout_seconds'], 8)
        connected.assert_not_called()

    def test_initial_progress_delay_does_not_consume_network_setup_budget(self):
        clock = [1000.0]
        provider = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'configured',
                    'protocol': 'openai_chat', 'api_key': ''}
        remaining_at_dns = []

        def report(progress):
            if progress['stage'] == 'dns':
                clock[0] += ai.CONNECTION_TIMEOUT + 5

        def resolve(host, port, deadline, **options):
            remaining_at_dns.append(deadline - clock[0])
            clock[0] += 2
            return '127.0.0.1'

        class FakeSocket:
            def settimeout(self, value):
                pass
            def close(self):
                pass

        class FakeResponse:
            status = 200
            pending = True
            def getheader(self, name):
                return 'application/json' if name == 'Content-Type' else None
            def read1(self, maximum):
                if not self.pending:
                    return b''
                self.pending = False
                return b'{"choices":[{"message":{"content":"Ready"}}]}'

        class FakeConnection:
            def __init__(self, *args, **options):
                pass
            def request(self, *args, **options):
                pass
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass

        with patch.object(ai.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(ai, '_resolve_address', side_effect=resolve), \
                patch.object(ai.socket, 'create_connection', return_value=FakeSocket()) as connected, \
                patch.object(ai.http.client, 'HTTPConnection', FakeConnection):
            result = ai.complete(provider, [{'role': 'user', 'content': 'Test'}], on_progress=report)
        self.assertEqual(result['text'], 'Ready')
        self.assertEqual(remaining_at_dns, [ai.CONNECTION_TIMEOUT])
        self.assertEqual(connected.call_args.kwargs['timeout'], ai.CONNECTION_TIMEOUT - 2)
        connected.assert_called_once()

    def test_transport_timeout_identifies_each_stage_without_sensitive_data(self):
        provider = {'base_url': 'https://example.com/v1', 'model': 'selected',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        for failing_stage in ('dns', 'connect', 'tls', 'send', 'headers', 'body'):
            class FakeSocket:
                def do_handshake(self):
                    pass
                def settimeout(self, value):
                    pass
                def shutdown(self, how):
                    pass
                def close(self):
                    pass

            class FakeResponse:
                status = 200
                def getheader(self, name):
                    return 'req_safe'
                def read1(self, maximum):
                    raise TimeoutError('private socket details')

            class FakeConnection:
                def __init__(self, *args, **kwargs):
                    pass
                def request(self, *args, **kwargs):
                    if failing_stage == 'send':
                        raise TimeoutError('fake-key')
                def getresponse(self):
                    if failing_stage == 'headers':
                        raise TimeoutError('fake-key')
                    return FakeResponse()
                def close(self):
                    pass

            sock = FakeSocket()
            def resolved(*args, **kwargs):
                if failing_stage == 'dns':
                    raise TimeoutError('private DNS')
                return '93.184.216.34'
            def connected(*args, **kwargs):
                if failing_stage == 'connect':
                    raise TimeoutError('private connection')
                return sock
            def secured(*args, **kwargs):
                if failing_stage == 'tls':
                    raise TimeoutError('private TLS')
                return sock
            with self.subTest(stage=failing_stage), patch.object(ai, '_resolve_address', side_effect=resolved), \
                    patch.object(ai.socket, 'create_connection', side_effect=connected), \
                    patch.object(ai.ssl, 'create_default_context') as tls, \
                    patch.object(ai.http.client, 'HTTPConnection', FakeConnection):
                tls.return_value.wrap_socket.side_effect = secured
                with self.assertRaises(ai.ProviderError) as caught:
                    ai.complete(provider, [{'role': 'user', 'content': 'private prompt'}])
                self.assertEqual(caught.exception.status_code, 504)
                self.assertEqual(caught.exception.diagnostics['stage'], failing_stage)
                if failing_stage in ('dns', 'connect', 'tls', 'send'):
                    self.assertEqual(caught.exception.diagnostics['timeout_seconds'], ai.CONNECTION_TIMEOUT)
                    self.assertIn('AI 服务连接准备超时', str(caught.exception))
                else:
                    self.assertNotIn('timeout_seconds', caught.exception.diagnostics)
                    self.assertIn('AI 服务响应超时', str(caught.exception))
                self.assertNotIn('private', str(caught.exception.diagnostics))
                self.assertNotIn('fake-key', str(caught.exception.diagnostics))

    def test_cancellation_during_tls_handshake_closes_the_wrapped_socket(self):
        provider = {'base_url': 'https://example.com/v1', 'model': 'selected',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        cancelled = threading.Event()

        class RawSocket:
            closed = False
            def settimeout(self, value):
                pass
            def shutdown(self, how):
                raise AssertionError('the detached raw socket must not handle TLS cancellation')
            def close(self):
                self.closed = True

        class TLSSocket:
            def __init__(self):
                self.stopped = threading.Event()
                self.closed = False
            def do_handshake(self):
                if self.stopped.wait(1):
                    raise OSError('TLS socket closed')
                raise AssertionError('TLS handshake cancellation did not close socket')
            def shutdown(self, how):
                self.stopped.set()
            def close(self):
                self.closed = True

        raw, secured = RawSocket(), TLSSocket()
        timer = threading.Timer(.03, cancelled.set)
        with patch.object(ai, '_resolve_address', return_value='93.184.216.34'), \
                patch.object(ai.socket, 'create_connection', return_value=raw), \
                patch.object(ai.ssl, 'create_default_context') as tls:
            tls.return_value.wrap_socket.return_value = secured
            timer.start()
            try:
                with self.assertRaises(ai.ProviderError) as caught:
                    ai.complete(provider, [{'role': 'user', 'content': 'Test'}],
                        wait_for_completion=True, cancellation_event=cancelled)
            finally:
                timer.cancel()
        tls.return_value.wrap_socket.assert_called_once_with(raw, server_hostname='example.com',
                                                            do_handshake_on_connect=False)
        self.assertEqual(caught.exception.status_code, 499)
        self.assertEqual(caught.exception.diagnostics['stage'], 'tls')
        self.assertTrue(secured.closed)
        self.assertTrue(secured.stopped.is_set())

    def test_agent_accepts_explicit_complete_json_when_compatible_service_ignores_stream(self):
        body = json.dumps({'choices': [{'message': {'content': 'Complete JSON'}, 'finish_reason': 'stop'}]}).encode()
        wire = (b'HTTP/1.1 200 OK\r\nContent-Type: Application/JSON; charset=utf-8\r\nContent-Length: '
                + str(len(body)).encode() + b'\r\n\r\n' + body)
        sent = []

        class WireSocket:
            def do_handshake(self):
                pass
            def settimeout(self, timeout):
                pass
            def sendall(self, value):
                sent.append(value)
            def makefile(self, mode):
                return io.BytesIO(wire)
            def shutdown(self, how):
                pass
            def close(self):
                pass

        sock = WireSocket()
        provider = {'base_url': 'https://example.com/v1', 'model': 'configured',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        with patch.object(ai, '_resolve_address', return_value='93.184.216.34'), \
                patch.object(ai.socket, 'create_connection', return_value=sock) as connect_socket, \
                patch.object(ai.ssl, 'create_default_context') as tls:
            tls.return_value.wrap_socket.return_value = sock
            result = ai.complete(provider, [{'role': 'user', 'content': 'Test'}], wait_for_completion=True)
        self.assertEqual(result['text'], 'Complete JSON')
        self.assertIn(b'"stream": true', b''.join(sent))
        connect_socket.assert_called_once()

    def test_grok_strict_decision_shape_limits_body_and_allowed_sources(self):
        from ctn_data.agent_decision import response_schema
        provider = {'base_url': 'https://api.x.ai/v1', 'model': 'grok-4.7',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        schema = response_schema({'allowed_actions': ['reply', 'skip']}, [{'id': 'post:7', 'post_id': 7}])
        response = {'choices': [{'message': {'content': '{"action":"skip"}'}}]}
        with patch.object(ai, '_request_json', return_value=response) as request:
            ai.complete(provider, [{'role': 'user', 'content': 'Return a JSON decision.'}], response_schema=schema)
        wire = json.loads(request.call_args.args[1])
        shape = wire['response_format']['json_schema']
        self.assertTrue(shape['strict'])
        self.assertEqual(shape['schema']['properties']['body']['maxLength'], 500)
        self.assertEqual(shape['schema']['properties']['action']['enum'], ['reply', 'skip'])
        self.assertEqual(shape['schema']['properties']['source_ids']['items']['enum'], ['post:7'])
        self.assertEqual(shape['schema']['properties']['target_post_id']['enum'], [None, 7])

    def test_only_structured_openrouter_requests_require_supporting_upstreams(self):
        schema = {'type': 'object', 'properties': {'source_ids': {
            'type': 'array', 'items': {'type': 'string', 'enum': ['post:7']}}},
            'required': ['source_ids'], 'additionalProperties': False}
        response = {'choices': [{'message': {'content': '{"source_ids":["post:7"]}'}}]}
        cases = (
            ('https://openrouter.ai/api/v1', schema, True),
            ('https://openrouter.ai/api/v1', None, False),
            ('https://api.x.ai/v1', schema, False),
            ('https://example.com/v1', schema, False),
        )
        for base_url, supplied_schema, requires_support in cases:
            provider = {'base_url': base_url, 'model': 'configured-model',
                        'protocol': 'openai_chat', 'api_key': 'fake-key'}
            with self.subTest(base_url=base_url, structured=supplied_schema is not None), \
                    patch.object(ai, '_request_json', return_value=response) as request:
                ai.complete(provider, [{'role': 'user', 'content': 'Return JSON source IDs.'}],
                            response_schema=supplied_schema)
                request.assert_called_once()
                wire = json.loads(request.call_args.args[1])
                self.assertEqual('provider' in wire, requires_support)
                if requires_support:
                    self.assertEqual(wire['provider'], {'require_parameters': True})
                self.assertEqual('response_format' in wire, supplied_schema is not None)
                if supplied_schema is not None:
                    self.assertEqual(wire['response_format'], {'type': 'json_schema', 'json_schema': {
                        'name': 'structured_response', 'strict': True, 'schema': schema}})
                self.assertNotIn('max_tokens', wire)
                self.assertNotIn('max_completion_tokens', wire)

    def test_http_wire_protocol_keeps_original_host_and_pins_checked_ip(self):
        raw_body = json.dumps({'choices': [{'message': {'content': 'Verified'}}]}).encode()
        wire = b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(raw_body)).encode() + b'\r\n\r\n' + raw_body
        sent = []

        class WireSocket:
            def do_handshake(self):
                pass
            def settimeout(self, timeout):
                pass

            def sendall(self, value):
                sent.append(value)

            def makefile(self, mode):
                return io.BytesIO(wire)

            def shutdown(self, how):
                pass

            def close(self):
                pass

        sock = WireSocket()
        provider = {'base_url': 'https://api.example.com/v1', 'model': 'selected',
                    'protocol': 'openai_chat', 'api_key': 'fake-key'}
        with patch.object(ai, '_resolve_address', return_value='93.184.216.34'), \
                patch.object(ai.socket, 'create_connection', return_value=sock) as connect_socket, \
                patch.object(ai.ssl, 'create_default_context') as tls:
            tls.return_value.wrap_socket.return_value = sock
            result = ai.complete(provider, [{'role': 'user', 'content': 'Hello'}])
        self.assertEqual(result['text'], 'Verified')
        self.assertEqual(connect_socket.call_args.args[0], ('93.184.216.34', 443))
        tls.return_value.wrap_socket.assert_called_once_with(sock, server_hostname='api.example.com',
                                                            do_handshake_on_connect=False)
        request = b''.join(sent)
        self.assertIn(b'POST /v1/chat/completions HTTP/1.1\r\n', request)
        self.assertIn(b'Host: api.example.com:443\r\n', request)
        self.assertIn(b'Authorization: Bearer fake-key\r\n', request)

    def test_transport_redirect_error_response_size_and_absolute_deadline(self):
        calls = []

        class FakeSocket:
            def __init__(self):
                self.stopped = threading.Event()

            def settimeout(self, timeout):
                self.timeout = timeout

            def shutdown(self, how):
                self.stopped.set()

            def close(self):
                pass

        class FakeResponse:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            def read1(self, maximum):
                part, self.body = self.body[:maximum], self.body[maximum:]
                return part

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                self.sock = None

            def request(self, method, path, body, headers):
                self.path = path
                calls.append(path)

            def getresponse(self):
                if self.path.startswith('/redirect'):
                    return FakeResponse(302, b'private upstream redirect body')
                if self.path.startswith('/error'):
                    return FakeResponse(401, b'fake-key and private upstream error')
                if self.path.startswith('/slow'):
                    # Simulate getresponse continuously reading header bytes
                    # until the absolute-deadline watchdog shuts the socket.
                    if self.sock.stopped.wait(2):
                        raise OSError('socket stopped')
                    raise AssertionError('absolute deadline did not stop socket')
                return FakeResponse(200, b'x' * 2000)

            def close(self):
                if self.sock:
                    self.sock.close()

        base = 'http://127.0.0.1:1234'
        provider = {'model': 'local-test', 'protocol': 'openai_chat', 'api_key': 'fake-key'}
        with patch.object(ai, '_resolve_address', return_value='127.0.0.1'), \
                patch.object(ai.socket, 'create_connection', side_effect=lambda *a, **k: FakeSocket()), \
                patch.object(ai.http.client, 'HTTPConnection', FakeConnection):
            for path, status in (('/redirect', 400), ('/error', 400), ('/oversized', 502), ('/slow', 504)):
                with self.subTest(path=path), patch.object(ai, 'REQUEST_TIMEOUT', 0.25), \
                        patch.object(ai, 'MAX_RESPONSE_BYTES', 1000):
                    started = time.monotonic()
                    provider['base_url'] = base + path
                    with self.assertRaises((ai.ProviderError, ai._TransportTimeout)) as caught:
                        ai._request_json(provider['base_url'] + '/chat/completions', b'{}', {})
                    if path == '/slow':
                        self.assertIsInstance(caught.exception, ai._TransportTimeout)
                    else:
                        self.assertEqual(caught.exception.status_code, status)
                    self.assertNotIn('private upstream', str(caught.exception))
                    self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(calls, [path + '/chat/completions' for path in ('/redirect', '/error', '/oversized', '/slow')])


class CompletionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.provider = {'base_url': 'https://openrouter.ai/api/v1', 'model': 'configured-model',
                         'protocol': 'openai_chat', 'api_key': 'fake-key'}

    def test_length_rejects_even_valid_json_and_keeps_only_safe_failure_diagnostics(self):
        response = {'id': 'gen_123', 'model': 'vendor/actual-model', 'provider': 'Example Provider',
            'choices': [{'finish_reason': 'length', 'native_finish_reason': 'MAX_TOKENS',
                         'message': {'content': '{"action":"hold"}', 'reasoning': 'private trace'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
                      'completion_tokens_details': {'reasoning_tokens': 45, 'trace': 'private trace'}},
            '_diagnostics': {'request_id': 'req_transport', 'received_bytes': 512, 'raw_body': 'private'}}
        with patch.object(ai, '_request_json', return_value=response) as request, \
                self.assertRaises(ai.ProviderError) as caught:
            ai.complete(self.provider, [{'role': 'user', 'content': 'private prompt'}])
        request.assert_called_once()
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics['error_code'], 'output_truncated')
        self.assertEqual(diagnostics['finish_reason'], 'length')
        self.assertEqual(diagnostics['native_finish_reason'], 'MAX_TOKENS')
        self.assertEqual(diagnostics['model'], 'vendor/actual-model')
        self.assertEqual(diagnostics['provider'], 'Example Provider')
        self.assertEqual(diagnostics['request_id'], 'req_transport')
        self.assertEqual(diagnostics['completion_tokens'], 50)
        self.assertEqual(diagnostics['reasoning_tokens'], 45)
        self.assertEqual(diagnostics['answer_chars'], 17)
        self.assertTrue(diagnostics['has_reasoning'])
        self.assertGreaterEqual(diagnostics['elapsed_seconds'], 0)
        self.assertNotIn('private', str(diagnostics))
        self.assertNotIn('action', str(diagnostics))

    def test_reasoning_usage_without_trace_identifies_thinking_and_unsafe_ids_are_omitted(self):
        response = {'id': 'fake-key', 'model': 'fake-key', 'provider': 'fake-key',
            'choices': [{'finish_reason': 'length', 'native_finish_reason': 'untrusted private reason',
                         'message': {'content': None}}],
            'usage': {'completion_tokens': 4096, 'completion_tokens_details': {'reasoning_tokens': 4096}}}
        with patch.object(ai, '_request_json', return_value=response), self.assertRaises(ai.ProviderError) as caught:
            ai.complete(self.provider, [{'role': 'user', 'content': 'Test'}])
        diagnostics = caught.exception.diagnostics
        self.assertTrue(diagnostics['has_reasoning'])
        self.assertEqual(diagnostics['answer_chars'], 0)
        self.assertNotIn('native_finish_reason', diagnostics)
        self.assertNotIn('fake-key', str(diagnostics))
        self.assertNotIn('untrusted', str(diagnostics))

    def test_native_truncation_is_not_accepted_as_a_complete_answer(self):
        for native_reason in ('MAX_TOKENS', 'CONTINUATION', 'max_tokens', 'model_length'):
            with self.subTest(native_reason=native_reason), self.assertRaises(ai.ProviderError) as caught:
                ai._completion_text({'message': {'content': '{"complete": true}'},
                                     'finish_reason': None, 'native_finish_reason': native_reason})
            self.assertEqual(caught.exception.diagnostics['error_code'], 'output_truncated')
            self.assertEqual(caught.exception.diagnostics['native_finish_reason'], native_reason)

    def test_sse_keeps_final_openrouter_usage_native_reason_and_identifiers_without_trace(self):
        parser = ai._SSECompletion()
        for event in ({'id': 'gen_456', 'model': 'vendor/actual-model', 'provider': 'Example Provider',
                       'choices': [{'delta': {'reasoning_details': [{'text': 'private trace'}]},
                                    'finish_reason': None}]},
                      {'choices': [{'delta': {'content': '{"partial":'}, 'finish_reason': None}]},
                      {'choices': [{'delta': {}, 'finish_reason': 'length', 'native_finish_reason': 'MAX_TOKENS'}],
                       'usage': {'prompt_tokens': 11, 'completion_tokens': 30, 'total_tokens': 41,
                                 'completion_tokens_details': {'reasoning_tokens': 27, 'secret': 'private trace'}}}):
            parser.feed(('data: ' + json.dumps(event) + '\n\n').encode())
        parser.feed(b'data: [DONE]\n\n')
        result = parser.result()
        self.assertEqual(result['request_id'], 'gen_456')
        self.assertEqual(result['provider'], 'Example Provider')
        self.assertEqual(result['model'], 'vendor/actual-model')
        self.assertEqual(result['choices'][0]['native_finish_reason'], 'MAX_TOKENS')
        self.assertEqual(result['usage'], {'prompt_tokens': 11, 'completion_tokens': 30,
                                           'total_tokens': 41, 'reasoning_tokens': 27})
        self.assertNotIn('private trace', str(result))
        self.assertNotIn('private trace', str(vars(parser)))
        with patch.object(ai, '_request_json', return_value=result), self.assertRaises(ai.ProviderError) as caught:
            ai.complete(self.provider, [{'role': 'user', 'content': 'Test'}])
        self.assertEqual(caught.exception.diagnostics['request_id'], 'gen_456')
        self.assertEqual(caught.exception.diagnostics['reasoning_tokens'], 27)

    def test_success_keeps_numeric_reasoning_usage_without_returning_reasoning(self):
        response = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Complete'}}],
                    'usage': {'completion_tokens': 21, 'completion_tokens_details': {
                        'reasoning_tokens': 17, 'reasoning': 'private trace'}}}
        with patch.object(ai, '_request_json', return_value=response):
            result = ai.complete(self.provider, [{'role': 'user', 'content': 'Test'}])
        self.assertEqual(result['usage'], {'completion_tokens': 21, 'reasoning_tokens': 17})
        self.assertNotIn('private trace', str(result))

    def test_stream_truncation_keeps_header_request_id_and_reasoning_usage_through_transport(self):
        body = ('data: ' + json.dumps({'id': 'gen_stream', 'provider': 'Example Provider',
            'model': 'vendor/actual-model', 'choices': [{'delta': {'content': '{"action":"hold"}'},
                'finish_reason': 'length', 'native_finish_reason': 'MAX_TOKENS'}],
            'usage': {'completion_tokens': 50, 'completion_tokens_details': {'reasoning_tokens': 45}}})
            + '\n\ndata: [DONE]\n\n').encode()
        wire = ('HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                'X-Request-Id: req_stream\r\nContent-Length: ' + str(len(body)) + '\r\n\r\n').encode() + body

        class WireSocket:
            def settimeout(self, value):
                pass
            def sendall(self, value):
                pass
            def makefile(self, mode):
                return io.BytesIO(wire)
            def shutdown(self, how):
                pass
            def close(self):
                pass

        provider = {**self.provider, 'base_url': 'http://127.0.0.1:1234/v1'}
        with patch.object(ai, '_resolve_address', return_value='127.0.0.1'), \
                patch.object(ai.socket, 'create_connection', return_value=WireSocket()), \
                self.assertRaises(ai.ProviderError) as caught:
            ai.complete(provider, [{'role': 'user', 'content': 'Test'}])
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics['error_code'], 'output_truncated')
        self.assertEqual(diagnostics['request_id'], 'req_stream')
        self.assertEqual(diagnostics['provider'], 'Example Provider')
        self.assertEqual(diagnostics['model'], 'vendor/actual-model')
        self.assertEqual(diagnostics['reasoning_tokens'], 45)
        self.assertEqual(diagnostics['received_bytes'], len(body))

    def test_allowlist_rejects_non_numeric_counts_and_arbitrary_reasons(self):
        diagnostics = ai._safe_diagnostics({'reasoning_tokens': True, 'answer_chars': '10',
            'finish_reason': ['length'], 'native_finish_reason': {'secret': 'private'},
            'error_code': 'unknown private reason', 'request_id': 'req\nprivate',
            'model': 'model\nprivate', 'provider': 'Example Provider', 'has_reasoning': 'true',
            'reasoning': 'private trace'})
        self.assertEqual(diagnostics, {'provider': 'Example Provider'})
        self.assertEqual(ai._safe_usage({'prompt_tokens': True, 'completion_tokens': -1,
            'total_tokens': float('inf'), 'completion_tokens_details': {'reasoning_tokens': False}}), {})


if __name__ == '__main__':
    unittest.main()
