"""Shared reading-model configuration and audit; choices remain role-specific."""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
import uuid

from . import ai_providers


MAX_CALLS = 200
MODEL_FIELDS = ('id', 'protocol', 'base_url', 'model', 'enabled', 'token_parameter',
                'thinking_mode', 'max_output_tokens')
SELECTION_FIELDS = ('status', 'selected_ids', 'selected_count', 'candidate_count', 'available_count',
                    'cache_hit', 'cache_mode', 'reason', 'error', 'error_code', 'error_status',
                    'error_stage', 'error_detail', 'error_diagnostics',
                    'provider_id', 'provider_name', 'model', 'settings_revision', 'call_id')
CALL_FIELDS = ('id', 'at', 'agent_id', 'agent_name', 'status', 'candidate_count', 'selected_count',
               'cache_hit', 'cache_mode', 'reason', 'provider_id', 'provider_name', 'model',
               'settings_revision', 'error', 'error_code', 'error_status', 'error_stage', 'error_detail', 'error_diagnostics')


def _error(message, status=400):
    from .agent_control import AgentError
    return AgentError(message, status)


def _settings(data):
    value = data.get('settings', {}).get('reading_selection', {})
    if not isinstance(value, dict):
        raise _error('公共资料筛选配置无法读取，请检查配置备份', 500)
    provider_id, revision = value.get('provider_id', ''), value.get('revision', 1)
    if not isinstance(provider_id, str) or type(revision) is not int or revision < 1:
        raise _error('公共资料筛选配置无法读取，请检查配置备份', 500)
    result = {'provider_id': provider_id, 'revision': revision}
    if isinstance(value.get('updated_at'), str):
        result['updated_at'] = value['updated_at'][:100]
    return result


def _validate_provider(provider):
    if provider.get('enabled') is False:
        raise ai_providers.ProviderError('所选资料筛选 AI 连接已停用', 400)
    if provider.get('protocol') != 'openai_chat':
        error = ai_providers.ProviderError('资料筛选需要 OpenAI 兼容文字连接', 400)
        error.reading_selection_code = 'unsupported_provider'
        raise error
    return provider


def _configuration_view(controller, provider_id, conn=None):
    store = controller.providers
    if isinstance(store, ai_providers.ProviderStore):
        # Read the provider configuration through the SAME locked transaction.
        # Calling get() here would open another BEGIN IMMEDIATE and deadlock.
        public = (store._read(conn) if conn is not None and str(store.db_path) == str(controller.db_path)
                  else store.list_public())
        effective = provider_id or public.get('default_provider_id', '')
        provider = next((row for row in public['providers'] if row.get('id') == effective), None)
    else:
        try:
            provider = store.get(provider_id)
            effective = provider.get('id') or provider_id
        except Exception as exc:
            from .agent_budget import Deferred
            if isinstance(exc, Deferred) or (getattr(exc, 'status_code', None) or getattr(exc, 'status', None)) == 499:
                raise
            provider, effective = None, provider_id
    return {'requested_provider_id': provider_id, 'effective_provider_id': effective,
            'provider': provider}


def _resolve(controller, provider_id, conn=None):
    if conn is None:
        return _validate_provider(controller.providers.get(provider_id))
    provider = _configuration_view(controller, provider_id, conn)['provider']
    if provider is None:
        raise ai_providers.ProviderError('请先保存并选择可用的资料筛选 AI 连接', 400)
    return _validate_provider(provider)


