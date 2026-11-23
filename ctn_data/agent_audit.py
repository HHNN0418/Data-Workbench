"""Append-only, per-role audit history, separate from bounded working memory.

The caller owns initialization and write transactions. No helper commits,
deletes, changes a role, or calls a service. Only allowlisted operational
diagnostics are retained; model prompts, credentials and response bodies are
never accepted as structured details.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping

from .db import iter_rows, timestamp_key


class AuditValidationError(ValueError):
    """A bad filter, event or cursor; HTTP callers may translate this to 400."""


_HISTORY_NOTE = '完整日志从启用审计后保留；此前只能迁移当时仍然存在的最近记录。'
_MAX_MESSAGE = 8000
_TOKEN = re.compile(r'^[A-Za-z0-9._:/-]{1,200}$')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_SECRET = re.compile(
    r'(?i)\b(?:bearer\s+|(?:sk-|xai-)[A-Za-z0-9_-]{8})[A-Za-z0-9._~+/-]*'
    r'|\b(?:api[_ -]?key|authorization|cookie|set-cookie|password|passwd|'
    r'access[_ -]?token|refresh[_ -]?token|session[_ -]?(?:id|token)|'
    r'prompt|messages|response[_ -]?body)[\"\']?\s*[=:]\s*[^\n\r]+'
)
_TEXT_FIELDS = frozenset(('job_id', 'run_id', 'generation_id', 'phase', 'stage',
    'model', 'provider_id', 'provider_host', 'request_id', 'action', 'draft_state',
    'result', 'bucket', 'finish_reason', 'native_finish_reason', 'provider', 'error_code', 'origin', 'selection_status', 'error_detail', 'delivery_reason',
    'review_id', 'review_status', 'review_kind', 'rule_version'))
_NUMBER_FIELDS = frozenset(('elapsed_seconds', 'stage_elapsed_seconds', 'duration_ms',
    'dns_seconds', 'connect_seconds', 'tls_seconds', 'send_seconds', 'local_progress_seconds',
    'timeout_seconds', 'received_bytes', 'http_status', 'retry_after', 'account_id',
    'target_account_id', 'target_post_id', 'post_id', 'source_count', 'read_count',
    'notification_count', 'dropped_notification_count', 'remaining', 'limit', 'attempt', 'max_output_tokens', 'candidate_count', 'selected_count',
    'prompt_tokens', 'completion_tokens', 'total_tokens', 'reasoning_tokens', 'answer_chars', 'revision'))
_BOOL_FIELDS = frozenset(('automatic', 'manual', 'wait_for_completion', 'streaming', 'cancelled',
    'operation_started', 'reconciled', 'already_applied', 'has_reasoning', 'remote_started',
    'role_lease_released', 'waiting_for_role_lease'))
_USAGE_FIELDS = frozenset(('prompt_tokens', 'completion_tokens', 'total_tokens',
    'input_tokens', 'output_tokens', 'reasoning_tokens', 'cached_tokens'))
_COLUMNS = ('id', 'agent_id', 'at', 'kind', 'message', 'draft_id', 'details_json')


def initialize(conn) -> None:
    """Create the independent audit table without changing global DB versions."""
    conn.execute('''CREATE TABLE IF NOT EXISTS agent_audit_events(
        id TEXT PRIMARY KEY, agent_id TEXT, at TEXT NOT NULL, kind TEXT NOT NULL,
        message TEXT NOT NULL, draft_id TEXT, details_json TEXT NOT NULL)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_agent_audit_agent_time
        ON agent_audit_events(agent_id,at,id)''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_agent_audit_agent_kind_time
        ON agent_audit_events(agent_id,kind,at,id)''')


def _label(value, name, maximum=128, *, optional=False):
    if optional and value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise AuditValidationError(f'{name} 格式无效')
    return value


def _safe_text(value):
    return _SECRET.sub('[已隐藏敏感字段]', _CONTROL.sub('', value))


# A draft that failed its checks keeps its own text and issues on the failure event.
REJECTED_BODY_CHARS, REJECTED_ISSUE_CHARS, MAX_REJECTED_ISSUES = 300, 200, 4


def rejected_fields(body=None, issues=None):
    """The rejected text and issues, cleaned of secrets and cut to size; {} when there are none."""
    fields = {}
    if isinstance(body, str) and body.strip():
        fields['rejected_body'] = _safe_text(body)[:REJECTED_BODY_CHARS]
    kept = [_safe_text(issue)[:REJECTED_ISSUE_CHARS] for issue in (issues if isinstance(issues, (list, tuple)) else ())
            if isinstance(issue, str) and issue.strip()][:MAX_REJECTED_ISSUES]
    if kept:
        fields['rejected_issues'] = kept
    return fields


def _safe_details(event):
    details = {}
    # Older events use diagnostics; new callers may use details or direct
    # operational fields. None of those containers is copied wholesale.
    inputs = [event]
    for name in ('diagnostics', 'details'):
        value = event.get(name)
        if isinstance(value, Mapping):
            inputs.append(value)
    for source in inputs:
        for key in _TEXT_FIELDS:
            value = source.get(key)
            pattern = r'[A-Za-z0-9][A-Za-z0-9 ._:/+()\-]{0,199}' if key in ('model', 'provider') else _TOKEN
            valid = re.fullmatch(pattern, value) if isinstance(value, str) else None
            if valid and _safe_text(value) == value:
                details[key] = value
        for key in _NUMBER_FIELDS:
            value = source.get(key)
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10 ** 15:
                details[key] = value
            elif key == 'timeout_seconds' and value is None and key in source:
                details[key] = None
        for key in _BOOL_FIELDS:
            if type(source.get(key)) is bool:
                details[key] = source[key]
        details.update(rejected_fields(source.get('rejected_body'), source.get('rejected_issues')))
        dropped = source.get('dropped_notification_ids')
        if (isinstance(dropped, list) and len(dropped) <= 100
                and all(type(value) is int and 0 < value <= 2**63 - 1 for value in dropped)):
            details['dropped_notification_ids'] = list(dict.fromkeys(dropped))
        usage = source.get('usage')
        if isinstance(usage, Mapping):
            safe_usage = {key: value for key, value in usage.items()
                          if key in _USAGE_FIELDS and type(value) is int and 0 <= value <= 10 ** 15}
            if safe_usage:
                details['usage'] = safe_usage
    return details


def _event_row(event):
    if not isinstance(event, Mapping):
        raise AuditValidationError('审计事件须为对象')
    event_id = _label(event.get('id'), '事件 ID')
    agent_id = _label(event.get('agent_id'), '角色 ID', optional=True)
    draft_id = _label(event.get('draft_id'), '草稿 ID', optional=True)
    kind = _label(event.get('kind'), '事件类型')
    at = timestamp_key(event.get('at'))
    if at is None:
        raise AuditValidationError('事件时间无效')
    message = event.get('message')
    if not isinstance(message, str):
        raise AuditValidationError('事件说明须为文本')
    details = _safe_details(event)
    message = _safe_text(message)
    if len(message) > _MAX_MESSAGE:
        # Do not silently shorten an exceptional unbounded log message. The
        # event itself is still retained and its truncation is explicit.
        details.update(message_truncated=True, original_message_chars=len(message))
        message = message[:_MAX_MESSAGE] + ' [过长说明已截断]'
    return (event_id, agent_id, at, kind, message, draft_id,
            json.dumps(details, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')))


def record(conn, events: Iterable[Mapping]) -> int:
    """Append every supplied event, retaining the first record of an event ID.

    No transaction is opened or committed here. A persistence error must reach
    the caller, so its state mutation can roll back together with its audit.
    """
    count = 0
    for event in events:
        cursor = conn.execute('''INSERT INTO agent_audit_events
            (id,agent_id,at,kind,message,draft_id,details_json) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING''', _event_row(event))
        count += max(0, cursor.rowcount)
    return count


def _filters(agent_id, kind, q):
    agent_id = _label(agent_id, '角色 ID')
    if kind:
        _label(kind, '事件类型')
    elif not isinstance(kind, str):
        raise AuditValidationError('事件类型格式无效')
    if not isinstance(q, str) or len(q) > 200 or _CONTROL.search(q):
        raise AuditValidationError('日志搜索词须为 200 字以内文本')
    q = q.strip()
    clauses, params = ['agent_id=?'], [agent_id]
    if kind:
        clauses.append('kind=?')
        params.append(kind)
    if q:
        literal = q.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        clauses.append("(LOWER(message) LIKE ? ESCAPE '\\' OR LOWER(kind) LIKE ? ESCAPE '\\' "
                       "OR LOWER(COALESCE(draft_id,'')) LIKE ? ESCAPE '\\' "
                       "OR LOWER(details_json) LIKE ? ESCAPE '\\')")
        params.extend(['%' + literal + '%'] * 4)
    scope = hashlib.sha256(json.dumps([agent_id, kind, q], ensure_ascii=False).encode()).hexdigest()
    return ' AND '.join(clauses), params, scope


def _cursor(value, scope):
    if not isinstance(value, str) or not value or len(value) > 1500:
        raise AuditValidationError('日志分页游标无效')
    try:
        decoded = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        if (not isinstance(decoded, dict) or decoded.get('version') != 1 or decoded.get('scope') != scope
                or timestamp_key(decoded.get('at')) != decoded.get('at')):
            raise ValueError
        return decoded['at'], _label(decoded.get('id'), '游标事件 ID')
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise AuditValidationError('日志分页游标无效或与当前筛选不匹配') from None


def _encode_cursor(row, scope):
    raw = json.dumps({'version': 1, 'scope': scope, 'at': row['at'], 'id': row['id']},
                     separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _public_row(row):
    result = {key: row[key] for key in _COLUMNS if key != 'details_json'}
    result['details'] = json.loads(row['details_json'])
    return result


def query(conn, agent_id, *, kind='', q='', limit=50, cursor=None):
    """Read one exact role, newest first, using an (at,id) keyset cursor."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise AuditValidationError('日志每页条数须为 1 至 200 的整数')
    where, params, scope = _filters(agent_id, kind, q)
    total = int(conn.execute('SELECT COUNT(*) FROM agent_audit_events WHERE ' + where, params).fetchone()[0])
    if cursor is not None:
        at, event_id = _cursor(cursor, scope)
        where += ' AND (at<? OR (at=? AND id<?))'
        params += [at, at, event_id]
    rows = conn.execute('SELECT ' + ','.join(_COLUMNS) + ' FROM agent_audit_events WHERE '
                        + where + ' ORDER BY at DESC,id DESC LIMIT ?', [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {'items': [_public_row(row) for row in rows], 'total': total,
            'next_cursor': _encode_cursor(rows[-1], scope) if has_more else None,
            'has_more': has_more, 'scope': 'agent', 'agent_id': agent_id,
            'retention': 'since_audit_enabled', 'history_note': _HISTORY_NOTE}


def _csv_cell(value):
    value = '' if value is None else str(value)
    # Spreadsheet tools may execute formulas after stripping whitespace.
    if value.lstrip(' \t\r\n\ufeff').startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')):
        value = "'" + value
    return value


def export_csv(conn, agent_id, *, kind='', q='', batch_size=500) -> Iterator[str]:
    """Yield UTF-8-ready CSV chunks from one bounded streaming cursor.

    Consume the iterator while its connection is open. Closing the iterator
    early releases its cursor. The caller can stream it as text/csv without
    loading complete history in memory or inheriting the UI's page limit.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 2000:
        raise AuditValidationError('CSV 批次条数须为 1 至 2000 的整数')
    where, params, _ = _filters(agent_id, kind, q)
    statement = ('SELECT ' + ','.join(_COLUMNS) + ' FROM agent_audit_events WHERE '
                 + where + ' ORDER BY at DESC,id DESC')

    def chunks():
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(_COLUMNS)
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
        with iter_rows(conn, statement, params, batch_size=batch_size) as rows:
            buffered = 0
            for row in rows:
                writer.writerow([_csv_cell(row[key]) for key in _COLUMNS])
                buffered += 1
                if buffered >= batch_size:
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)
                    buffered = 0
            if buffered:
                yield output.getvalue()
    return chunks()
