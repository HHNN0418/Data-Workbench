"""Server-owned AI connections with cancellable completion waits and SSE progress.

Only public connection settings go into database backups. API keys live in the
separate secret directory: Windows uses current-user DPAPI; other platforms use
owner-only files. A server may alternatively supply CTN_AI_KEY_<UUID_HEX>.
"""
from __future__ import annotations

import ctypes
import codecs
from contextlib import contextmanager, nullcontext
from datetime import timezone
from email.utils import parsedate_to_datetime
import http.client
import ipaddress
import json
import math
import os
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import llm_gateway
from .db import begin_write, connect, initialize


SETTINGS_KEY = 'ai_provider_settings_v1'
REQUEST_TIMEOUT = 45
# Operational default for DNS/TCP/TLS/request setup, adjustable per connection.
# This is a local policy, not a service-provider or model-generation limit.
CONNECTION_TIMEOUT = 60
MAX_RESPONSE_BYTES = 2_000_000
_LOCK = threading.RLock()
_PUBLIC_FIELDS = ('id', 'name', 'base_url', 'model', 'protocol', 'enabled', 'token_parameter',
                  'thinking_mode', 'max_output_tokens', 'competition_tier', 'connection_timeout_seconds',
                  'provider_order')
_PUBLIC_DEFAULTS = {'token_parameter': 'max_tokens', 'thinking_mode': 'auto', 'max_output_tokens': None,
                    'competition_tier': 'standard', 'connection_timeout_seconds': CONNECTION_TIMEOUT,
                    'provider_order': ()}
# OpenRouter upstreams, tried in this order and no others (D80), e.g. ['morph', 'relace'].
# https://openrouter.ai/docs/guides/routing/provider-selection
PROVIDER_ORDER_LIMIT = 6
_PROVIDER_SLUG = re.compile(r'[a-z0-9][a-z0-9._-]{0,40}(?:/[a-z0-9._-]{1,40}){0,3}')


class ProviderError(Exception):
    """A client-safe error that deliberately omits upstream error bodies."""

    def __init__(self, message, status_code=502, *, diagnostics=None):
        super().__init__(message)
        self.status_code = status_code
        self.diagnostics = _safe_diagnostics(diagnostics)


_TRANSPORT_STAGES = {'dns': '解析服务地址', 'connect': '连接服务', 'tls': '建立安全连接',
                     'send': '发送请求', 'headers': '等待响应', 'body': '接收回答',
                     'thinking': '模型正在思考', 'receiving': '正在接收回答',
                     'request': '等待模型请求完成', 'complete': '回答已完成'}
_FINISH_REASONS = {'stop', 'length', 'content_filter', 'tool_calls', 'function_call',
                   'error', 'insufficient_system_resource', 'aborted'}
_NATIVE_FINISH_REASONS = _FINISH_REASONS | {'STOP', 'MAX_TOKENS', 'CONTINUATION',
    'SAFETY', 'RECITATION', 'OTHER', 'MALFORMED_FUNCTION_CALL', 'end_turn',
    'max_tokens', 'stop_sequence', 'pause_turn', 'refusal', 'model_length'}
_TRUNCATED_NATIVE_REASONS = {'MAX_TOKENS', 'CONTINUATION', 'max_tokens', 'model_length'}
_TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens')
REASONING_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high')


