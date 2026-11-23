"""Task-specific, permission-filtered evidence for reporting review."""
from __future__ import annotations

from contextlib import closing
from itertools import zip_longest

from .account_screening import build_screening
from .ai_evidence import POST_FIELDS, POST_JOINS, DETAIL_JOINS, _scope, _predicate, _post_source
from .db import connect


def _choose_accounts(items, maximum=6):
    # Include low/unknown-score diagnostics so AI is not restricted to rule hits.
    high = [x for x in items if x.get('score') is not None and x['score'] > 0]
    low = [x for x in items if x.get('score') == 0]
    unknown = [x for x in items if x.get('score') is None]
    buckets = [high, low, unknown]
    chosen = []
    for group in zip_longest(*buckets):
        chosen.extend(x for x in group if x is not None)
        if len(chosen) >= maximum:
            break
    return chosen[:maximum]


def reporting_evidence(db_path, params=None, *, sample_size=100):
    scope = {key: value for key, value in (params or {}).items() if key in ('since', 'until')}
    screening = build_screening(db_path, scope, limit=200, min_score=0, include_diagnostics=True)
    accounts = _choose_accounts(screening['items'])
    id_lists, compact = [], []
    for account in accounts:
        ids = []
        signals = []
        for signal in sorted(account.get('signals', []), key=lambda x: (x.get('points', 0) > 0, 'coordination' in x.get('key', ''), x.get('points', 0)), reverse=True):
            measured = signal.get('measurements', {})
            own_ids = signal.get('post_ids', [])
            peers = measured.get('peer_post_ids', [])
            ids.extend(own_ids)
            ids.extend(peers)
            signals.append({key: signal[key] for key in ('key', 'title', 'measured', 'measurements', 'post_ids') if key in signal})
        ids.extend(x['post_id'] for x in account.get('examples', []) if 'post_id' in x)
        ids = list(dict.fromkeys(int(x) for x in ids if str(x).isdigit()))[:24]
        id_lists.append(ids)
        compact.append({key: account.get(key) for key in ('account_id', 'username', 'risk_priority_score',
            'known_post_count', 'data_observed', 'account_report_eligibility')})
        compact[-1].update(signals=signals, requested_source_ids=[f'post:{x}' for x in ids])
    # Round-robin avoids the first account consuming all of the evidence budget.
    ordered_ids = list(dict.fromkeys(x for group in zip_longest(*id_lists) for x in group if x is not None))[:min(200, sample_size)]
    sources = []
    if ordered_ids:
        where, args = _predicate(_scope(scope))
        where += ' AND (t.is_own IS NULL OR t.is_own<>1)'
        placeholders = ','.join('?' for _ in ordered_ids)
        with closing(connect(db_path)) as conn:
            rows = conn.execute('SELECT ' + POST_FIELDS + POST_JOINS + DETAIL_JOINS + where +
                ' AND p.post_id IN (' + placeholders + ')', args + ordered_ids).fetchall()
        by_id = {row['post_id']: row for row in rows}
        for identifier in ordered_ids:
            if identifier not in by_id:
                continue
            row = by_id[identifier]
            source = _post_source(row, 'reporting_witness')
            source['text'] = row['body'] or ''
            source['text_truncated'] = row['body_length'] > len(source['text'])
            sources.append(source)
    actual = {x['id'] for x in sources}
    for account in compact:
        requested = account.pop('requested_source_ids')
        account['source_ids'] = [x for x in requested if x in actual]
        account['unavailable_source_ids'] = [x for x in requested if x not in actual]
    return {'sources': sources, 'summary': screening.get('summary', {}),
        'reporting_context': {'accounts': compact, 'candidate_pool_count': len(screening['items']),
            'selected_accounts': len(compact), 'methodology': screening.get('methodology', {}),
            'selection': '按规则优先、零分及资料不足三组交错抽样；仅复核候选，未覆盖账号不能视为无风险。',
            'scope': scope, 'excluded_own_accounts': True,
            'eligibility': {'status': 'unknown', 'required_permanently_hidden_posts': 1,
                'required_official_actions': 100, 'official_counts_available': False}},
        'retrieval': {'method': 'behavior_candidates_and_original_witnesses', 'semantic_search': False,
                      'reranker': False, 'selected_count': len(sources)},
        'caveats': screening.get('caveats', []) + [
            '举报任务只沿用时间范围，排除本队账号；作者和归属筛选不限制关联账号复核。',
            '规则分是未校准的复核优先级。公开协调、AI文风或相似立场不证明非真实身份或竞争方归属。',
            '账号举报需至少一篇官方永久隐藏帖子及至少100次官方行为；未接入官方计数，资格未知。',
            '仅对已提供原文作语义结论；unavailable_source_ids表示未加载或无权提供，不能据编号推断内容。']}
