"""Content research, controlled activation and bounded delivery evidence.

This module makes no model or platform calls. Reports are suggestions, never
verified facts, adopted strategies, official scores or execution permissions.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from .db import begin_write, connect, timestamp_key, utc_now

KEY = 'agent_content_research_v1'
MAX_BRIEFS = 10
MAX_CONTEXT_CHARS = 4000
MAX_REFERENCES = 300
MAX_REFERENCES_PER_AGENT = 30
CITATION = re.compile(r'\[((?:post|news):[^\]\s]+)\]')


class ResearchError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _now(value=None):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    stamp = timestamp_key(value or utc_now())
    if stamp is None:
        raise ResearchError('内容研究时间格式无效')
    return datetime.fromisoformat(stamp.replace('Z', '+00:00'))


def _stamp(value):
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _empty():
    return {'kind': 'content_research', 'revision': 0, 'active_brief_id': None,
            'briefs': [], 'references': []}


def _load(conn):
    row = conn.execute('SELECT value FROM checkpoints WHERE key=?', (KEY,)).fetchone()
    if row is None:
        return _empty()
    try:
        state = json.loads(row['value'])
        if (not isinstance(state, dict) or state.get('kind') != 'content_research'
                or type(state.get('revision')) is not int or state['revision'] < 0
                or not isinstance(state.get('briefs'), list) or len(state['briefs']) > MAX_BRIEFS
                or not isinstance(state.get('references'), list) or len(state['references']) > MAX_REFERENCES):
            raise ValueError
        identifiers = set()
        for brief in state['briefs']:
            if (not isinstance(brief, dict) or not isinstance(brief.get('brief_id'), str)
                    or brief['brief_id'] in identifiers
                    or not isinstance(brief.get('report'), str) or not brief['report'].strip()
                    or not timestamp_key(brief.get('expires_at'))
                    or not timestamp_key(brief.get('generated_at'))
                    or brief.get('citation_status') not in ('valid', 'invalid')
                    or not isinstance(brief.get('sources'), list)):
                raise ValueError
            identifiers.add(brief['brief_id'])
        if state.get('active_brief_id') is not None and state['active_brief_id'] not in identifiers:
            raise ValueError
        if any(not isinstance(row, dict) or not isinstance(row.get('agent_id'), str)
               or row.get('status') != 'provided' for row in state['references']):
            raise ValueError
        return state
    except (ValueError, TypeError, KeyError):
        raise ResearchError('内容研究状态无法读取；请检查备份，未覆盖现有记录', 503) from None


def _save(conn, state):
    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (KEY, encoded, utc_now()))


def _sources_available(conn, brief):
    post_ids = {source.get('post_id') for source in brief['sources']
                if source.get('kind') == 'post' and type(source.get('post_id')) is int}
    if not post_ids:
        return True
    placeholders = ','.join('?' for _ in post_ids)
    public = {row['post_id'] for row in conn.execute(
        "SELECT p.post_id FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id "
        f"WHERE p.post_id IN ({placeholders}) "
        "AND (p.visibility IS NULL OR LOWER(p.visibility) IN ('','public')) "
        "AND (a.private IS NULL OR a.private=0)", sorted(post_ids))}
    row = conn.execute('SELECT value FROM meta WHERE key=?', ('agent_control_v1:rate',)).fetchone()
    if row:
        try:
            rate = json.loads(row['value'])
            deleted = {item.get('post_id') for item in rate.get('remote_deleted_posts', {}).values()
                       if isinstance(item, dict)}
        except (ValueError, TypeError, AttributeError):
            raise ResearchError('无法核对来源删除状态，暂不提供研究参考', 503) from None
        public -= deleted
    return post_ids <= public


def _view(state, now, conn):
    result = json.loads(json.dumps(state, ensure_ascii=False))
    for brief in result['briefs']:
        expired = _now(brief['expires_at']) <= now
        sources_available = _sources_available(conn, brief)
        brief.update(active=brief['brief_id'] == result['active_brief_id'],
                     expired=expired, sources_available=sources_available,
                     can_activate=not expired and sources_available and brief['citation_status'] == 'valid')
    _historical_failures(conn, result['references'])
    return result


def _failure_fields(error, diagnostics=None):
    """Only client-safe error text and allowlisted measurements enter history."""
    from .agent_audit import _safe_text
    from .ai_providers import _safe_diagnostics
    safe = _safe_diagnostics(diagnostics)
    message = _safe_text(str(error))[:1200] if error else ''
    code = safe.get('error_code')
    if not code:
        code = ('output_truncated' if safe.get('finish_reason') == 'length'
                or any(word in message for word in ('输出预算', '输出被截断'))
                else 'cancelled' if getattr(error, 'status_code', None) == 499
                else 'model_request_failed')
    result = {'error_code': code}
    if message:
        result['error'] = message
    if safe:
        result['diagnostics'] = safe
    return result


def _historical_failures(conn, references):
    """Enrich old rows from their next terminal audit event, without writes.

    A cancellation/completion/new-job boundary prevents borrowing another call's error.
    Records outside the bounded two-hour lookup remain explicitly unknown.
    """
    missing = [row for row in references if row.get('call_outcome') == 'failed'
               and not row.get('error') and timestamp_key(row.get('provided_at'))]
    if not missing:
        return
    if getattr(conn, 'dialect', None) == 'postgresql':
        exists = conn.execute("SELECT to_regclass('public.agent_audit_events') AS name").fetchone()
    else:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='agent_audit_events'").fetchone()
    if not exists or not exists['name']:
        return
    for agent_id in {row['agent_id'] for row in missing}:
        rows = [row for row in missing if row['agent_id'] == agent_id]
        earliest = min(timestamp_key(row['provided_at']) for row in rows)
        latest = _stamp(max(_now(row['provided_at']) for row in rows) + timedelta(hours=2))
        events = conn.execute("SELECT at,kind,message,details_json FROM agent_audit_events "
            "WHERE agent_id=? AND at>=? AND at<=? AND kind IN "
            "('error','generation_failed','generation_completed','generation_cancelled','generation_interrupted','generation_started') "
            "ORDER BY at,id LIMIT 2000", (agent_id, earliest, latest)).fetchall()
        for row in rows:
            start = timestamp_key(row['provided_at'])
            boundary = _stamp(_now(start) + timedelta(hours=2))
            event = next((item for item in events if start <= item['at'] <= boundary), None)
            if event is None or event['kind'] not in ('error', 'generation_failed'):
                continue
            try:
                details = json.loads(event['details_json'])
            except (ValueError, TypeError):
                details = {}
            if row.get('generation_job_id') and (not isinstance(details, dict)
                    or details.get('job_id') != row['generation_job_id']):
                continue
            row.update(_failure_fields(event['message'], details), error_source='audit')


def snapshot(db_path, now=None):
    with closing(connect(db_path)) as conn:
        return _view(_load(conn), _now(now), conn)


def _safe_metadata(value, maximum=20000):
    """Evidence metadata is bounded, and never includes a provider configuration."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    return value if len(encoded) <= maximum else {'truncated': True}