def _safe_diagnostics(value):
    """Only bounded identifiers, fixed reasons and counts reach public errors."""
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get('stage') in _TRANSPORT_STAGES:
        result['stage'] = value['stage']
    for key in ('elapsed_seconds', 'timeout_seconds', 'stage_elapsed_seconds', 'received_bytes',
                'dns_seconds', 'connect_seconds', 'tls_seconds', 'send_seconds', 'local_progress_seconds',
                *_TOKEN_FIELDS, 'answer_chars'):
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number) and number >= 0:
            result[key] = round(number, 3)
    request_id = value.get('request_id')
    if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}', request_id):
        result['request_id'] = request_id
    for key in ('wait_for_completion', 'has_reasoning'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key, allowed in (('finish_reason', _FINISH_REASONS),
                         ('native_finish_reason', _NATIVE_FINISH_REASONS),
                         ('error_code', {'output_truncated', 'stream_interrupted', 'reasoning_only',
                                         'in_flight_budget', 'payment_required', 'invalid_json'})):
        if isinstance(value.get(key), str) and value[key] in allowed:
            result[key] = value[key]
    for key in ('model', 'provider'):
        label = value.get(key)
        if isinstance(label, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/+()\-]{0,199}', label):
            result[key] = label
    return result


def _safe_usage(value):
    """Retain numeric reasoning, cache and cost usage, never provider text or reasoning traces.

    https://openrouter.ai/docs/features/prompt-caching (prompt_tokens_details)
    """
    if not isinstance(value, dict):
        return {}
    result = {key: number for key in _TOKEN_FIELDS
              if type(number := value.get(key)) is int and number >= 0}
    details = value.get('completion_tokens_details')
    if isinstance(details, dict):
        number = details.get('reasoning_tokens')
        if type(number) is int and number >= 0:
            result['reasoning_tokens'] = number
    # Streamed usage is sanitized twice (aggregator, then complete), so the
    # flattened cache counts must survive a second pass.
    details = value.get('prompt_tokens_details')
    details = details if isinstance(details, dict) else {}
    for key in ('cached_tokens', 'cache_write_tokens'):
        number = details.get(key, value.get(key))
        if type(number) is int and number >= 0:
            result[key] = number
    cost = value.get('cost')
    if type(cost) in (int, float) and math.isfinite(cost) and 0 <= cost < 1000:
        result['cost'] = float(cost)
    return result


class _TransportTimeout(TimeoutError):
    def __init__(self, diagnostics):
        super().__init__('AI transport deadline exceeded')
        self.diagnostics = _safe_diagnostics(diagnostics)


class _TransportCancelled(Exception):
    def __init__(self, diagnostics=None):
        super().__init__('AI request cancelled')
        self.diagnostics = _safe_diagnostics(diagnostics)


def _notify_progress(callback, progress):
    if callback is not None:
        try:
            callback(_safe_diagnostics(progress))
        except Exception:
            # UI progress must never change the outcome of a model request.
            pass


def _retry_after_seconds(value):
    """Keep only a bounded delay; never retain an upstream header verbatim."""
    if not isinstance(value, str):
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0, date.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return min(delay, 604800) if math.isfinite(delay) and delay >= 0 else None


class _SSECompletion:
    """Aggregate one Chat Completions stream without retaining reasoning text."""

    def __init__(self, on_stage=None):
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.buffer = ''
        self.data_lines = []
        self.content = []
        self.has_reasoning = False
        self.refusal = False
        self.tool_calls = False
        self.finish_reason = None
        self.native_finish_reason = None
        self.metadata = {}
        self.done = False
        self.usage = {}
        self.on_stage = on_stage

    def _event(self):
        if not self.data_lines:
            return
        data = '\n'.join(self.data_lines)
        self.data_lines = []
        if data == '[DONE]':
            self.done = True
            return
        if self.done:
            raise ProviderError('AI 流式回答在结束后仍返回数据，未接受不完整结果', 502)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('invalid SSE event')
        if value.get('error'):
            raise ProviderError('AI 服务在流式回答中返回错误，请稍后重试', 502)
        self.metadata.update(_safe_diagnostics({
            'request_id': value.get('id'), 'model': value.get('model'),
            'provider': value.get('provider')}))
        usage = value.get('usage')
        if isinstance(usage, dict):
            self.usage.update(_safe_usage(usage))
        choices = value.get('choices')
        if choices is None or choices == []:
            return
        if not isinstance(choices, list):
            raise ValueError('invalid SSE choices')
        choice = next((item for item in choices if isinstance(item, dict) and item.get('index', 0) == 0), None)
        if choice is None:
            return
        delta = choice.get('delta') or {}
        if not isinstance(delta, dict):
            raise ValueError('invalid SSE delta')
        reasoning = delta.get('reasoning_content') or delta.get('reasoning')
        if (isinstance(reasoning, str) and reasoning) or (
                isinstance(delta.get('reasoning_details'), list) and delta['reasoning_details']):
            self.has_reasoning = True
            if self.on_stage is not None:
                self.on_stage('thinking')
        content = delta.get('content')
        if content is not None:
            if not isinstance(content, str):
                raise ValueError('invalid SSE content')
            if content:
                self.content.append(content)
                if self.on_stage is not None:
                    self.on_stage('receiving')
        self.refusal = self.refusal or bool(delta.get('refusal'))
        self.tool_calls = self.tool_calls or bool(delta.get('tool_calls'))
        finish = choice.get('finish_reason')
        if finish is not None:
            if not isinstance(finish, str) or not finish:
                raise ValueError('invalid SSE finish reason')
            self.finish_reason = finish
        native_finish = _safe_diagnostics({'native_finish_reason': choice.get('native_finish_reason')})
        if native_finish:
            self.native_finish_reason = native_finish['native_finish_reason']

    def feed(self, chunk, *, final=False):
        self.buffer += self.decoder.decode(chunk, final=final)
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            line = line.removesuffix('\r')
            if not line:
                self._event()
            elif line.startswith('data:'):
                self.data_lines.append(line[5:].lstrip(' '))
            # SSE comments, keep-alives and event/id/retry metadata contain no
            # completion text and must not become part of the final answer.
        if final:
            if self.buffer:
                line, self.buffer = self.buffer.removesuffix('\r'), ''
                if line.startswith('data:'):
                    self.data_lines.append(line[5:].lstrip(' '))
            self._event()

    def result(self):
        self.feed(b'', final=True)
        if not self.done or self.finish_reason is None:
            raise ProviderError('AI 流式连接在回答完成前中断，未保存部分结果；请重新执行', 502,
                diagnostics={**self.metadata, **self.usage, 'error_code': 'stream_interrupted',
                    'finish_reason': self.finish_reason, 'native_finish_reason': self.native_finish_reason,
                    'has_reasoning': self.has_reasoning, 'answer_chars': sum(map(len, self.content))})
        message = {'content': ''.join(self.content), 'has_reasoning': self.has_reasoning}
        if self.refusal:
            message['refusal'] = True
        if self.tool_calls:
            message['tool_calls'] = True
        choice = {'message': message, 'finish_reason': self.finish_reason}
        if self.native_finish_reason is not None:
            choice['native_finish_reason'] = self.native_finish_reason
        return {'choices': [choice], 'usage': self.usage, **self.metadata}


def _provider_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, AttributeError, TypeError):
        raise ValueError('AI 连接 ID 无效') from None


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{label}不能为空，且不能超过 {maximum} 个字符')
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{label}包含无效字符')
    return value


def _loopback_host(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_address(address):
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)


