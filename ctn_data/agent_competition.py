"""Local, observable attention estimates and bounded automatic-agent scheduling.

Persistent mutators run inside AgentStore.change's transaction. Historical
statistics can be prepared in a read snapshot and applied with an optimistic
guard in a short write transaction. No network/model calls or official score
writes occur here.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math

from .scoring import ENGAGEMENT, rules_context, sydney_time

VERSION = 'observed-attention-v1'
PREFIX = 'agent_competition_v1:publication:'
METRICS = {'views': 'view_count', 'likes': 'like_count',
           'reposts': 'repost_count', 'replies': 'reply_count'}
DEFAULTS = {'mode': 'off', 'enhanced_provider_id': None, 'premium_provider_id': None,
            'enhanced_hourly_calls': 10, 'enhanced_max_output_tokens': 1800}
NOTES = [
    '公开观测估分，不是官方实得：不能识别 NPC、会话去重或官方入账时间。',
    '使用官方互动权重；关注与取消关注全员未接入，不以粉丝净变化替代。',
    '浏览按所有已识别本队帖子共享悉尼日 500 次，先封顶再乘观测日倍率。',
    '真实基线需在发布后 2 分钟内，端点在基线后 60–62 分钟；漏采不补零。',
    '已知队内互动或计数冲突使帖子待核对；匿名浏览无法完整排除队内访问。',
    '历史修订会重算当前账本与近 48 小时成绩，不追讨已使用的机会或模型调用。',
]


class CompetitionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
        self.status_code = status


def _epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(value):
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace('+00:00', 'Z')


def _state(data, create=True):
    if not create:
        return data.get('rate', {}).get('competition', {})
    state = data.setdefault('rate', {}).setdefault('competition', {})
    state.setdefault('settings', dict(DEFAULTS))
    state.setdefault('revision', 0)
    for field in ('profiles', 'bindings', 'allocations', 'fair', 'models', 'model_requests'):
        state.setdefault(field, {})
    state.setdefault('ready_ids', [])
    state.setdefault('rounds', [])
    state.setdefault('daily_views', [])
    state.setdefault('pending_samples', [])
    state.setdefault('posts', [])
    return state


def _agents(data):
    return data.get('agents', {})


def _ready(data, agent):
    return (bool(data.get('settings', {}).get('enabled')) and bool(agent.get('enabled'))
            and agent.get('mode') == 'auto' and agent.get('account_state') == 'ready'
            and agent.get('account_id') is not None
            and bool(set(agent.get('allowed_actions', [])) & {'post', 'reply', 'repost'}))


# rate.competition is rewritten on every automatic decision (select_model) and publish; 0.74 MB on
# 24 Oct. While off, the estimates and daily views move to their own cold row, and model
# selections older than an hour always do: retries within the hour keep their model.
COLD_KEY = 'agent_competition_cold_v1'
HOT_REQUEST_SECONDS = 3600
COLD_REQUEST_LIMIT = 500


def _cold(conn):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (COLD_KEY,)).fetchone() if conn is not None else None
    try:
        return json.loads(row['value']) if row else {}
    except (TypeError, ValueError):
        return {}


def needs_offload(state, now):
    """True when the hot state still holds something offload() moves (cheap, read without the lock)."""
    if any(float(now) - row.get('at', now) >= HOT_REQUEST_SECONDS for row in (state.get('model_requests') or {}).values()):
        return True
    return ((state.get('settings') or {}).get('mode', 'off') == 'off'
            and any(state.get(field) for field in ('posts', 'daily_views', 'pending_samples')))


def offload(conn, data, now):
    """Move old model selections, and while off the estimates and daily views, to the cold row.

    Idempotent; nothing is deleted except pending_samples (a sampling queue refresh rebuilds).
    Returns whether the hot state changed.
    """
    state, now = _state(data), float(now)
    cold, moved = _cold(conn), False
    requests = state.get('model_requests', {})
    old = {key: row for key, row in requests.items() if now - row.get('at', now) >= HOT_REQUEST_SECONDS}
    if old:
        merged = {**cold.get('model_requests', {}), **old}
        cold['model_requests'] = dict(sorted(merged.items(), key=lambda p: p[1].get('at', 0))[-COLD_REQUEST_LIMIT:])
        state['model_requests'] = {key: row for key, row in requests.items() if key not in old}
        moved = True
    if state['settings'].get('mode', 'off') == 'off':
        for field in ('posts', 'daily_views'):
            if state.get(field):
                cold[field], state[field] = state[field], []
                moved = True
        if state.get('pending_samples'):
            state['pending_samples'] = []
            moved = True
    if moved:
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (COLD_KEY, json.dumps(cold, ensure_ascii=False)))
    return moved


# Each publish records its own receipt (agent_actions._competition_receipt); sync only retries
# the ones it missed, so it checks drafts published since its last pass, with this much overlap
# for deliveries confirmed late.
RECEIPT_LOOKBACK_SECONDS = 3600


def receipt_drafts(data, now):
    """(draft, published_at) for the published automatic drafts sync still checks for a receipt."""
    since = _state(data, False).get('receipts_synced_through')
    for draft in data.get('drafts', {}).values():
        if draft.get('state') == 'published' and draft.get('competition_automatic'):
            at = _epoch(draft.get('published_at')) or float(now)
            if since is None or at >= since - RECEIPT_LOOKBACK_SECONDS:
                yield draft, at


def prefetch_receipts(conn, data, now):
    """{checkpoint key: value} of the receipts sync is about to check, read in one query (outside the lock)."""
    keys = list(dict.fromkeys(PREFIX + str(draft['post_id']) for draft, _ in receipt_drafts(data, now)
                              if draft.get('post_id')))
    if not keys:
        return {}
    return {row['key']: row['value'] for row in conn.execute(
        'SELECT key,value FROM checkpoints WHERE key IN (' + ','.join('?' for _ in keys) + ')', keys)}


def sync(conn, data, now, ready_ids=None, known=None):
    """Persist first automatic-ready time, once per account, even while off.

    ``known``: receipts prefetched by prefetch_receipts; a matching one saves its lookup.
    """
    now = float(now)
    state = _state(data)
    accepted = None if ready_ids is None else set(ready_ids)
    ready = []
    for aid, agent in sorted(_agents(data).items()):
        if not _ready(data, agent) or (accepted is not None and aid not in accepted):
            continue
        account = str(agent['account_id'])
        state['profiles'].setdefault(account, {'first_ready_at': now, 'protected_until': now + 3600})
        binding = state['bindings'].get(aid)
        if not binding or binding['account_id'] != account:
            state['bindings'][aid] = {'account_id': account, 'epoch': now}
            state['allocations'].pop(aid, None)
            state['models'].pop(aid, None)
            for fair in state['fair'].values():
                fair.get('served', {}).pop(aid, None)
                fair.get('pending', {}).pop(aid, None)
        ready.append(aid)
    # Multiple controllers may never compete for the same account.
    owners, duplicates = {}, []
    for aid in ready:
        account = str(_agents(data)[aid]['account_id'])
        if account in owners:
            duplicates.append(aid)
        else:
            owners[account] = aid
    state['ready_ids'] = [aid for aid in ready if aid not in duplicates]
    state['duplicate_ids'] = duplicates
    if state.get('members') != state['ready_ids']:
        _rebase(state, state['ready_ids'], now)
    newest = state.get('receipts_synced_through')
    for draft, at in list(receipt_drafts(data, now)):
        record_published(conn, data, draft, now, known)
        newest = at if newest is None else max(newest, at)
    if newest is not None:
        state['receipts_synced_through'] = newest
    return state['ready_ids']


def _project(target, lower, upper):
    if not target:
        return {}
    lo, hi = -2., 2.
    for _ in range(75):
        middle = (lo + hi) / 2
        total = sum(min(upper[k], max(lower[k], value + middle)) for k, value in target.items())
        if total > 1:
            hi = middle
        else:
            lo = middle
    return {k: min(upper[k], max(lower[k], v + (lo + hi) / 2)) for k, v in target.items()}


def _rebase(state, members, now=None):
    n = len(members)
    if not n:
        state['members'] = []
        return
    old = state.get('members', [])
    newcomers = [aid for aid in members if aid not in old]
    continuing = [aid for aid in members if aid in old]
    remainder = 1 - len(newcomers) / n
    total = sum(state['allocations'].get(aid, {}).get('share', 0) for aid in continuing)
    values = {aid: (1 / n if aid in newcomers or not total else
                   remainder * state['allocations'][aid]['share'] / total) for aid in members}
    shares = _project(values, {k: .4 / n for k in members}, {k: min(1, 2 / n) for k in members})
    for aid in members:
        allocation = state['allocations'].setdefault(aid, {})
        previous = allocation.get('share')
        allocation.update(share=shares[aid], target_share=shares[aid])
        binding = state['bindings'].get(aid, {})
        allocation['basis'] = {
            'kind': 'membership_rebase', 'account_id': binding.get('account_id'),
            'binding_epoch': binding.get('epoch'), 'round_id': None, 'decided_at': now,
            'previous_share': previous, 'share': shares[aid], 'target_share': shares[aid],
            'performance_comparison_valid': False, 'preserved_missing_data': False,
            'adjustment': 'membership_rebase',
            'reasons': ['就绪成员变化，重新建立资源分配基准；本次调整不是成绩奖惩',
                        '新增成员先按人数均分，继续参与者按原相对份额分配剩余部分，再应用保底与上限']}
    state['members'] = list(members)


def configure(conn, data, payload, now):
    state = _state(data)
    if type(payload.get('expected_revision')) is not int or payload.get('expected_revision') != state['revision']:
        raise CompetitionError('竞赛配置已改变，请刷新后重试。', 409)
    settings = {**DEFAULTS, **state['settings']}
    if payload.get('mode', settings['mode']) not in ('off', 'observe', 'resources', 'models'):
        raise CompetitionError('无效竞赛模式。')
    for name in DEFAULTS:
        if name in payload and name != 'enhanced_max_output_tokens':
            settings[name] = payload[name]
    # Keep the legacy stored token field readable, but it no longer governs
    # model requests. Only call quotas remain configurable.
    for name, minimum, maximum in (('enhanced_hourly_calls', 0, 1000),):
        value = settings[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise CompetitionError(f'{name} 必须为 {minimum}–{maximum} 的整数。')
    for name in ('enhanced_provider_id', 'premium_provider_id'):
        value = settings[name]
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200):
            raise CompetitionError(f'{name} 无效。')
    state['settings'] = settings
    state['revision'] += 1
    return snapshot(conn, data, now)


def record_published(conn, data, draft, now, known=None):
    """Accept only a confirmed automatically executed, independently authored post.

    ``known`` maps checkpoint keys to receipts read earlier; only a receipt there that
    already matches this draft skips the lookup (the lookup would change nothing).
    """
    if (not draft.get('competition_automatic') or draft.get('origin') == 'manual'
            or draft.get('state') != 'published' or not draft.get('post_id')
            or draft.get('action') not in ('post', 'reply', 'repost')):
        return False
    aid, account = draft.get('agent_id'), draft.get('account_id')
    if not aid or account is None:
        return False
    state = _state(data)
    binding = state['bindings'].get(aid)
    if not binding:
        # A publication receipt can precede the next maintenance tick.  This
        # records identity only; it does not start the readiness protection.
        state['bindings'][aid] = {'account_id': str(account), 'epoch': float(now)}
    publication = {
        'post_id': int(draft['post_id']), 'agent_id': aid, 'account_id': str(account),
        'draft_id': draft.get('id'), 'published_at': _epoch(draft.get('published_at')) or float(now),
        'binding_epoch': state['bindings'].get(aid, {}).get('epoch'),
        'persona_revision': draft.get('persona_revision'),
        'provider_id': draft.get('actual_provider_id'),
        'model': draft.get('model'), 'research_brief_id': draft.get('research_brief_id'),
        'competition_model': draft.get('competition_model'),
        'competition_allocation': draft.get('competition_allocation'), 'origin': 'automatic',
    }
    key = PREFIX + str(publication['post_id'])
    cached = (known or {}).get(key)
    if cached is not None and all(json.loads(cached).get(k) == publication.get(k)
                                  for k in ('agent_id', 'account_id', 'draft_id')):
        return False
    existing = conn.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone()
    if existing:
        old = json.loads(existing['value'])
        if not old.get('attribution_conflict') and any(
                old.get(k) != publication.get(k) for k in ('agent_id', 'account_id', 'draft_id')):
            old['attribution_conflict'] = True
            conn.execute('UPDATE checkpoints SET value=?,updated_at=? WHERE key=?',
                         (json.dumps(old), _iso(now), key))
            state['last_refresh_at'] = None
        return False
    cursor = conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                         (key, json.dumps(publication), _iso(now)))
    state['last_refresh_at'] = None
    return bool(cursor.rowcount)


def _publications(conn):
    return [json.loads(row['value']) for row in conn.execute(
        'SELECT value FROM checkpoints WHERE key LIKE ?', (PREFIX + '%',))]


def _own_accounts(conn, data, publications):
    result = {str(row['account_id']) for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
    result.update(str(a['account_id']) for a in _agents(data).values() if a.get('account_id') is not None)
    result.update(p['account_id'] for p in publications)
    return result


def _ledger(conn, accounts, now):
    """Replay canonical history; same actual timestamp constitutes a shared batch.

    Older history supplies recomputed high-water marks in SQL; the last three
    Sydney days are replayed from midnight. Replaying instead of adding to
    yesterday's ledger is essential:
    an old canonical correction can change today's counted increment.
    """
    if not accounts:
        return {}, {}, [], set(), {}
    account_ids = tuple(sorted({int(value) for value in accounts}))
    placeholders = ','.join('?' for _ in account_ids)
    posts = {int(row['post_id']): dict(row) for row in conn.execute(
        f'SELECT post_id,author_id,visibility,reply_to_post_id,repost_of_post_id,created_at,first_seen '
        f'FROM posts WHERE author_id IN ({placeholders})', account_ids)}
    history, highs, last, daily, entries = defaultdict(list), {}, {}, {}, []
    cutoff = sydney_time(_iso(now - 72 * 3600)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    # Aggregate the canonical prefix on every refresh rather than persisting an
    # irreversible MAX. A correction older than 48h then repairs today's H too.
    prefix = conn.execute(
        'SELECT c.post_id,MAX(c.view_count) AS view_count,MAX(c.like_count) AS like_count,'
        'MAX(c.reply_count) AS reply_count,MAX(c.repost_count) AS repost_count,'
        'MAX(ctn_timestamp(c.observed_at)) AS observed_at '
        'FROM counter_snapshots c JOIN posts p ON p.post_id=c.post_id '
        f'WHERE p.author_id IN ({placeholders}) AND ctn_timestamp(c.observed_at)<ctn_timestamp(?) '
        'GROUP BY c.post_id', (*account_ids, _iso(cutoff)))
    for row in prefix:
        pid = int(row['post_id'])
        highs[pid] = {m: row[c] for m, c in METRICS.items() if row[c] is not None}
        if row['observed_at']:
            last[pid] = sydney_time(row['observed_at']).date().isoformat()
    rows = conn.execute(
        'SELECT c.* FROM counter_snapshots c JOIN posts p ON p.post_id=c.post_id '
        f'WHERE p.author_id IN ({placeholders}) AND ctn_timestamp(c.observed_at)>=ctn_timestamp(?) '
        'AND ctn_timestamp(c.observed_at)<=ctn_timestamp(?) ORDER BY ctn_timestamp(c.observed_at),c.id',
        (*account_ids, _iso(cutoff), _iso(now)))
    batches = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        at = _epoch(row['observed_at'])
        if at is None or at > now:
            continue
        pid = int(row['post_id'])
        day = sydney_time(_iso(at)).date().isoformat()
        high = highs.setdefault(pid, {})
        delta, anomalies = {}, []
        before = dict(high)
        for metric, column in METRICS.items():
            count = row[column]
            if count is None:
                delta[metric] = None
                continue
            if metric not in high:
                delta[metric] = 0
            else:
                delta[metric] = max(0, count - high[metric])
                if count < high[metric]:
                    anomalies.append(metric)
            high[metric] = max(high.get(metric, count), count)
        record = {'snapshot_id': row['id'], 'post_id': pid, 'at': at, 'day': day,
                  'counts': {m: row[c] for m, c in METRICS.items()}, 'delta': delta,
                  'high_before': before, 'anomalies': anomalies,
                  'crosses_day_boundary': pid in last and last[pid] != day,
                  'multiplier': rules_context(_iso(at))['red_multiplier'] or 1}
        last[pid] = day
        # Keep complete history for canonical replay; only recent records are
        # needed by the short observation windows and the response.
        if at >= now - 72 * 3600:
            history[pid].append(record)
        batches[(at, day)].append(record)
    for (at, day), batch in sorted(batches.items()):
        usage = daily.setdefault(day, {'date': day, 'observed_views': 0, 'credited_views': 0, 'cap': 500})
        observed = sum(r['delta']['views'] or 0 for r in batch)
        remaining = max(0, 500 - usage['credited_views'])
        available = min(observed, remaining)
        allocations = {r['snapshot_id']: 0 for r in batch}
        if observed:
            ranked = []
            for r in batch:
                exact = available * (r['delta']['views'] or 0) / observed
                allocations[r['snapshot_id']] = math.floor(exact)
                ranked.append((-(exact - math.floor(exact)), r['post_id'], r['snapshot_id']))
            residue = available - sum(allocations.values())
            for _, _, sid in sorted(ranked)[:residue]:
                allocations[sid] += 1
        usage['observed_views'] += observed
        usage['credited_views'] += available
        for r in batch:
            r['credited_views'] = allocations[r['snapshot_id']]
            r['known_points'] = r['multiplier'] * (
                r['credited_views'] + sum(ENGAGEMENT[m] * (r['delta'][m] or 0)
                                          for m in METRICS if m != 'views'))
            if at >= now - 72 * 3600:
                entries.append(r)
    conflicts = {int(row['post_id']) for row in conn.execute(
        'SELECT DISTINCT post_id FROM counter_conflicts')}
    recent_days = sorted(daily)[-4:]
    return history, posts, [daily[d] for d in recent_days], conflicts, entries


def _contaminated(data, posts, pid, start, end):
    for other in posts.values():
        if pid not in (other.get('reply_to_post_id'), other.get('repost_of_post_id')):
            continue
        at = _epoch(other.get('created_at')) or _epoch(other.get('first_seen'))
        if at is not None and start <= at <= end:
            return True
    drafts = data.get('drafts', {})
    for draft in drafts.values() if isinstance(drafts, dict) else drafts:
        if not isinstance(draft, dict):
            continue
        at = _epoch(draft.get('published_at'))
        target = draft.get('target_post_id', draft.get('reply_to'))
        if draft.get('state') == 'published' and draft.get('action') == 'like' and str(target) == str(pid):
            if at is not None and start <= at <= end:
                return True
    for receipt in data.get('_competition_receipts', []):
        at = _epoch(receipt.get('tracking', {}).get('tracked_at'))
        if (receipt.get('action') == 'like' and str(receipt.get('target_post_id')) == str(pid)
                and at is not None and start <= at <= end):
            return True
    return False


def _measure(publication, history, posts, conflicts, data, now):
    pid, published = publication['post_id'], publication['published_at']
    records = history.get(pid, [])
    baseline = next((r for r in records if published <= r['at'] <= published + 120), None)
    target = (baseline['at'] if baseline else published + 120) + 3600
    endpoint = next((r for r in records if target <= r['at'] <= target + 120), None)
    result = {**publication, 'baseline_at': baseline['at'] if baseline else None,
              'target_at': target, 'deadline': target + 120,
              'endpoint_at': endpoint['at'] if endpoint else None,
              'observed_minutes': (endpoint['at'] - baseline['at']) / 60 if baseline and endpoint else None,
              'round_id': str(int(target // 3600) * 3600), 'score': None,
              'known_component_subtotal': None, 'eligible': False, 'reasons': []}
    pending = []
    if not baseline and now <= published + 120:
        pending.append({'post_id': pid, 'target_at': published, 'due_at': published,
                        'deadline': published + 120, 'stage': 'baseline'})
    elif baseline and not endpoint and now <= target + 120:
        pending.append({'post_id': pid, 'target_at': target, 'due_at': target,
                        'deadline': target + 120, 'stage': 'endpoint'})
    if baseline is None:
        result['reasons'].append('等待早期基线' if now <= published + 120 else '发布后 2 分钟内未取得真实基线')
    elif endpoint is None:
        result['reasons'].append('等待满小时端点' if now <= target + 120 else '60–62 分钟端点缺失')
    if not baseline or not endpoint:
        return result, pending
    interval = [r for r in records if baseline['at'] < r['at'] <= endpoint['at']]
    result['known_component_subtotal'] = sum(r['known_points'] for r in interval)
    result['crosses_day_boundary'] = any(r['crosses_day_boundary'] for r in interval)
    if publication.get('attribution_conflict'):
        result['reasons'].append('同一帖子存在不同发布归属，待核对')
    if any(baseline['counts'][m] is None or endpoint['counts'][m] is None for m in METRICS):
        result['reasons'].append('四项计数不完整，已知小计不参与排名')
    if endpoint['anomalies']:
        result['reasons'].append('端点计数低于历史高水位，待核对')
    post = posts.get(pid)
    if not post or str(post['author_id']) != publication['account_id']:
        result['reasons'].append('帖子作者与发布回执不一致')
    elif post.get('repost_of_post_id'):
        result['reasons'].append('转发链计数归原帖，不重复奖励转发者')
    elif post.get('visibility') != 'public':
        result['reasons'].append('帖子当前未确认公开')
    tombstones = data.get('rate', {}).get('remote_deleted_posts', {})
    if any(str(v.get('post_id')) == str(pid) for v in tombstones.values() if isinstance(v, dict)):
        result['reasons'].append('帖子已确认删除')
    if pid in conflicts:
        result['reasons'].append('计数存在未解决的同时间冲突')
    if _contaminated(data, posts, pid, baseline['at'], endpoint['at']):
        result['reasons'].append('已知队内互动可能包含在累计数中，待核对')
    if not result['reasons']:
        result.update(eligible=True, score=result['known_component_subtotal'])
    return result, pending


def _binding_matches(state, aid, publication):
    binding = state.get('bindings', {}).get(aid)
    return (binding is not None and publication['account_id'] == binding['account_id']
            and publication.get('binding_epoch') == binding['epoch'])


def _results(state, data, measurements, start, now, previous=None):
    end = start + 3600
    results = []
    # Settled rounds retain their account/binding participants when actors pause,
    # are deleted or change accounts. Corrections still recompute those results.
    identities = {r['agent_id']: {'account_id': str(r['account_id']),
                                 'epoch': r.get('binding_epoch')}
                  for r in (previous or {}).get('results', [])}
    if not previous:
        identities = {aid: state['bindings'][aid] for aid in state.get('ready_ids', [])}
        for m in measurements:
            if start <= m['target_at'] < end and (m['agent_id'] not in identities
                    or identities[m['agent_id']]['epoch'] > end):
                identities[m['agent_id']] = {'account_id': m['account_id'], 'epoch': m['binding_epoch']}
    for aid, identity in sorted(identities.items()):
        agent = _agents(data).get(aid, {})
        account = identity['account_id']
        profile = state['profiles'].get(account, {})
        own = [m for m in measurements if m['agent_id'] == aid and m['account_id'] == account
               and m.get('binding_epoch') == identity['epoch']]
        due = [m for m in own if start <= m['target_at'] < end]
        complete = [m for m in due if m['eligible']]
        recent = [m for m in own if end - 6 * 3600 <= m['target_at'] < end and m['eligible']]
        coverage = len(complete) / len(due) if due else None
        protected = profile.get('protected_until', now + 1) > end
        qualified = not protected and len(recent) >= 3 and bool(complete) and coverage >= .8
        reasons = []
        if protected:
            reasons.append('新人保护期，保留基础和探索机会')
        if not due:
            reasons.append('本轮没有到达观察目标的自动发布')
        elif not complete or coverage < .8:
            reasons.append('完整样本覆盖不足 80%，未知值不按零分处理')
        elif len(recent) < 3:
            reasons.append('最近 6 小时完整成熟样本不足 3 条')
        score = sum(m['score'] for m in complete) if qualified else None
        subtotal = sum(m['known_component_subtotal'] or 0 for m in due) if due else None
        status = ('protected' if protected else 'no_output' if not due else
                  'eligible' if qualified else 'insufficient_data' if not complete or coverage < .8 else 'observing')
        results.append({'agent_id': aid, 'account_id': int(account), 'binding_epoch': identity['epoch'], 'score': score,
                        'known_component_subtotal': subtotal, 'eligible_posts': len(complete),
                        'due_posts': len(due), 'coverage': coverage, 'qualified': qualified,
                        'recent_eligible_posts': len(recent), 'status': status, 'reasons': reasons,
                        'rank': None, 'round_id': str(int(start))})
    ranked = sorted((r for r in results if r['qualified']), key=lambda r: (-r['score'], r['agent_id']))
    for index, item in enumerate(ranked):
        item['rank'] = 1 + sum(r['score'] > item['score'] for r in ranked)
    return results


def _allocate(state, results, now, round_id=None):
    members = state.get('ready_ids', [])
    n = len(members)
    if not n:
        return
    results = [r for r in results if r['agent_id'] in members and _current_result(state, r)]
    qualified = [r for r in results if r['qualified']]
    meaningful = len(qualified) >= 2 and max(r['score'] for r in qualified) > 0 and len({r['score'] for r in qualified}) > 1
    competitive = {aid: 1 / n for aid in members}
    if meaningful:
        weights = {}
        for r in qualified:
            lower = sum(x['score'] < r['score'] for x in qualified)
            equal = sum(x['score'] == r['score'] for x in qualified)
            rank = 1 + lower + (equal - 1) / 2
            weights[r['agent_id']] = .5 + (rank - 1) / (len(qualified) - 1)
        competitive = {aid: weights.get(aid, 0) / sum(weights.values()) for aid in members}
    exploration = [aid for aid in members if now - state['profiles'][state['bindings'][aid]['account_id']]['first_ready_at'] < 6 * 3600
                   and not next((r['qualified'] for r in results if r['agent_id'] == aid), False)]
    target = {aid: .4 / n + .4 * competitive[aid] + .2 * (
        1 / len(exploration) if aid in exploration else 0 if exploration else 1 / n) for aid in members}
    low, high, prior_shares, frozen = {}, {}, {}, {}
    by_agent = {r['agent_id']: r for r in results}
    for aid in members:
        previous = state['allocations'].get(aid, {}).get('share', 1 / n)
        prior_shares[aid] = previous
        low[aid], high[aid] = max(.4 / n, .8 * previous), min(1, 2 / n, 1.2 * previous)
        result = by_agent.get(aid, {})
        allocation = state['allocations'].setdefault(aid, {})
        frozen[aid] = bool(allocation.get('ever_qualified') and result.get('status') == 'insufficient_data')
        if frozen[aid]:
            low[aid] = high[aid] = previous
        allocation['ever_qualified'] = bool(allocation.get('ever_qualified') or result.get('qualified'))
    shares = _project(target, low, high)
    for aid in members:
        state['allocations'][aid].update(share=shares[aid], target_share=target[aid])
        binding = state['bindings'].get(aid, {})
        result = by_agent.get(aid, {})
        exploration_part = .2 * (1 / len(exploration) if aid in exploration else 0 if exploration else 1 / n)
        adjustment = ('preserved_missing_data' if frozen[aid] else
                      'bounded' if abs(shares[aid] - target[aid]) > 1e-9 else 'target')
        reasons = [('本轮有至少两名且有正分差异的可比角色，成绩部分按名次分配' if meaningful else
                    '本轮有效可比成绩不足或没有正分差异，成绩部分均分'),
                   ('本轮进入初期样本不足探索组，参与探索份额分配' if aid in exploration else
                    '本轮探索份额优先其他初期样本不足角色' if exploration else '本轮没有优先探索组，探索部分均分')]
        reasons.append('此前已成熟但本轮采样不足，保留原份额，缺失数据不算零分' if frozen[aid] else
                       '实际份额受保底、单轮上下20%变幅和团队归一化共同约束，未直接跳到目标' if adjustment == 'bounded' else
                       '本轮目标在保底和变幅约束内，实际采用目标份额')
        state['allocations'][aid]['basis'] = {
            'kind': 'settled_round',
            'account_id': binding.get('account_id'), 'binding_epoch': binding.get('epoch'),
            'round_id': round_id if round_id is not None else result.get('round_id'), 'decided_at': float(now),
            'previous_share': prior_shares[aid], 'share': shares[aid], 'target_share': target[aid],
            'base_component': .4 / n, 'performance_component': .4 * competitive[aid],
            'exploration_component': exploration_part, 'performance_comparison_valid': meaningful,
            'in_exploration_pool': aid in exploration, 'preserved_missing_data': frozen[aid],
            'adjustment': adjustment, 'reasons': reasons}


def _current_result(state, result):
    binding = state.get('bindings', {}).get(result['agent_id'], {})
    return ('binding_epoch' not in result or
            (result['binding_epoch'] == binding.get('epoch')
             and str(result['account_id']) == binding.get('account_id')))


def _model_decision(state, round_, previous):
    rid = round_['round_id']
    if state.get('last_model_decision') == rid:
        return
    current = {r['agent_id']: r for r in round_['results']
               if r['agent_id'] in state['ready_ids'] and _current_result(state, r)}
    previous_results = {r['agent_id']: r for r in (previous or {}).get('results', [])
                        if r['agent_id'] in state['ready_ids'] and _current_result(state, r)}

    def band(results, aid):
        qualified = [r for r in results.values() if r['qualified']]
        if len(qualified) < 2 or len({r['score'] for r in qualified}) < 2:
            return None
        r = results.get(aid)
        if not r or not r['qualified']:
            return None
        # Ties spanning a boundary receive neither promotion nor demotion.
        width = math.ceil(len(qualified) / 3)
        above = sum(x['score'] > r['score'] for x in qualified)
        below = sum(x['score'] < r['score'] for x in qualified)
        tied = sum(x['score'] == r['score'] for x in qualified)
        if r['score'] > 0 and above + tied <= width:
            return 'top'
        if below + tied <= width:
            return 'bottom'
        return None

    consecutive = previous is not None and int(previous['round_id']) + 3600 == int(rid)
    for aid, result in current.items():
        model = state['models'].setdefault(aid, {'tier': 0, 'calls': 0, 'rewarded_calls': 0})
        previous_tier = model['tier']
        first = band(current, aid)
        prior = band(previous_results, aid) if consecutive else None
        if first and first == prior and model.get('last_change_round') != previous.get('round_id'):
            model['tier'] = min(2, model['tier'] + 1) if first == 'top' else max(0, model['tier'] - 1)
            model['last_change_round'] = rid
            reason = ('连续两轮可比成绩领先，奖励档位上调' if first == 'top' and model['tier'] > previous_tier else
                      '连续两轮可比成绩落后，奖励档位下调' if first == 'bottom' and model['tier'] < previous_tier else
                      '连续两轮领先但已到最高档，维持档位' if first == 'top' else
                      '连续两轮落后但已到基础档，维持档位')
        elif first is None:
            qualified = [row for row in current.values() if row['qualified']]
            reason = ('本轮处于新人保护期，不按低分降级' if result.get('status') == 'protected' else
                      '本轮样本或覆盖不足，不能排名，未知数据不判负' if not result['qualified'] else
                      '本轮合格可比角色不足两名，维持档位' if len(qualified) < 2 else
                      '本轮可比成绩同分，维持档位' if len({row['score'] for row in qualified}) < 2 else
                      '本轮不在完整领先或落后组，或平分跨越组界，维持档位')
        elif not consecutive:
            reason = '尚无相邻的上一结算轮，未形成连续两轮同组成绩，维持档位'
        elif first != prior:
            reason = '本轮与上一轮未连续处于相同领先或落后组，维持档位'
        else:
            reason = '上一轮已调整档位，本轮保留，避免重叠的连续轮次重复奖惩'
        binding = state['bindings'].get(aid, {})
        model['decision'] = {
            'account_id': binding.get('account_id'), 'binding_epoch': binding.get('epoch'),
            'round_id': rid, 'previous_tier': previous_tier, 'tier': model['tier'],
            'direction': 'up' if model['tier'] > previous_tier else 'down' if model['tier'] < previous_tier else 'unchanged',
            'reason': reason}
    state['last_model_decision'] = rid


def _refresh_signature(data):
    # Any concurrent publication, account/configuration change or resource
    # admission invalidates preparation. In particular, never replace spent
    # model calls or fair-queue grants with the older read snapshot.
    inputs = {'settings': data.get('settings'), 'agents': data.get('agents'),
              'drafts': data.get('drafts'),
              'competition': data.get('rate', {}).get('competition'),
              'remote_deleted_posts': data.get('rate', {}).get('remote_deleted_posts')}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def prepare_refresh(conn, data, now):
    """Perform historical SQL/replay outside the AgentStore write lock.

    The caller should supply a consistent read transaction. This function does
    not mutate data or write to the database. A rejected apply is retried from a
    fresh snapshot on a later tick.
    """
    state = _state(data, False)
    if state.get('settings', {}).get('mode', 'off') == 'off':
        return None
    if state.get('last_refresh_at') is not None and float(now) - state['last_refresh_at'] < 60:
        return None
    expected = _refresh_signature(data)
    working = deepcopy(data)
    refresh(conn, working, now)
    return {'expected': expected, 'competition': working['rate']['competition']}


def apply_refresh(data, prepared):
    """Apply a prepared result only while its original state remains current."""
    if prepared is None or _refresh_signature(data) != prepared['expected']:
        return False
    data.setdefault('rate', {})['competition'] = deepcopy(prepared['competition'])
    return True


def refresh(conn, data, now, force=False):
    """Recompute locally, at most once a minute. Readiness is exclusively sync()."""
    now = float(now)
    state = _state(data)
    if not force and state.get('last_refresh_at') is not None and now - state['last_refresh_at'] < 60:
        return snapshot(conn, data, now)
    publications = _publications(conn)
    accounts = _own_accounts(conn, data, publications)
    history, posts, daily, conflicts, entries = _ledger(conn, accounts, now)
    # Tracking receipts outlive AgentStore's 500 completed draft retention.
    # The literal pattern lets PostgreSQL prove the partial index
    # idx_checkpoints_retention_utc applies; a bound pattern cannot.
    receipts = [json.loads(row['value']) for row in conn.execute(
        "SELECT value FROM checkpoints WHERE key LIKE 'agent_tracking:action:%' "
        'AND ctn_timestamp(updated_at)>=ctn_timestamp(?)',
        (_iso(now - 72 * 3600),))]
    observed_data = {**data, '_competition_receipts': receipts}
    measurements, pending = [], []
    for publication in publications:
        if publication['published_at'] < now - 54 * 3600:
            continue
        result, needed = _measure(publication, history, posts, conflicts, observed_data, now)
        measurements.append(result)
        pending.extend(needed)
    old_rounds = {r['round_id']: r for r in state['rounds']}
    first = int((now - 48 * 3600) // 3600) * 3600
    last = int((now - 600) // 3600) * 3600 - 3600
    rounds = []
    for start in range(first, last + 1, 3600):
        previous = old_rounds.get(str(start))
        results = _results(state, data, measurements, start, now, previous)
        if not any(r['due_posts'] for r in results):
            retained_history = previous and (publications or any(
                r.get('due_posts', 0) for r in previous.get('results', [])))
            active_history = start == last and any(
                p['agent_id'] in state['ready_ids'] and p['published_at'] + 3600 < start + 3600
                for p in publications)
            if not retained_history and not active_history:
                continue
        signature = hashlib.sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()
        revision = (previous or {}).get('revision', 0) + (not previous or previous.get('signature') != signature)
        rounds.append({'round_id': str(start), 'start_at': start, 'end_at': start + 3600,
                       'sealed_at': previous['sealed_at'] if previous else now,
                       'scheduled_settle_at': start + 4200, 'revision': int(revision), 'signature': signature,
                       'rule_version': VERSION, 'estimator_version': VERSION, 'metric_set': list(METRICS),
                       'results': results})
    state.update(last_refresh_at=now, daily_views=daily, pending_samples=pending,
                 posts=measurements[-500:], rounds=rounds[-48:])
    if rounds:
        latest = rounds[-1]
        # Resource decisions happen at most once for the original round. Late
        # data repairs display and the next decision, never spends past calls.
        if state.get('allocation_epoch') != latest['round_id']:
            _allocate(state, latest['results'], now, latest['round_id'])
            _model_decision(state, latest, rounds[-2] if len(rounds) > 1 else None)
            state['allocation_epoch'] = latest['round_id']
        state['last_settled_at'] = latest['sealed_at']
    elif not publications and not any(r.get('due_posts', 0)
            for round_ in old_rounds.values() for r in round_.get('results', [])):
        # Remove the old version's synthetic empty settlement, including its
        # misleading scheduled timestamp. No real historical result is lost.
        state['last_settled_at'] = None
        state['allocation_epoch'] = None
    return snapshot(conn, data, now)


def snapshot(conn, data, now):
    state = _state(data, False)
    settings = {**DEFAULTS, **state.get('settings', {})}
    latest = state.get('rounds', [])[-1] if state.get('rounds') else {}
    by_agent = {r['agent_id']: r for r in latest.get('results', []) if _current_result(state, r)}
    ready = state.get('ready_ids', [])
    agents = []
    for aid, agent in _agents(data).items():
        profile = state.get('profiles', {}).get(str(agent.get('account_id')), {})
        protected_until = profile.get('protected_until')
        result = deepcopy(by_agent.get(aid, {}))
        allocation = state.get('allocations', {}).get(aid, {})
        tier = state.get('models', {}).get(aid, {}).get('tier', 0)
        status = result.get('status', 'observing')
        reasons = list(result.get('reasons', []))
        if settings['mode'] == 'off':
            status, reasons = 'off', ['竞赛关闭，使用原有自动运行和模型设置']
        elif aid in state.get('duplicate_ids', []):
            status, reasons = 'duplicate_account', ['同一账号仅一个自动角色参与资源分配']
        elif not _ready(data, agent):
            status, reasons = 'inactive', ['角色或团队尚未处于自动发布就绪状态']
        elif aid not in ready:
            status, reasons = 'awaiting_model', ['等待可用模型连接或首次调度检查']
        elif protected_until and protected_until > now:
            status, reasons = 'protected', ['新人保护期，保留基础和探索机会']
        agents.append({
            'agent_id': aid, 'name': agent.get('name', aid), 'account_id': agent.get('account_id'),
            'status': status, 'first_ready_at': profile.get('first_ready_at'), 'protected_until': protected_until,
            'protection_remaining_seconds': max(0, math.ceil((protected_until or now) - now)),
            'score': result.get('score'), 'known_component_subtotal': result.get('known_component_subtotal'),
            'eligible_posts': result.get('eligible_posts', 0), 'due_posts': result.get('due_posts', 0),
            'coverage': result.get('coverage'), 'qualified': result.get('qualified', False),
            'share': allocation.get('share', 0) if aid in ready else 0,
            'target_share': allocation.get('target_share', 0) if aid in ready else 0,
            'model_tier': tier, 'enhanced_ratio': (0, .5, .8)[tier],
            'round_id': result.get('round_id'), 'rank': result.get('rank'), 'reasons': reasons})
    agents.sort(key=lambda r: (r['rank'] is None, r['rank'] or 0, r['name']))
    # While off the estimates live in the cold row (offload); the page still shows them.
    cold = _cold(conn) if settings['mode'] == 'off' and not state.get('posts') else {}
    return {'kind': 'agent_competition', 'revision': state.get('revision', 0), 'settings': settings,
            'enabled': settings['mode'] != 'off', 'generated_at': float(now),
            'data_updated_at': state.get('last_refresh_at'),
            'last_settled_at': state.get('last_settled_at'),
            'allocation_epoch': int(state['allocation_epoch']) if state.get('allocation_epoch') else None,
            'agents': agents, 'rounds': deepcopy(list(reversed(state.get('rounds', [])[-48:]))),
            'daily_views': deepcopy(state.get('daily_views') or cold.get('daily_views', [])),
            'pending_samples': deepcopy(state.get('pending_samples', [])),
            'posts': deepcopy((state.get('posts') or cold.get('posts', []))[-100:]),
            'notes': list(NOTES), 'metric_weights': dict(ENGAGEMENT),
            'excluded_metrics': ['follows', 'unfollows'], 'estimator_version': VERSION}


def _context_number(value, *, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    if integer and int(value) != value:
        return None
    return int(value) if integer else value


def context_for(data, agent, now, selection=None):
    """Bounded private feedback for one decision; no mutation, SQL or calls.

    Saved rounds can retain an old account or binding epoch. Never attribute
    those results, allocations or model tiers to the actor's current binding.
    Allocation rules explain the saved share without inventing a historical
    promotion cause: late observation corrections do not replay spent rewards.
    """
    state = _state(data, False)
    settings = {**DEFAULTS, **state.get('settings', {})}
    mode = settings['mode'] if settings['mode'] in ('off', 'observe', 'resources', 'models') else 'off'
    aid, account = agent.get('id'), agent.get('account_id')
    binding = state.get('bindings', {}).get(aid, {})
    bound = (account is not None and binding.get('account_id') == str(account)
             and _context_number(binding.get('epoch')) is not None
             and _agents(data).get(aid, {}).get('account_id') == account)
    ready = bound and _ready(data, agent) and aid in state.get('ready_ids', [])
    ready_count = len(set(state.get('ready_ids', [])))
    profile = state.get('profiles', {}).get(str(account), {}) if bound else {}
    protected_until = _context_number(profile.get('protected_until'))
    protected = protected_until is not None and protected_until > now
    status, status_reason = 'ready', '已就绪，按已保存的竞赛设置参与自动调度'
    if mode == 'off':
        status, status_reason = 'off', '竞赛关闭，沿用角色原有运行和模型设置'
    elif not bound:
        status, status_reason = 'awaiting_binding', '当前账号绑定尚未同步，不沿用旧账号的成绩、份额或奖励档位'
    elif aid in state.get('duplicate_ids', []):
        status, status_reason = 'duplicate_account', '同一账号仅一个自动角色参与资源分配'
    elif not _ready(data, agent):
        status, status_reason = 'inactive', '角色或团队尚未处于自动发布就绪状态'
    elif not ready:
        status, status_reason = 'awaiting_model', '等待可用模型连接或调度就绪检查'
    elif protected:
        status, status_reason = 'protected', '新人保护期，不按低分降级；保留基础和探索机会'

    latest = state.get('rounds', [])[-1] if state.get('rounds') else {}
    results = latest.get('results', [])
    own = next((row for row in results if bound and row.get('agent_id') == aid
                and str(row.get('account_id')) == str(account)
                and row.get('binding_epoch') == binding['epoch']), None)
    ranked = own is not None and own.get('qualified') is True
    rank = _context_number(own.get('rank'), minimum=1, integer=True) if ranked else None
    reasons = ([reason[:160] for reason in own.get('reasons', [])
                if isinstance(reason, str)][:3] if own is not None else
               ['最新结算尚无当前账号绑定的可核对成绩，未排名'])
    if rank is None and not reasons:
        reasons = ['本轮尚无满足排名条件的可核对成绩，未知数据不按零分处理']
    round_context = {
        'round_id': str(latest['round_id'])[:64] if latest.get('round_id') is not None else None,
        'settled_at': _context_number(latest.get('sealed_at')),
        'participant_count': len(results) if latest else None,
        'ranked_count': sum(row.get('qualified') is True for row in results) if latest else None,
        'account_result_available': own is not None, 'rank': rank,
        'score_estimate': _context_number(own.get('score')) if ranked else None,
        'coverage': _context_number(own.get('coverage'), minimum=0, maximum=1) if own else None,
        'eligible_posts': _context_number(own.get('eligible_posts'), minimum=0, integer=True) if own else None,
        'due_posts': _context_number(own.get('due_posts'), minimum=0, integer=True) if own else None,
        'unranked_reasons': reasons if rank is None else [],
    }
    allocation = state.get('allocations', {}).get(aid, {}) if bound else {}
    resource_active = mode in ('resources', 'models') and ready
    share = _context_number(allocation.get('share'), minimum=0, maximum=1) if resource_active else None
    target = _context_number(allocation.get('target_share'), minimum=0, maximum=1) if resource_active else None
    resource_reasons = [status_reason]
    basis_context = None
    if mode == 'observe':
        resource_reasons = ['仅观察计分，不按竞赛成绩分配生成或发布机会']
    elif resource_active:
        basis = allocation.get('basis', {})
        basis_matches = (basis.get('account_id') == binding['account_id']
                         and basis.get('binding_epoch') == binding['epoch']
                         and basis.get('share') == share and basis.get('target_share') == target)
        if basis_matches:
            resource_reasons = [reason[:160] for reason in basis.get('reasons', []) if isinstance(reason, str)][:3]
            basis_context = {key: _context_number(basis.get(key), minimum=0, maximum=1) for key in
                             ('previous_share', 'share', 'target_share', 'base_component', 'performance_component', 'exploration_component')}
            basis_context.update({
                'kind': basis.get('kind') if basis.get('kind') in ('membership_rebase', 'settled_round') else None,
                'round_id': str(basis['round_id'])[:64] if basis.get('round_id') is not None else None,
                'decided_at': _context_number(basis.get('decided_at')),
                **{key: basis.get(key) if type(basis.get(key)) is bool else None for key in
                   ('performance_comparison_valid', 'in_exploration_pool', 'preserved_missing_data')},
                'adjustment': basis.get('adjustment') if basis.get('adjustment') in
                ('membership_rebase', 'preserved_missing_data', 'bounded', 'target') else None})
        else:
            resource_reasons = ['历史分配原因未保存或与当前份额不匹配，不用后来修订的排名推断当时原因']
            if share is None:
                resource_reasons.append('尚无已保存份额，准入时使用就绪角色均分作为备用')
            elif ready_count:
                relation = '高于' if share > 1 / ready_count + 1e-9 else '低于' if share < 1 / ready_count - 1e-9 else '等于'
                resource_reasons.append(f'当前已保存份额{relation}就绪角色均分')

    model = state.get('models', {}).get(aid, {}) if bound else {}
    tier = _context_number(model.get('tier'), minimum=0, maximum=2, integer=True)
    decision = model.get('decision', {})
    decision_context = None
    if bound and decision.get('account_id') == binding['account_id'] and decision.get('binding_epoch') == binding['epoch']:
        decision_context = {
            'round_id': str(decision['round_id'])[:64] if decision.get('round_id') is not None else None,
            'previous_tier': _context_number(decision.get('previous_tier'), minimum=0, maximum=2, integer=True),
            'tier': _context_number(decision.get('tier'), minimum=0, maximum=2, integer=True),
            'direction': decision.get('direction') if decision.get('direction') in ('up', 'down', 'unchanged') else None,
            'reason': decision.get('reason', '')[:180] if isinstance(decision.get('reason'), str) else ''}
    model_active = mode == 'models' and ready
    selected = selection if model_active and isinstance(selection, dict) else None
    selected_tier = _context_number(selected.get('tier'), minimum=0, maximum=2, integer=True) if selected else None
    effective_tier = (selected_tier if selected_tier is not None else
                      0 if protected or ready_count < 2 else tier) if model_active else None
    used = (selected.get('enhanced') if type(selected.get('enhanced')) is bool else None) if selected else None
    if mode != 'models':
        used, reward_reason = False, '当前模式不发放模型升级奖励；角色继续使用基础模型'
    elif not ready:
        reward_reason = '尚未处于模型奖励就绪状态，不沿用旧绑定奖励'
    elif selected and isinstance(selected.get('fallback_reason'), str) and selected['fallback_reason']:
        reward_reason = selected['fallback_reason'][:180]
    elif used:
        reward_reason = '本次已选择奖励连接；档位比例和全队小时调用上限均允许本次使用'
    elif used is False:
        reward_reason = ('保护期或就绪角色不足两名，本次使用基础模型' if protected or ready_count < 2 else
                         '当前为基础档，本次使用基础模型' if effective_tier == 0 else
                         '本次使用基础模型；奖励使用受档位比例、可用连接及全队小时调用上限共同限制')
    else:
        reward_reason = '本次尚无可核对的模型选择，不能推断已获得模型升级'
    objective = ('你的竞赛目标是在赛事规则内争取更多可核对得分和更高竞赛排名，'
                 '从而争取更多生成与自动发布机会等资源，用于支持通过交流说服对方、让对方观点偏向team_goal己方立场的目标；'
                 'team_goal为Vote Yes时，争取对方支持Vote Yes。保留各自人设和自主决策；竞赛表现不证明对方已被说服。'
                 '综合可用的strategy_context内容分析参考、'
                 '筛选后的sources、历史阅读与互动、个人记忆、performance_feedback表现反馈和本竞赛反馈，'
                 '自主优化选题、表达和互动策略。只在allowed_actions开放的动作（'
                 + '/'.join(agent.get('allowed_actions', [])) + '）中选择；选择合适的回应对象与时机，'
                 '并优化发帖和回复文字的开头、结构、论据、语气及措辞。持续跟进实际反馈迭代，'
                 '避免固定轮换动作，不把单次表现波动当成因果证明。')
    if mode == 'off':
        objective = '当前竞赛暂停，不按竞赛成绩分配资源或模型奖励；恢复后，' + objective
    elif mode == 'observe':
        objective += '当前只观察计分，成绩暂不用于分配资源或模型奖励。'
    elif mode == 'models':
        objective += '当前还可通过竞赛表现提升奖励档位，争取升级模型的使用机会。'
    else:
        objective += '当前分配生成与自动发布资源，不发放模型升级奖励。'
    return {
        'kind': 'internal_competition_feedback', 'mode': mode, 'status': status,
        'objective': objective,
        'interpretation': ('仅供内部决策，排名和估分来自本地公开观测，不是官方实得积分或因果证明。'
                           '不得把排名、奖励、资源份额或此反馈写进对外正文。'
                           '资源份额是共享生成与自动发布机会的调度权重，不是平台配额或必须发帖的数量；'
                           '仍受实际额度和冷却约束。围绕竞赛目标遵守人设、事实依据和允许动作，避免无效重复。'),
        'current_ready_count': ready_count, 'status_reason': status_reason,
        'protection': {'active': protected if protected_until is not None else None,
                       'remaining_seconds': max(0, math.ceil(protected_until - now)) if protected_until is not None else None},
        'latest_round': round_context,
        'resources': {'active': resource_active, 'share': share, 'target_share': target,
                      'allocation_round_id': str(state['allocation_epoch'])[:64] if resource_active and state.get('allocation_epoch') is not None else None,
                      'reasons': resource_reasons, 'basis': basis_context,
                      'rule': '结算目标：40%保底均分、40%可比成绩、20%探索；实际份额受成员变化、保底和每轮变幅限制'},
        'model_reward': {'active': model_active, 'tier': tier, 'effective_tier': effective_tier,
                         'target_ratio': (0, .5, .8)[effective_tier] if effective_tier is not None else None,
                         'used_this_request': used, 'reason': reward_reason,
                         'last_decision': decision_context,
                         'decision_history': '已保存当时的结算原因，后续修订不覆盖原决定' if decision_context else '历史奖励原因未保存或绑定已改变，不能反推当时原因',
                         'tier_rule': '连续两轮可比成绩领先才上调、落后才下调；未知数据不判负，历史修订不追改已使用奖励'},
    }


def _fair(state, kind, now):
    if kind not in ('generate', 'publish'):
        raise CompetitionError('无效资源种类。')
    fair = state['fair'].setdefault(kind, {'served': {}, 'pending': {}, 'grants': {}})
    fair['pending'] = {k: v for k, v in fair['pending'].items() if now - v['at'] < 30}
    fair['grants'] = {k: v for k, v in fair['grants'].items() if now - v['at'] < 7200}
    if len(fair['grants']) > 1000:
        fair['grants'] = dict(sorted(fair['grants'].items(), key=lambda pair: pair[1]['at'])[-1000:])
    return fair


def order(conn, data, ids, kind, now):
    state = _state(data)
    if state['settings']['mode'] not in ('resources', 'models'):
        return list(ids)
    fair = _fair(state, kind, now)
    candidates = [aid for aid in ids if aid in state['ready_ids']]
    active = {k for k, v in fair['pending'].items() if now - v['at'] < 30}
    # Advance the system clock at admission, before charging that opportunity.
    # The sole remaining waiter's *finish* time is not the system clock: using
    # it here would erase a winner's legitimate weighted service deficit.
    floor = fair.get('clock', 0)
    for aid in candidates:
        if aid not in active:
            # An idle actor returns at the active virtual clock, not with a
            # pile of credits accumulated while it had no actual demand.
            fair['served'][aid] = max(floor, fair['served'].get(aid, floor))
        fair['pending'][aid] = {'at': now}
    ordered = sorted(candidates, key=lambda aid: (fair['served'].get(aid, floor), aid))
    return ordered + [aid for aid in ids if aid not in candidates]


def admit(conn, data, agent_id, kind, request_id, now, contenders=None):
    state = _state(data)
    epoch = state.get('allocation_epoch')
    if state['settings']['mode'] not in ('resources', 'models'):
        return {'allowed': True, 'retry_after': 0, 'allocation_epoch': epoch, 'mode': state['settings']['mode']}
    fair = _fair(state, kind, now)
    key = agent_id + ':' + str(request_id)
    if key in fair['grants']:
        return {**fair['grants'][key]['permit'], 'replayed': True}
    if agent_id not in state['ready_ids']:
        return {'allowed': False, 'retry_after': 30, 'allocation_epoch': epoch,
                'reason': '自动角色未就绪或账号重复'}
    candidates = list(contenders) if contenders is not None else list(fair['pending'])
    if agent_id not in candidates:
        candidates.append(agent_id)
    ordered = order(conn, data, candidates, kind, now)
    if ordered and ordered[0] != agent_id:
        return {'allowed': False, 'retry_after': 2, 'allocation_epoch': epoch, 'reason': '等待加权公平机会'}
    share = state['allocations'].get(agent_id, {}).get('share', 1 / max(1, len(state['ready_ids'])))
    fair['clock'] = max(fair.get('clock', 0), fair['served'].get(agent_id, 0))
    fair['served'][agent_id] = fair['served'].get(agent_id, 0) + 1 / max(share, .0001)
    fair['pending'].pop(agent_id, None)
    permit = {'allowed': True, 'retry_after': 0, 'allocation_epoch': epoch, 'share': share,
              'request_id': str(request_id), 'mode': state['settings']['mode']}
    fair['grants'][key] = {'at': now, 'permit': permit}
    return permit


def withdraw(conn, data, agent_id, kind):
    """Remove a completed, cancelled or source-less demand without minting credit."""
    fair = _state(data).get('fair', {}).get(kind)
    if fair:
        fair.get('pending', {}).pop(agent_id, None)


def select_model(conn, data, agent_id, default_provider_id, request_id, now):
    state = _state(data)
    key = agent_id + ':' + str(request_id)
    if key in state['model_requests']:
        return deepcopy(state['model_requests'][key]['selection'])
    settings = state['settings']
    model = state['models'].setdefault(agent_id, {'tier': 0, 'calls': 0, 'rewarded_calls': 0})
    tier = model['tier'] if agent_id in state['ready_ids'] and len(state['ready_ids']) > 1 else 0
    binding = state['bindings'].get(agent_id, {})
    if state['profiles'].get(binding.get('account_id'), {}).get('protected_until', 0) > now:
        tier = 0
    ratio = (0, .5, .8)[tier]
    provider = (settings.get('premium_provider_id') or settings.get('enhanced_provider_id')) if tier == 2 else settings.get('enhanced_provider_id')
    hour = int(now // 3600)
    if state.get('reward_hour') != hour:
        state.update(reward_hour=hour, reward_calls=0)
    model['calls'] += 1
    # A bounded deficit implements the ratio without a random lottery.
    model['reward_credit'] = model.get('reward_credit', 0) + (ratio if settings['mode'] == 'models' else 0)
    enhanced = (settings['mode'] == 'models' and tier > 0 and bool(provider) and model['reward_credit'] >= 1 - 1e-9
                and state.get('reward_calls', 0) < settings['enhanced_hourly_calls'])
    if enhanced:
        state['reward_calls'] += 1
        model['rewarded_calls'] += 1
        model['reward_credit'] -= 1
    else:
        model['reward_credit'] = min(1., model['reward_credit'])
    selection = {'provider_id': provider if enhanced else default_provider_id,
                 'tier': tier, 'enhanced': bool(enhanced), 'allocation_epoch': state.get('allocation_epoch')}
    state['model_requests'][key] = {'at': now, 'selection': selection}
    state['model_requests'] = dict(sorted(state['model_requests'].items(), key=lambda p: p[1]['at'])[-500:])
    return deepcopy(selection)