def _fingerprint(view):
    provider = view.get('provider')
    payload = {key: view.get(key) for key in ('requested_provider_id', 'effective_provider_id')}
    payload['provider'] = ({key: provider.get(key, True if key == 'enabled' else None)
                            for key in MODEL_FIELDS} if isinstance(provider, dict) else None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _safe_provider_error(exc):
    if isinstance(exc, ai_providers.ProviderError):
        return str(exc)
    return '资料筛选 AI 连接暂不可用，请检查 AI 配置'


def capture(controller, data, *, resolve_provider=True):
    """Internal immutable binding; never expose its provider credentials."""
    settings = _settings(data)
    provider, error, description = None, None, None
    if resolve_provider:
        try:
            provider = copy.deepcopy(_resolve(controller, settings['provider_id']))
        except Exception as exc:
            from .agent_budget import Deferred
            if isinstance(exc, Deferred) or (getattr(exc, 'status_code', None) or getattr(exc, 'status', None)) == 499:
                raise
            error = {'kind': getattr(exc, 'reading_selection_code', 'provider_unavailable'),
                     'status': getattr(exc, 'status_code', 503)}
            description = _safe_provider_error(exc)
    view = None
    if resolve_provider:
        if provider is not None:
            # Bind to the actual connection used, even if the default changes
            # between get() and the following validation transaction.
            view = {'requested_provider_id': settings['provider_id'],
                    'effective_provider_id': provider.get('id') or settings['provider_id'], 'provider': provider}
        else:
            view = _configuration_view(controller, settings['provider_id'])
    return {'settings': settings, 'provider': provider, 'error': error,
            'provider_error': description, 'provider_checked': resolve_provider,
            'config_provider': view.get('provider') if view else None,
            'fingerprint': _fingerprint(view) if resolve_provider else None}


def assert_binding(controller, binding, data, conn=None):
    current = _settings(data)
    previous = binding['settings']
    if (current['revision'], current['provider_id']) != (previous['revision'], previous['provider_id']):
        raise _error('公共资料筛选设置已改变，本轮旧筛选结果未采用，请重新运行', 409)
    if not binding.get('provider_checked'):
        return
    view = _configuration_view(controller, current['provider_id'], conn)
    if _fingerprint(view) != binding['fingerprint']:
        raise _error('资料筛选默认连接或模型设置已改变，本轮旧筛选结果未采用，请重新运行', 409)


def provider_metadata(binding):
    provider = binding.get('provider') or binding.get('config_provider') or {}
    return {'provider_id': provider.get('id') or binding['settings']['provider_id'],
            'provider_name': provider.get('name') or provider.get('id') or '',
            'model': provider.get('model') or '', 'settings_revision': binding['settings']['revision']}


def _public_selection(value):
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in SELECTION_FIELDS if key in value}
    if result.get('error') and not isinstance(result['error'], str):
        result['error'] = '此前资料筛选未完成，请查看最新筛选记录'
    return result


def _actor_identity(agent):
    from .agent_lifecycle import persona_revision
    from .agent_reading import _digest, _persona
    return {'id': agent['id'], 'persona_revision': persona_revision(agent),
            'fingerprint': _digest(_persona(agent))}


def _assert_actor(identity, data):
    current = data.get('agents', {}).get(identity.get('id')) if isinstance(identity, dict) else None
    if not isinstance(current, dict) or _actor_identity(current) != identity:
        raise _error('角色人设已改变，本轮旧选读结果未记为已读，请重新运行', 409)


def snapshot(controller):
    from . import agent_reading
    data = controller.store.read()
    binding = capture(controller, data)
    provider = binding.get('provider')
    result = {'settings': binding['settings'],
              'provider': {key: provider.get(key) for key in ('id', 'name', 'model', 'protocol')} if provider else None,
              'provider_error': binding.get('provider_error'),
              'limits': {'max_candidates': agent_reading.MAX_CATALOG, 'max_selected': agent_reading.MAX_SELECTED,
                         'cache_seconds': agent_reading.CACHE_SECONDS}, 'roles': [], 'recent_calls': []}
    if binding.get('provider_error'):
        result['provider_error'] = binding['provider_error']
    calls = data.get('rate', {}).get('reading_selection_calls', [])
    calls = [row for row in calls if isinstance(row, dict)][-MAX_CALLS:]
    result['recent_calls'] = [{key: row[key] for key in CALL_FIELDS if key in row} for row in reversed(calls)]
    for agent in data.get('agents', {}).values():
        observation = data.get('memory', {}).get(agent['id'], {}).get('observation', {})
        latest = next((row for row in reversed(calls) if row.get('agent_id') == agent['id']
                       and row.get('status') in ('selected', 'empty', 'fallback')), None)
        selection = _public_selection(latest.get('_selection')) if latest else _public_selection(observation.get('reading_selection'))
        role = {**{key: agent.get(key) for key in ('id', 'name', 'enabled', 'mode')},
                'last_selection_at': None, 'selection': None, 'source_count': None, 'context': {}}
        if selection is not None:
            role.update(selection=selection,
                        last_selection_at=latest['at'] if latest else observation.get('observed_at'))
        if selection is not None and (not latest or observation.get('reading_selection', {}).get('call_id') == latest.get('id')):
            for source, target in (('source_count', 'source_count'), ('thread_context', 'context')):
                if source in observation:
                    role[target] = observation[source]
        result['roles'].append(role)
    return result