def _validate_url(value):
    value = _text(value, 'API 地址', 2000)
    try:
        parts = urlsplit(value)
        host = (parts.hostname or '').lower()
        port = parts.port
        if (parts.scheme not in ('https', 'http') or not host or
                parts.username is not None or parts.password is not None or
                parts.query or parts.fragment or '\\' in value or '%' in parts.netloc or
                not value.isascii() or (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        local = _loopback_host(host)
        if parts.scheme != 'https' and not local:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (host != 'localhost' and ('.' not in host or host.endswith(
                    ('.localhost', '.local', '.internal', '.home', '.lan')))):
                raise ValueError
            if not re.fullmatch(r'[a-z0-9.-]+', host):
                raise ValueError
        else:
            if not (_public_address(address) or address.is_loopback):
                raise ValueError
        # Accept a base URL or a copied full supported endpoint.
        path = parts.path.rstrip('/')
        for suffix in ('/chat/completions', '/embeddings', '/rerank'):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        return urlunsplit((parts.scheme, parts.netloc.lower(), path, '', ''))
    except (ValueError, UnicodeError):
        raise ValueError('API 地址须为公网 HTTPS 地址，或明确的 localhost / 回环地址；不能含凭据、查询参数或片段') from None


def _dpapi(data, decrypt=False):
    """Use the logged-in Windows identity; never prompt for a password."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ProviderError('无法保护或读取 API Key，请在运行服务的账户下重新保存', 400)
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _protect(key):
    raw = key.encode('utf-8')
    return b'DPAPI1\n' + _dpapi(raw) if os.name == 'nt' else b'PRIVATE1\n' + raw


def _unprotect(raw):
    if raw.startswith(b'DPAPI1\n') and os.name == 'nt':
        return _dpapi(raw[7:], decrypt=True).decode('utf-8')
    if raw.startswith(b'PRIVATE1\n') and os.name != 'nt':
        return raw[9:].decode('utf-8')
    raise ProviderError('API Key 无法由当前服务账户读取，请重新保存', 400)


def _validate_key(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError('API Key 格式无效')
    value = value.strip()
    if value and (not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError('API Key 格式无效')
    return value


def _connection_timeout(value):
    if (type(value) not in (int, float) or not 5 <= value <= 300
            or not math.isfinite(value) or value != int(value)):
        raise ValueError('连接准备时限须为 5–300 秒的整数')
    return int(value)


def _provider_order(value, base_url):
    """OpenRouter provider slugs to try in order; empty keeps OpenRouter's own routing."""
    if value in (None, ''):
        return []
    if (not isinstance(value, (list, tuple)) or len(value) > PROVIDER_ORDER_LIMIT
            or not all(isinstance(slug, str) and _PROVIDER_SLUG.fullmatch(slug) for slug in value)
            or len(set(value)) != len(value)):
        raise ValueError(f'上游提供商顺序须为最多 {PROVIDER_ORDER_LIMIT} 个不重复的 OpenRouter 提供商标识')
    if value and urlsplit(base_url).hostname != 'openrouter.ai':
        raise ValueError('只有 OpenRouter 连接可以指定上游提供商顺序')
    return list(value)


class ProviderStore:
    """Manage public settings and private credentials as separate resources."""

    def __init__(self, db_path, secret_dir):
        self.db_path = db_path
        self.secret_dir = Path(secret_dir).resolve()
        initialize(db_path)

    def _path(self, provider_id):
        return self.secret_dir / (_provider_id(provider_id) + '.key')

    def _environment_key(self, provider_id):
        return os.environ.get('CTN_AI_KEY_' + uuid.UUID(provider_id).hex.upper(), '')

    def _read(self, conn):
        row = conn.execute('SELECT value FROM meta WHERE key=?', (SETTINGS_KEY,)).fetchone()
        if not row:
            return {'providers': [], 'default_provider_id': ''}
        try:
            data = json.loads(row['value'])
            if not isinstance(data, dict) or not isinstance(data['providers'], list):
                raise ValueError
            # An allowlist ensures even imported metadata cannot disclose keys.
            providers = [{key: item.get(key, _PUBLIC_DEFAULTS[key]) if key in _PUBLIC_DEFAULTS else item[key]
                          for key in _PUBLIC_FIELDS} for item in data['providers']]
            for provider in providers:
                _provider_id(provider['id'])
                provider['connection_timeout_seconds'] = _connection_timeout(provider['connection_timeout_seconds'])
                # Older versions inserted restrictive defaults even when the
                # member had never chosen a limit. Do not silently carry those
                # budgets or disabled reasoning into the new completion policy.
                provider['max_output_tokens'] = None
                if provider['thinking_mode'] == 'disabled':
                    provider['thinking_mode'] = 'auto'
                provider['provider_order'] = list(provider['provider_order'] or ())
            return {'providers': providers, 'default_provider_id': data.get('default_provider_id', '')}
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderError('AI 连接设置无法读取，请检查数据库备份', 500) from None

    def _write(self, conn, data):
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (SETTINGS_KEY, json.dumps(data, ensure_ascii=False)))

    def _public(self, data):
        providers = []
        for item in data['providers']:
            provider = {key: item[key] for key in _PUBLIC_FIELDS}
            provider['has_key'] = bool(self._environment_key(item['id'])) or self._path(item['id']).is_file()
            provider['is_default'] = item['id'] == data['default_provider_id']
            providers.append(provider)
        return {'providers': providers, 'default_provider_id': data['default_provider_id']}

    def list_public(self):
        with _LOCK:
            conn = connect(self.db_path)
            try:
                return self._public(self._read(conn))
            finally:
                conn.close()

    def _atomic_secret(self, path, raw):
        self.secret_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            self.secret_dir.chmod(0o700)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix='.ai-', suffix='.tmp', dir=self.secret_dir)
            with os.fdopen(descriptor, 'wb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def save(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('AI 连接设置格式无效')
        with _LOCK:
            conn = connect(self.db_path)
            changed_path = None
            old_secret = None
            try:
                begin_write(conn, 'ai_providers')
                data = self._read(conn)
                providers = data['providers']
                default_only = set(payload) == {'default_provider_id'}
                if default_only:
                    chosen = payload['default_provider_id']
                    if not any(item['id'] == chosen and item['enabled'] for item in providers):
                        raise ValueError('默认 AI 连接必须已启用')
                    data['default_provider_id'] = chosen
                else:
                    provider_id = _provider_id(payload['id']) if payload.get('id') else str(uuid.uuid4())
                    old = next((item for item in providers if item['id'] == provider_id), None)
                    if payload.get('id') and old is None:
                        raise ProviderError('AI 连接不存在', 404)
                    if old is None and len(providers) >= 25:
                        raise ValueError('最多保存 25 个 AI 连接')
                    merged = dict(old or {})
                    merged.update(payload)
                    provider = {'id': provider_id,
                                'name': _text(merged.get('name'), '连接名称', 80),
                                'base_url': _validate_url(merged.get('base_url')),
                                'model': _text(merged.get('model'), '模型名称', 200),
                                'protocol': merged.get('protocol', 'openai_chat'),
                                'enabled': merged.get('enabled', True)}
                    provider['token_parameter'] = merged.get('token_parameter') or (
                        'max_completion_tokens' if urlsplit(provider['base_url']).hostname == 'api.openai.com'
                        else 'max_tokens')
                    provider['thinking_mode'] = merged.get('thinking_mode', 'auto')
                    # Accept old clients' disabled value without turning off
                    # reasoning; the provider chooses a supported default.
                    if provider['thinking_mode'] == 'disabled':
                        provider['thinking_mode'] = 'auto'
                    provider['max_output_tokens'] = None
                    provider['competition_tier'] = merged.get('competition_tier', 'standard')
                    provider['connection_timeout_seconds'] = _connection_timeout(
                        merged.get('connection_timeout_seconds', CONNECTION_TIMEOUT))
                    # Kept from the saved connection when an edit does not send it.
                    provider['provider_order'] = _provider_order(merged.get('provider_order'), provider['base_url'])
                    if provider['competition_tier'] not in ('standard', 'enhanced', 'premium'):
                        raise ValueError('模型等级只支持 standard/enhanced/premium')
                    if provider['thinking_mode'] not in ('auto', 'enabled'):
                        raise ValueError('思考模式只支持 auto/enabled')
                    if provider['protocol'] not in ('openai_chat', 'typesafe', 'openai_embeddings', 'rerank'):
                        raise ValueError('AI 协议须为聊天、TypeSafe、向量化或重排接口')
                    if provider['protocol'] == 'typesafe' and provider['base_url'] != 'https://api.typesafe.ai/v1':
                        raise ValueError('TypeSafe Jev 地址须为 https://api.typesafe.ai/v1')
                    if provider['token_parameter'] not in ('max_tokens', 'max_completion_tokens'):
                        raise ValueError('输出上限参数无效')
                    if type(provider['enabled']) is not bool or (
                            'is_default' in payload and type(payload['is_default']) is not bool):
                        raise ValueError('启用和默认设置必须为布尔值')
                    if payload.get('is_default') and not provider['enabled']:
                        raise ValueError('默认 AI 连接必须已启用')
                    key = _validate_key(payload.get('api_key', ''))
                    if old:
                        providers[providers.index(old)] = provider
                    else:
                        providers.append(provider)
                    if payload.get('is_default'):
                        data['default_provider_id'] = provider_id
                    if not any(item['id'] == data['default_provider_id'] and item['enabled'] for item in providers):
                        data['default_provider_id'] = next((item['id'] for item in providers if item['enabled']), '')
                    if key:
                        path = self._path(provider_id)
                        old_secret = path.read_bytes() if path.is_file() else None
                        protected = _protect(key)
                        self._atomic_secret(path, protected)
                        changed_path = path
                self._write(conn, data)
                conn.commit()
                changed_path = None
                return self._public(data)
            except Exception as exc:
                conn.rollback()
                if changed_path is not None:
                    try:
                        if old_secret is None:
                            changed_path.unlink(missing_ok=True)
                        else:
                            self._atomic_secret(changed_path, old_secret)
                    except OSError:
                        raise ProviderError('API Key 保存失败，请重新保存该连接', 500) from None
                if isinstance(exc, (ValueError, ProviderError)):
                    raise
                raise ProviderError('AI 连接设置保存失败，请检查服务存储权限', 500) from None
            finally:
                conn.close()

    def delete(self, provider_id):
        provider_id = _provider_id(provider_id)
        with _LOCK:
            conn = connect(self.db_path)
            old_secret = None
            removed = False
            try:
                begin_write(conn, 'ai_providers')
                data = self._read(conn)
                if not any(item['id'] == provider_id for item in data['providers']):
                    raise ProviderError('AI 连接不存在', 404)
                data['providers'] = [item for item in data['providers'] if item['id'] != provider_id]
                if data['default_provider_id'] == provider_id:
                    data['default_provider_id'] = next((item['id'] for item in data['providers'] if item['enabled']), '')
                # Delete while holding the shared DB write lock so a concurrent
                # get cannot pair the old setting with a newly saved credential.
                path = self._path(provider_id)
                old_secret = path.read_bytes() if path.is_file() else None
                path.unlink(missing_ok=True)
                removed = True
                self._write(conn, data)
                conn.commit()
                removed = False
                return self._public(data)
            except Exception as exc:
                conn.rollback()
                if removed and old_secret is not None:
                    try:
                        self._atomic_secret(path, old_secret)
                    except OSError:
                        raise ProviderError('API Key 删除失败，请重新保存该连接', 500) from None
                if isinstance(exc, (ValueError, ProviderError)):
                    raise
                raise ProviderError('AI 连接删除失败，请检查服务存储权限', 500) from None
            finally:
                conn.close()

    def get(self, provider_id=''):
        """Internal only: returns the key for server-side transport."""
        with _LOCK:
            conn = connect(self.db_path)
            try:
                # The DB transaction also serializes credentials with writers
                # in another server process sharing this settings database.
                begin_write(conn, 'ai_providers')
                data = self._read(conn)
                selected = provider_id or data['default_provider_id']
                provider = next((dict(item) for item in data['providers'] if item['id'] == selected), None)
                if provider is None:
                    raise ProviderError('请先保存并选择一个 AI 连接', 400)
                if not provider['enabled']:
                    raise ProviderError('该 AI 连接已停用', 400)
                key = self._environment_key(provider['id'])
                if not key:
                    path = self._path(provider['id'])
                    if path.is_file():
                        try:
                            key = _unprotect(path.read_bytes())
                        except (OSError, UnicodeError):
                            raise ProviderError('API Key 无法读取，请重新保存', 400) from None
                provider['api_key'] = _validate_key(key)
                if not key and not _loopback_host(urlsplit(provider['base_url']).hostname):
                    raise ProviderError('该 AI 连接尚未配置 API Key', 400)
                return provider
            finally:
                conn.close()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _resolve_address(host, port, deadline, *, cancellation_event=None):
    """Bound DNS waiting, then pin the checked address to prevent rebinding."""
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    while True:
        if cancellation_event is not None and cancellation_event.is_set():
            raise _TransportCancelled
        try:
            addresses = result.get(timeout=min(.1, _remaining(deadline)) if cancellation_event is not None else _remaining(deadline))
            break
        except queue.Empty:
            if time.monotonic() >= deadline:
                raise TimeoutError from None
    if not addresses:
        raise ProviderError('无法解析 AI 服务地址', 502)
    local = _loopback_host(host)
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        if not (address.is_loopback if local else _public_address(address)):
            raise ProviderError('AI 服务地址解析到了不允许访问的网络', 400)
    return addresses[0][4][0]


def _request_json(endpoint, body, headers, *, timeout_seconds=None,
                  wait_for_completion=False, cancellation_event=None, on_progress=None,
                  stream_response=False, idle_timeout_seconds=None, connection_timeout_seconds=None):
    """No proxy inheritance, redirects, retries, or tools; optionally read SSE."""
    started = stage_started = time.monotonic()
    timeout = REQUEST_TIMEOUT if timeout_seconds is None else timeout_seconds
    setup_timeout = _connection_timeout(CONNECTION_TIMEOUT if connection_timeout_seconds is None
        else connection_timeout_seconds) if wait_for_completion else None
    # Generation waits for completion. DNS/connection setup remains
    # bounded independently; waiting for response headers/body has no deadline.
    deadline = started + (setup_timeout if wait_for_completion else timeout)
    parts = urlsplit(endpoint)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    stage = 'dns'
    request_id = None
    connection = None
    sock = None
    received_bytes = 0
    finished = threading.Event()
    request_thread = threading.get_ident()
    timing_lock = threading.Lock()
    timings = {name + '_seconds': 0.0 for name in ('dns', 'connect', 'tls', 'send')}
    timings['local_progress_seconds'] = 0.0
    active_timing = None

    @contextmanager
    def timed_operation(name):
        nonlocal active_timing
        began = time.monotonic()
        with timing_lock:
            active_timing = (name + '_seconds', began)
        try:
            yield
        finally:
            with timing_lock:
                timings[name + '_seconds'] += max(0, time.monotonic() - began)
                active_timing = None

    def report_progress():
        if on_progress is None:
            return
        # Concurrent watcher notifications run outside the request path. Their
        # time must never be counted twice as local work or subtracted from a
        # DNS/socket operation that is still in progress on the request thread.
        main_thread = threading.get_ident() == request_thread
        began = time.monotonic() if main_thread else None
        try:
            _notify_progress(on_progress, measurements())
        finally:
            if main_thread:
                with timing_lock:
                    timings['local_progress_seconds'] += max(0, time.monotonic() - began)

    def enter(name):
        nonlocal stage, stage_started
        if stage == name:
            return
        stage, stage_started = name, time.monotonic()
        report_progress()

    def measurements():
        now = time.monotonic()
        with timing_lock:
            measured = dict(timings)
            if active_timing is not None:
                key, began = active_timing
                measured[key] += max(0, now - began)
        return _safe_diagnostics({'stage': stage, 'elapsed_seconds': max(0, now - started),
            'stage_elapsed_seconds': max(0, now - stage_started),
            'timeout_seconds': setup_timeout if wait_for_completion and deadline is not None else timeout if deadline is not None else None,
            'request_id': request_id, 'received_bytes': received_bytes,
            'wait_for_completion': wait_for_completion, **measured})

    def check_cancelled():
        if cancellation_event is not None and cancellation_event.is_set():
            raise _TransportCancelled(measurements())

    def stop_socket():
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def watch_request():
        last_progress = time.monotonic()
        while not finished.wait(.1):
            if cancellation_event is not None and cancellation_event.is_set():
                stop_socket()
                return
            current_deadline = deadline
            if current_deadline is not None and time.monotonic() >= current_deadline:
                stop_socket()
                return
            if time.monotonic() - last_progress >= 5:
                report_progress()
                last_progress = time.monotonic()

    watcher = threading.Thread(target=watch_request, daemon=True)
    try:
        check_cancelled()
        report_progress()
        check_cancelled()
        if wait_for_completion:
            # Initial local progress reporting is not DNS/network setup. Start
            # its budget and watchdog together after that notification, so a
            # slow callback cannot consume the budget before DNS even starts
            # or make the watchdog exit before a socket exists.
            stage_started = time.monotonic()
            deadline = stage_started + setup_timeout
        watcher.start()
        options = {'cancellation_event': cancellation_event} if cancellation_event is not None else {}
        with timed_operation('dns'):
            address = _resolve_address(parts.hostname, port, deadline, **options)
        check_cancelled()
        _remaining(deadline)
        enter('connect')
        with timed_operation('connect'):
            connection = http.client.HTTPConnection(parts.hostname, port, timeout=_remaining(deadline))
            sock = socket.create_connection((address, port), timeout=_remaining(deadline))
        check_cancelled()
        if parts.scheme == 'https':
            enter('tls')
            with timed_operation('tls'):
                sock.settimeout(_remaining(deadline))
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname,
                                                               do_handshake_on_connect=False)
                check_cancelled()
                sock.do_handshake()
                check_cancelled()
        connection.sock = sock
        enter('send')
        with timed_operation('send'):
            sock.settimeout(_remaining(deadline))
            connection.request('POST', parts.path, body=body, headers=headers)
        check_cancelled()
        if wait_for_completion:
            deadline = None
        enter('headers')
        # Quiet reasoning and delayed first tokens are valid. Once the request
        # is sent, only explicit cancellation or a network failure stops it.
        sock.settimeout(None if deadline is None else _remaining(deadline))
        response = connection.getresponse()
        check_cancelled()
        get_header = getattr(response, 'getheader', None)
        content_type = ''
        if callable(get_header):
            request_id = get_header('x-request-id') or get_header('request-id')
            supplied_type = get_header('Content-Type')
            content_type = supplied_type.split(';', 1)[0].strip().lower() if isinstance(supplied_type, str) else ''
            # An upstream header is untrusted and must not echo credentials.
            if not isinstance(request_id, str) or any(
                    secret and secret in request_id for secret in (headers.get('Authorization', ''),
                        headers.get('Authorization', '').removeprefix('Bearer '))):
                request_id = None
        if response.status != 200:
            if response.status in (401, 403):
                raise ProviderError('AI 服务拒绝认证，请检查 API Key 和模型权限', 400)
            if response.status == 429:
                error = ProviderError('AI 服务限流或额度不足，请稍后重试', 429)
                delay = _retry_after_seconds(get_header('Retry-After') if callable(get_header) else None)
                if delay is not None:
                    error.retry_after = delay
                raise error
            if response.status == 402:
                # Only an in-flight budget 402 carries Retry-After; it is a
                # transient wait like 429. Without it the credits are exhausted.
                # https://openrouter.ai/docs/api-reference/errors
                delay = _retry_after_seconds(get_header('Retry-After') if callable(get_header) else None)
                if delay is None:
                    raise ProviderError('AI 服务账户额度不足（402），请检查服务商余额', 402,
                                        diagnostics={'error_code': 'payment_required'})
                error = ProviderError('AI 服务在途额度暂满，请稍后重试', 429,
                                      diagnostics={'error_code': 'in_flight_budget'})
                error.retry_after = delay
                raise error
            if 300 <= response.status < 400:
                raise ProviderError('AI 服务返回重定向，请填写最终 API 地址', 400)
            raise ProviderError('AI 服务请求失败，请检查服务地址、模型和服务状态', 502)
        chunks = []
        size = 0
        enter('body')
        # Some compatible services ignore stream:true but explicitly return a
        # complete JSON response. Read that one response without retrying.
        stream = _SSECompletion(on_stage=enter) if stream_response and content_type != 'application/json' else None
        while True:
            check_cancelled()
            sock.settimeout(None if deadline is None else _remaining(deadline))
            # Generations end at the provider's completion marker, never at a
            # local byte budget. Non-generating retrieval responses stay bounded.
            limit_bytes = None if wait_for_completion else MAX_RESPONSE_BYTES
            chunk = response.read1(65536 if limit_bytes is None else min(65536, limit_bytes + 1 - size))
            check_cancelled()
            if not chunk:
                break
            size += len(chunk)
            received_bytes = size
            report_progress()
            if limit_bytes is not None and size > limit_bytes:
                raise ProviderError('AI 服务响应超过大小限制', 502)
            if stream is not None and not content_type and size == len(chunk) and chunk.lstrip().startswith(b'{'):
                # Some compatible servers omit Content-Type while returning
                # ordinary JSON despite stream:true. Consume this same request.
                stream = None
            if stream is not None:
                stream.feed(chunk)
                if stream.done:
                    break
            else:
                chunks.append(chunk)
        if deadline is not None:
            _remaining(deadline)
        check_cancelled()
        result = stream.result() if stream is not None else json.loads(b''.join(chunks))
        if isinstance(result, dict):
            result['_diagnostics'] = measurements()
        return result
    except _TransportCancelled:
        raise _TransportCancelled(measurements()) from None
    except ProviderError as exc:
        exc.diagnostics = _safe_diagnostics({**measurements(), **exc.diagnostics})
        raise
    except (TimeoutError, socket.timeout):
        check_cancelled()
        raise _TransportTimeout(measurements()) from None
    except (OSError, http.client.HTTPException):
        check_cancelled()
        if deadline is not None and time.monotonic() >= deadline:
            raise _TransportTimeout(measurements()) from None
        if stream_response and stage in ('body', 'thinking', 'receiving'):
            raise ProviderError('AI 流式连接在回答完成前中断，未保存部分结果；请重新执行',
                                502, diagnostics=measurements()) from None
        raise
    except (ValueError, UnicodeError):
        raise ProviderError('AI 服务返回了无法识别的响应', 502, diagnostics=measurements()) from None
    finally:
        finished.set()
        if connection is not None:
            connection.close()
        if sock is not None:
            sock.close()