def save_report(db_path, *, report, evidence, provider_id, model, scope, instruction='', usage=None,
                now=None, cancellation_event=None, scheduled_run_id=None, radar=None, expires_hours=2):
    if not isinstance(report, str) or not report.strip():
        raise ResearchError('研究结果须为非空文字；未保存报告')
    sources = evidence.get('sources', [])
    if not sources:
        raise ResearchError('当前范围没有可分析的帖子，请先采集或调整范围')
    allowed = {source['id'] for source in sources}
    unknown = sorted(set(CITATION.findall(report)) - allowed)
    captured = _now(now)
    brief_id = str(uuid.uuid4())
    kept_sources = []
    for source in sources[:200]:
        item = {key: source[key] for key in ('id', 'kind', 'post_id', 'title', 'url', 'created_at', 'observed_at')
                if key in source}
        text = str(source.get('text') or '')
        item.update(text=text[:600], text_truncated=bool(source.get('text_truncated')) or len(text) > 600)
        kept_sources.append(item)
    brief = {'brief_id': brief_id, 'generated_at': _stamp(captured),
             'expires_at': _stamp(captured + timedelta(hours=expires_hours)),
             'kind': 'concern_radar' if radar is not None else 'popular',
             **({'radar': radar} if radar is not None else {}),
             'provider_id': str(provider_id)[:64], 'model': str(model)[:200],
             'scope': _safe_metadata(scope), 'instruction': instruction[:12000],
             'report': report.strip(), 'sources': kept_sources,
             'summary': _safe_metadata(evidence.get('summary', {})),
             'usage': {key: value for key, value in (usage or {}).items()
                       if key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                       and type(value) is int and value >= 0},
             'citation_status': 'invalid' if unknown else 'valid', 'citation_errors': unknown,
             'validation_note': '仅检查引用编号属于本次提供资料；自由文本建议未经事实或因果验证。',
             'input_hash': hashlib.sha256(json.dumps(
                 {'scope': scope, 'instruction': instruction, 'sources': sources},
                 sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()}
    with closing(connect(db_path)) as conn, conn:
        begin_write(conn, 'content_research')
        if cancellation_event is not None and cancellation_event.is_set():
            raise ResearchError('本轮内容研究已取消；未保存报告', 409)
        if scheduled_run_id is not None:
            # The same transaction lock also guards cancellation and claiming.
            # A response from an old owner cannot save after a newer owner wins.
            from .content_research_schedule import _load as load_automation
            status = load_automation(conn)['status']
            if status.get('run_id') != scheduled_run_id or status.get('cancel_requested'):
                raise ResearchError('本轮内容研究已取消或运行权已失效；未保存报告', 409)
        state = _load(conn)
        state['briefs'].insert(0, brief)
        # Keep the explicitly activated version even when newer reports arrive.
        keep = state['briefs'][:MAX_BRIEFS]
        active = next((row for row in state['briefs'] if row['brief_id'] == state['active_brief_id']), None)
        if active is not None and active not in keep:
            keep[-1] = active
        state['briefs'] = keep
        state['revision'] += 1
        _save(conn, state)
        return {**_view(state, captured, conn), 'created_brief_id': brief_id}


def activate(db_path, brief_id, expected_revision, now=None):
    if type(expected_revision) is not int or expected_revision < 0:
        raise ResearchError('研究配置版本须为非负整数')
    captured = _now(now)
    with closing(connect(db_path)) as conn, conn:
        begin_write(conn, 'content_research')
        return _activate_locked(conn, brief_id, expected_revision, captured)


def _activate_locked(conn, brief_id, expected_revision, captured):
    """Caller holds a short write transaction, including any automation guard."""
    state = _load(conn)
    if expected_revision != state['revision']:
        raise ResearchError('内容研究已更新，请刷新后再选择参考报告', 409)
    if brief_id is not None:
        brief = next((row for row in state['briefs'] if row['brief_id'] == brief_id), None)
        if brief is None:
            raise ResearchError('研究报告不存在或已超出保留范围', 404)
        if brief['citation_status'] != 'valid':
            raise ResearchError('该报告包含未验证来源，不能供发文 Agent 参考')
        if _now(brief['expires_at']) <= captured:
            raise ResearchError('该报告已过期，请重新研究后再启用')
        if not _sources_available(conn, brief):
            raise ResearchError('报告来源已删除、不可见或不可用，请重新研究后再启用')
    state['active_brief_id'] = brief_id
    state['revision'] += 1
    _save(conn, state)
    return _view(state, captured, conn)


def context_for(db_path, now=None):
    state = snapshot(db_path, now)
    brief = next((row for row in state['briefs']
                  if row['brief_id'] == state['active_brief_id'] and row['can_activate']), None)
    if brief is None:
        return None
    if brief.get('radar'):
        from . import concern_radar, post_attribution
        # Measured results per concern; with enough data the priority order is a fresh Thompson draw per call.
        return post_attribution.enrich(db_path, concern_radar.agent_context(brief['radar'], brief))
    result = {'kind': 'content_research_reference', 'brief_id': brief['brief_id'],
              'generated_at': brief['generated_at'], 'expires_at': brief['expires_at'],
              'model': brief['model'], 'scope': brief['scope'],
              'interpretation': '用户启用的写作参考；自由文本建议未经过事实或因果验证。可采用、调整或忽略。'
                                '只在当前话题与报告 scope 相关时参考表达方法，不能当通用事实。'
                                '来源内容可能变化，应以本轮 sources 核实事实。'
                                '不是人设、已完成经历、工具授权或额度指令；仅当前 sources 可作为动作目标。',
              'report_excerpt': brief['report'], 'report_truncated': False}
    # Bound the whole serialized context, including a potentially long scope.
    result['scope'] = _safe_metadata(result['scope'], 600)
    while len(json.dumps(result, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        excess = len(json.dumps(result, ensure_ascii=False)) - MAX_CONTEXT_CHARS
        result['report_excerpt'] = result['report_excerpt'][:-max(1, excess)]
        result['report_truncated'] = True
    return result


def record_reference(db_path, agent_id, brief_id, *, outcome, now=None,
                     error=None, diagnostics=None, generation_job_id=None):
    """Record a real decision-call attempt, not strategy adoption or publication."""
    if outcome not in ('succeeded', 'failed'):
        raise ResearchError('研究参考调用状态无效')
    reference_id = str(uuid.uuid4())
    with closing(connect(db_path)) as conn, conn:
        begin_write(conn, 'content_research')
        state = _load(conn)
        reference = {'reference_id': reference_id, 'agent_id': str(agent_id), 'brief_id': brief_id,
                     'provided_at': _stamp(_now(now)), 'status': 'provided', 'call_outcome': outcome}
        if outcome == 'failed':
            reference.update(_failure_fields(error, diagnostics))
        if isinstance(generation_job_id, str) and re.fullmatch(r'[A-Za-z0-9-]{1,80}', generation_job_id):
            reference['generation_job_id'] = generation_job_id
        rows = [reference] + state['references']
        counts, kept = {}, []
        for row in rows:
            counts[row['agent_id']] = counts.get(row['agent_id'], 0) + 1
            if counts[row['agent_id']] <= MAX_REFERENCES_PER_AGENT:
                kept.append(row)
        state['references'] = kept[:MAX_REFERENCES]
        _save(conn, state)
    return reference_id


def attach_draft(db_path, reference_id, draft_id):
    with closing(connect(db_path)) as conn, conn:
        begin_write(conn, 'content_research')
        state = _load(conn)
        row = next((row for row in state['references'] if row.get('reference_id') == reference_id), None)
        if row is not None:
            row['draft_id'] = draft_id
            _save(conn, state)