def configure(controller, payload):
    if (not isinstance(payload, dict) or set(payload) != {'provider_id', 'expected_revision'}
            or not isinstance(payload['provider_id'], str) or len(payload['provider_id']) > 64
            or payload['provider_id'] != payload['provider_id'].strip()
            or type(payload['expected_revision']) is not int or payload['expected_revision'] < 1):
        raise _error('资料筛选设置须提供 provider_id 和有效 expected_revision')
    current = _settings(controller.store.read())
    if current['revision'] != payload['expected_revision']:
        raise _error('资料筛选设置已被修改，请刷新后重试', 409)
    try:
        checked = _resolve(controller, payload['provider_id'])
    except Exception as exc:
        raise _error(_safe_provider_error(exc), getattr(exc, 'status_code', 400)) from None
    fingerprint = _fingerprint({'requested_provider_id': payload['provider_id'],
                                'effective_provider_id': checked.get('id') or payload['provider_id'], 'provider': checked})
    def save(data, conn):
        previous = _settings(data)
        if previous['revision'] != payload['expected_revision']:
            raise _error('资料筛选设置已被修改，请刷新后重试', 409)
        try:
            latest_provider = _resolve(controller, payload['provider_id'], conn)
        except Exception as exc:
            raise _error(_safe_provider_error(exc), getattr(exc, 'status_code', 400)) from None
        if _fingerprint({'requested_provider_id': payload['provider_id'],
                         'effective_provider_id': latest_provider.get('id') or payload['provider_id'],
                         'provider': latest_provider}) != fingerprint:
            raise _error('所选默认连接或模型设置已改变，请刷新后重试', 409)
        if previous['provider_id'] == payload['provider_id']:
            return
        data['settings']['reading_selection'] = {'provider_id': payload['provider_id'],
            'revision': previous['revision'] + 1,
            'updated_at': datetime.fromtimestamp(controller.clock(), timezone.utc).isoformat().replace('+00:00', 'Z')}
        controller._event(data, 'reading_selection_settings', '公共资料筛选 AI 设置已保存')
    controller.store.change(save)
    return snapshot(controller)


def _record(controller, agent, token, metadata, binding, *, require_current=False):
    call_id = str(uuid.uuid4())
    actor = _actor_identity(agent)
    def save(data, conn):
        if require_current:
            controller._check_lease(data, agent['id'], token)
            controller.generations.check_cancelled(agent['id'], data)
            _assert_actor(actor, data)
            assert_binding(controller, binding, data, conn)
        current = data.get('agents', {}).get(agent['id'], agent)
        row = {'id': call_id,
               'at': datetime.fromtimestamp(controller.clock(), timezone.utc).isoformat().replace('+00:00', 'Z'),
               'agent_id': agent['id'], 'agent_name': current.get('name', ''),
               **{key: metadata[key] for key in CALL_FIELDS if key in metadata}, **provider_metadata(binding),
               '_selection': _public_selection({**metadata, 'call_id': call_id}),
               '_actor': actor,
               '_binding': {key: binding.get(key) for key in ('settings', 'fingerprint', 'provider_checked')}}
        rows = data['rate'].setdefault('reading_selection_calls', [])
        rows.append(row)
        data['rate']['reading_selection_calls'] = rows[-MAX_CALLS:]
    controller.store.change(save)
    return call_id


def check_result(controller, metadata, data, conn=None):
    """Observation commit guard after local thread packing, before recording reads."""
    if not isinstance(metadata, dict) or not metadata.get('call_id'):
        return
    row = next((row for row in data.get('rate', {}).get('reading_selection_calls', [])
                if row.get('id') == metadata['call_id']), None)
    if row is None or not isinstance(row.get('_binding'), dict):
        raise _error('本轮资料筛选记录已改变，请重新运行', 409)
    _assert_actor(row.get('_actor'), data)
    assert_binding(controller, row['_binding'], data, conn)


def select(controller, agent, token, candidates):
    from . import agent_reading, agent_budget, agent_opportunities
    candidates = list(candidates)
    available = agent_reading._all_sources(candidates)
    binding = capture(controller, controller.store.read(),
                      resolve_provider=bool(available) and agent_opportunities.mode(agent) != 'rules')
    try:
        selected, metadata = agent_reading.select(controller, agent, token, candidates, binding=binding)
        metadata.update(provider_metadata(binding))
        metadata['call_id'] = _record(controller, agent, token, metadata, binding, require_current=True)
        return selected, metadata
    except Exception as exc:
        code = getattr(exc, 'status_code', None) or getattr(exc, 'status', None)
        status = 'deferred' if isinstance(exc, agent_budget.Deferred) or code == 429 else 'cancelled' if code == 499 else 'error'
        description = (str(exc) if isinstance(exc, (ai_providers.ProviderError, agent_budget.Deferred))
                       else '选读任务已取消' if code == 499 else '本轮选读未完成，未采用旧结果')
        metadata = {'status': status, 'candidate_count': len(agent_reading._directory(available)),
                    'selected_count': 0, 'cache_hit': False, 'reason': description, 'error': description}
        try:
            _record(controller, agent, token, metadata, binding)
        except Exception:
            pass  # Logging cannot replace the original cancellation or deferral.
        raise