def _json_object(text):
    """True when a structured answer (optionally fenced) parses to a JSON object."""
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', str(text or '').strip()).strip()
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _completion_text(choice, *, reasoning_always_on=False, diagnostics=None, output_cap=None):
    if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
        raise ProviderError('AI 服务返回了无法识别的响应', 502)
    message = choice['message']
    finish = choice.get('finish_reason')
    native_finish = choice.get('native_finish_reason')
    content = message.get('content')
    has_reasoning = (message.get('has_reasoning') is True or
        _safe_diagnostics(diagnostics).get('reasoning_tokens', 0) > 0 or
        any(isinstance(message.get(key), str) and bool(message[key].strip())
            for key in ('reasoning_content', 'reasoning')) or
        isinstance(message.get('reasoning_details'), list) and bool(message['reasoning_details']))
    measurements = _safe_diagnostics({**(diagnostics or {}), 'finish_reason': finish,
        'native_finish_reason': native_finish, 'has_reasoning': bool(has_reasoning),
        'answer_chars': len(content) if isinstance(content, str) else sum(
            len(part['text']) for part in content if isinstance(part, dict)
            and isinstance(part.get('text'), str)) if isinstance(content, list) else 0})
    if finish == 'length' or isinstance(native_finish, str) and native_finish in _TRUNCATED_NATIVE_REASONS:
        measurements['error_code'] = 'output_truncated'
        if reasoning_always_on:
            raise ProviderError('AI 服务的输出预算已耗尽，最终回答未完成；此 Grok 模型不能关闭思考，请检查服务商模型限制', 502,
                                diagnostics=measurements)
        if output_cap:
            raise ProviderError(f'AI 服务的输出预算已耗尽，最终回答未完成；本次调用的输出上限为 {output_cap} token', 502,
                                diagnostics=measurements)
        raise ProviderError('AI 服务的输出预算已耗尽，最终回答未完成；本应用未设置输出上限，请检查服务商模型限制', 502,
                            diagnostics=measurements)
    if finish == 'content_filter' or message.get('refusal'):
        raise ProviderError('AI 服务拒绝回答或过滤了内容，请调整请求', 502, diagnostics=measurements)
    if finish == 'tool_calls' or message.get('tool_calls'):
        raise ProviderError('AI 服务返回了工具调用，当前功能需要直接文字回答', 502, diagnostics=measurements)
    if finish in ('insufficient_system_resource', 'aborted', 'error'):
        raise ProviderError('AI 服务中断了生成，请稍后重新执行', 502, diagnostics=measurements)
    if isinstance(content, list):
        if any(isinstance(part, dict) and part.get('type') == 'refusal' for part in content):
            raise ProviderError('AI 服务拒绝回答或过滤了内容，请调整请求', 502, diagnostics=measurements)
        content = '\n'.join(part['text'] for part in content if isinstance(part, dict)
                            and part.get('type') in ('text', 'output_text') and isinstance(part.get('text'), str))
    if not isinstance(content, str) or not content.strip():
        if has_reasoning:
            measurements['error_code'] = 'reasoning_only'
            if reasoning_always_on:
                raise ProviderError('AI 服务只返回了推理过程，尚无最终回答；此 Grok 模型不能关闭思考，请检查服务商模型限制', 502,
                                    diagnostics=measurements)
            raise ProviderError('AI 服务只返回了推理过程，尚无最终回答；请检查服务商模型限制', 502,
                                diagnostics=measurements)
        raise ProviderError('AI 服务未返回最终文字回答，请检查模型和接口协议设置', 502, diagnostics=measurements)
    return content


def complete(provider, messages, max_output_tokens=None, *, timeout_seconds=None, response_schema=None,
             wait_for_completion=True, cancellation_event=None, on_progress=None,
             idle_timeout_seconds=None, max_tokens=None, session_id=None, retry_budget_seconds=0,
             reasoning_effort=None, ledger=None):
    """Make one cancellable completion request without a generation deadline.

    Legacy output-budget and timeout/wait arguments remain accepted but cannot
    shorten generation, including quiet reasoning; a connection never sets an
    output cap. Only an explicit per-purpose ``max_tokens`` (see
    llm_calls.request_policy) is sent, which also bounds OpenRouter's in-flight
    credit hold. ``retry_budget_seconds`` lets a request that was rejected
    before generation (429, or a 402 in-flight budget wait) be sent again after
    its Retry-After. ``session_id`` pins OpenRouter's sticky cache routing.
    ``reasoning_effort`` overrides the connection's thinking mode for one
    OpenRouter request (light steps such as reading, writing and checking).
    ``ledger`` records one llm_calls row for the request, failures included.
    DNS/connection setup has a separate per-provider limit (local default 60
    seconds, configurable from 5 to 300); model reasoning/receiving has none.
    Schema: https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
    """
    trace = {'attempts': 0, 'wait_seconds': 0.0, 'queue_seconds': 0.0}
    started = time.time()
    try:
        for json_attempt in (1, 2):
            try:
                # Recorded requests share one admission gate (llm_gateway, S4).
                with (llm_gateway.GATE.slot(ledger.get('purpose'), ledger.get('agent_id'), cancellation_event)
                      if isinstance(ledger, dict) else nullcontext(0.0)) as queued:
                    trace['queue_seconds'] += queued
                    result = _complete(provider, messages, timeout_seconds=timeout_seconds,
                                       response_schema=response_schema, wait_for_completion=wait_for_completion,
                                       cancellation_event=cancellation_event, on_progress=on_progress,
                                       max_tokens=max_tokens, session_id=session_id,
                                       retry_budget_seconds=retry_budget_seconds,
                                       reasoning_effort=reasoning_effort,
                                       trace=trace if ledger is not None else None)
                break
            except ProviderError as exc:
                # One fresh request when an upstream broke the structured output.
                if json_attempt == 2 or exc.diagnostics.get('error_code') != 'invalid_json':
                    raise
    except llm_gateway.Cancelled:
        raise ProviderError('AI 请求已取消，未创建发布动作', 499) from None
    except ProviderError as exc:
        _record_call(ledger, provider, started, trace, max_tokens, session_id, error=exc)
        raise
    _record_call(ledger, provider, started, trace, max_tokens, session_id, usage=result['usage'])
    return result


_ERROR_CODES = {499: 'cancelled', 504: 'timeout', 429: 'rate_limited', 402: 'payment_required',
                400: 'request_rejected'}


def _record_call(ledger, provider, started, trace, max_tokens, session_id, *, usage=None, error=None):
    """One best-effort ledger row once a request was actually attempted."""
    if not isinstance(ledger, dict) or not trace.get('attempts'):
        return
    from . import llm_calls
    diagnostics = error.diagnostics if error is not None else {}
    status = 'ok' if error is None else 'cancelled' if error.status_code == 499 else 'error'
    llm_calls.record(ledger.get('db_path'), llm_calls.row(
        ledger, provider=provider, started=started, finished=time.time(),
        usage=usage if usage is not None else diagnostics, status=status,
        http_status=200 if error is None else error.status_code,
        error_code=None if error is None else diagnostics.get('error_code') or _ERROR_CODES.get(
            error.status_code, 'provider_error'),
        finish_reason=trace.get('finish_reason') or diagnostics.get('finish_reason'),
        upstream=trace.get('upstream'), attempts=trace['attempts'],
        wait_seconds=trace['wait_seconds'] + trace.get('queue_seconds', 0.0),
        ttft_seconds=trace.get('ttft'), max_tokens=max_tokens, session_id=session_id))


def _complete(provider, messages, *, timeout_seconds, response_schema, wait_for_completion, cancellation_event,
              on_progress, max_tokens, session_id, retry_budget_seconds, reasoning_effort, trace):
    if not isinstance(provider, dict) or provider.get('protocol') != 'openai_chat':
        raise ValueError('AI 连接协议无效')
    base_url = _validate_url(provider.get('base_url'))
    setup_timeout = _connection_timeout(provider.get('connection_timeout_seconds', CONNECTION_TIMEOUT))
    model = _text(provider.get('model'), '模型名称', 200)
    api_key = _validate_key(provider.get('api_key', ''))
    if not api_key and not _loopback_host(urlsplit(base_url).hostname):
        raise ProviderError('该 AI 连接尚未配置 API Key', 400)
    if not isinstance(messages, list) or not 1 <= len(messages) <= 50:
        raise ValueError('AI 消息数量须在 1 到 50 之间')
    clean_messages = []
    total = 0
    for message in messages:
        if (not isinstance(message, dict) or message.get('role') not in ('system', 'developer', 'user', 'assistant') or
                not isinstance(message.get('content'), str)):
            raise ValueError('AI 消息格式无效')
        total += len(message['content'])
        clean_messages.append({'role': message['role'], 'content': message['content']})
    if total > 120_000:
        raise ValueError('AI 上下文过长，请减少样本或历史消息')
    if type(wait_for_completion) is not bool:
        raise ValueError('AI 完成等待设置须为布尔值')
    wait_for_completion = True
    if cancellation_event is not None and not callable(getattr(cancellation_event, 'is_set', None)):
        raise ValueError('AI 取消信号无效')
    if on_progress is not None and not callable(on_progress):
        raise ValueError('AI 请求进度回调无效')
    thinking_mode = provider.get('thinking_mode') or 'auto'
    if thinking_mode not in ('auto', 'enabled', 'disabled'):
        raise ValueError('思考模式只支持 auto/enabled/disabled')
    if thinking_mode == 'disabled':
        thinking_mode = 'auto'
    if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 1_000_000):
        raise ValueError('AI 输出上限须为 1–1000000 的整数')
    if session_id is not None and (not isinstance(session_id, str) or not 0 < len(session_id) <= 256
                                   or not session_id.isascii() or not session_id.isprintable()):
        raise ValueError('AI 会话标识无效')
    if (type(retry_budget_seconds) not in (int, float) or not math.isfinite(retry_budget_seconds)
            or not 0 <= retry_budget_seconds <= 600):
        raise ValueError('AI 重试等待预算须为 0–600 秒')
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError('AI 思考强度只支持 none/minimal/low/medium/high')
    payload = {'model': model, 'messages': clean_messages, 'stream': True}
    if max_tokens is not None:
        parameter = provider.get('token_parameter')
        payload[parameter if parameter in ('max_tokens', 'max_completion_tokens') else 'max_tokens'] = max_tokens
    if session_id is not None and urlsplit(base_url).hostname == 'openrouter.ai':
        # https://openrouter.ai/docs/features/prompt-caching (sticky routing)
        payload['session_id'] = session_id
    if response_schema is not None:
        if not isinstance(response_schema, dict) or len(json.dumps(response_schema, allow_nan=False)) > 20000:
            raise ValueError('AI 响应结构无效或超过大小限制')
        payload['response_format'] = {'type': 'json_schema', 'json_schema': {
            'name': 'structured_response', 'strict': True, 'schema': response_schema}}
        if urlsplit(base_url).hostname == 'openrouter.ai':
            # Do not route structured requests to an upstream that silently
            # ignores response_format. If none supports it, fail explicitly
            # so the reading pipeline can use its fallback and retry later.
            # https://openrouter.ai/docs/guides/features/structured-outputs
            payload['provider'] = {'require_parameters': True}
    order = provider.get('provider_order') or []
    if order and urlsplit(base_url).hostname == 'openrouter.ai':
        # The first upstream serves every request, so its prompt cache stays warm
        # without sticky routing; the next ones only take over when it fails.
        payload['provider'] = {**payload.get('provider', {}), 'order': list(order), 'allow_fallbacks': False}
    # Auto leaves reasoning to the model on every service and request path.
    # https://api-docs.deepseek.com/guides/thinking_mode
    if urlsplit(base_url).hostname == 'openrouter.ai':
        # https://openrouter.ai/docs/use-cases/reasoning-tokens
        if reasoning_effort is not None:
            # A per-purpose step policy (llm_calls.REASONING); 'none' turns
            # reasoning off for that one request only.
            payload['reasoning'] = {'effort': reasoning_effort}
        elif thinking_mode != 'auto':
            payload['reasoning'] = {'enabled': thinking_mode == 'enabled'}
    elif thinking_mode == 'enabled' and urlsplit(base_url).hostname not in ('api.openai.com', 'api.x.ai') and not (
            urlsplit(base_url).hostname or '').endswith('.api.x.ai'):
        # Native OpenAI/xAI model reasoning is controlled by each model's
        # supported defaults; neither accepts this compatibility extension.
        payload['thinking'] = {'type': 'enabled'}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream, application/json'}
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    started = time.monotonic()
    try:
        request_options = {'wait_for_completion': True, 'stream_response': True,
                           'connection_timeout_seconds': setup_timeout}
        if cancellation_event is not None:
            request_options['cancellation_event'] = cancellation_event
            if cancellation_event.is_set():
                raise _TransportCancelled({'stage': 'request', 'elapsed_seconds': 0,
                    'wait_for_completion': wait_for_completion})
        if trace is not None:
            def traced_progress(progress):
                if (trace.get('ttft') is None and isinstance(progress, dict)
                        and progress.get('stage') in ('thinking', 'receiving')):
                    trace['ttft'] = progress.get('elapsed_seconds')
                if on_progress is not None:
                    on_progress(progress)
            request_options['on_progress'] = traced_progress
        elif on_progress is not None:
            request_options['on_progress'] = on_progress
        attempts, waited = 0, 0.0
        while True:
            attempts += 1
            if trace is not None:
                trace['attempts'] += 1
            try:
                result = _request_json(base_url + '/chat/completions', body, headers, **request_options)
                break
            except ProviderError as exc:
                # A 429 or in-flight 402 rejects the request before generation;
                # the identical request may be sent again within the budget.
                delay = getattr(exc, 'retry_after', None)
                delay = max(1.0, delay if delay is not None else 5.0)
                if trace is not None and exc.diagnostics.get('error_code') == 'in_flight_budget':
                    llm_gateway.GATE.backoff(delay)
                if exc.status_code != 429 or attempts >= 3 or waited + delay > retry_budget_seconds:
                    raise
                if cancellation_event is not None:
                    if cancellation_event.wait(delay):
                        raise _TransportCancelled({'stage': 'request', 'elapsed_seconds': time.monotonic() - started,
                                                   'wait_for_completion': wait_for_completion}) from None
                else:
                    time.sleep(delay)
                waited += delay
                if trace is not None:
                    trace['wait_seconds'] += delay
        if trace is not None:
            trace['upstream'] = _safe_diagnostics({'provider': result.get('provider')}).get('provider')
            choices = result.get('choices')
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                trace['finish_reason'] = _safe_diagnostics(
                    {'finish_reason': choices[0].get('finish_reason')}).get('finish_reason')
        if cancellation_event is not None and cancellation_event.is_set():
            raise _TransportCancelled({'stage': 'body', 'elapsed_seconds': time.monotonic() - started,
                'wait_for_completion': wait_for_completion})
        always_on_grok = ((urlsplit(base_url).hostname == 'api.x.ai'
                          or (urlsplit(base_url).hostname or '').endswith('.api.x.ai'))
                         and model in ('grok-4.5', 'grok-4.6', 'grok-4.7'))
        usage = _safe_usage(result.get('usage'))
        transport_diagnostics = _safe_diagnostics(result.get('_diagnostics'))
        diagnostics = _safe_diagnostics({**transport_diagnostics,
            **_safe_diagnostics({'request_id': transport_diagnostics.get('request_id') or result.get('request_id') or result.get('id'),
                'model': result.get('model'), 'provider': result.get('provider')}),
            **usage, 'elapsed_seconds': time.monotonic() - started,
            'wait_for_completion': True})
        diagnostics.setdefault('model', model)
        diagnostics.setdefault('provider', provider.get('id') or urlsplit(base_url).hostname)
        # Upstream IDs/model/provider labels are untrusted. Even syntactically
        # valid identifiers must never echo the credential used for this call.
        diagnostics = {key: value for key, value in diagnostics.items()
                       if not (api_key and isinstance(value, str) and api_key in value)}
        content = _completion_text(result['choices'][0], reasoning_always_on=always_on_grok,
                                   diagnostics=diagnostics, output_cap=max_tokens)
        if api_key:
            content = content.replace(api_key, '[REDACTED]')
        if response_schema is not None and retry_budget_seconds and not _json_object(content):
            # Some upstreams occasionally ignore strict structured output; the
            # caller (complete) sends the same request once more.
            raise ProviderError('AI 服务返回的结构化结果不是有效 JSON', 502,
                                diagnostics={**diagnostics, 'error_code': 'invalid_json'})
        _notify_progress(on_progress, {**transport_diagnostics, 'stage': 'complete', 'elapsed_seconds': time.monotonic() - started,
            'wait_for_completion': wait_for_completion, **usage})
        return {'text': content.strip(), 'usage': usage, 'model': model}
    except ProviderError as exc:
        exc.diagnostics = _safe_diagnostics({key: value for key, value in exc.diagnostics.items()
            if not (api_key and isinstance(value, str) and api_key in value)})
        raise
    except _TransportCancelled as exc:
        raise ProviderError('AI 请求已取消，未创建发布动作', 499,
                            diagnostics=exc.diagnostics) from None
    except (TimeoutError, socket.timeout) as exc:
        diagnostics = _safe_diagnostics(getattr(exc, 'diagnostics', None)) or {
            'stage': 'request', 'elapsed_seconds': round(time.monotonic() - started, 3),
            'wait_for_completion': wait_for_completion}
        if not wait_for_completion:
            diagnostics.setdefault('timeout_seconds', REQUEST_TIMEOUT if timeout_seconds is None else timeout_seconds)
        elapsed = diagnostics.get('elapsed_seconds', 0)
        stage = _TRANSPORT_STAGES.get(diagnostics.get('stage'), '等待模型请求完成')
        limit = diagnostics.get('timeout_seconds')
        setup = diagnostics.get('stage') in ('dns', 'connect', 'tls', 'send')
        phase_elapsed = diagnostics.get('stage_elapsed_seconds')
        phase = stage + (f'已用时 {phase_elapsed:.1f} 秒' if phase_elapsed is not None else '')
        detail = f'本次请求已等待 {elapsed:.1f} 秒，' + (f'连接准备时限 {limit:g} 秒，' if setup and limit is not None else
            f'本次时限 {limit:g} 秒，' if limit is not None else '') + phase
        label = 'AI 服务连接准备超时' if setup else 'AI 服务响应超时'
        raise ProviderError(f'{label}（{detail}），请稍后重试', 504,
                            diagnostics=diagnostics) from None
    except (OSError, http.client.HTTPException):
        raise ProviderError('无法连接 AI 服务，请检查网络和 API 地址', 502) from None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ProviderError('AI 服务返回了无法识别的响应', 502) from None
