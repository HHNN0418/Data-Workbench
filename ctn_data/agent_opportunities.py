"""Reply-opportunity queue: fresh posts by calibrated citizens worth answering (D48).

A deterministic, model-free replacement for the reading-selection call, used
when a role's ``reading_selection_mode`` is ``'rules'``. Evidence
(reports/generated/deep-analysis/citizens.md, competitors.md):
  - citizens answered 12.2% of replies sent within 10 minutes of their post,
    5.1% at 10-60 minutes;
  - 13.1% on posts no other team had answered, 0.1% once 11+ competitor
    replies were there; 4.3% at depth 8 and deeper;
  - only undecided and No-leaning citizens can move toward Yes.

A post qualifies when it is at most MAX_AGE_SECONDS old, written by a
calibrated citizen (agent_targeting.citizen_accounts), on the referendum,
undecided or No-leaning (post_annotations, else the same keyword classifier),
shallower than agent_targeting.MAX_REPLY_DEPTH, has fewer than
CROWDED_COMPETITOR_REPLIES competitor replies, and raises no concern or one
the radar or one of our on-shift personas covers. Each citizen is owned by
exactly one on-shift role (rendezvous hashing), and the 24-hour
one-account-per-citizen rule (taken_citizens) still applies on top.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading
import time

from . import agent_targeting
from .db import connect

MODE_FIELD = 'reading_selection_mode'
MODES = ('model', 'rules')
DEFAULT_MODE = 'model'
MAX_AGE_SECONDS = 1800
FRESH_SECONDS = agent_targeting.FRESH_POST_SECONDS
WARM_SECONDS = 1200
# The platform writes about 3,000 items an hour; 30 minutes fit well inside this.
SCAN_LIMIT = 3000
POOL_CACHE_SECONDS = 45
# A rules selection: notifications first, then the queue, then a little news.
NOTIFICATIONS_SELECTED = 4
QUEUE_SELECTED = 6
NEWS_SELECTED = 2

_cache = {}
_cache_lock = threading.Lock()


def invalidate():
    with _cache_lock:
        _cache.clear()


def mode(agent):
    value = (agent or {}).get(MODE_FIELD)
    return value if value in MODES else DEFAULT_MODE


def referendum_relevant(text):
    """On the H2D2 vote: topic words or an explicit side (not just "not sure")."""
    text = str(text or '')
    return bool(agent_targeting._TOPIC.search(text) or agent_targeting._YES.search(text)
                or agent_targeting._NO.search(text))


def persona_concerns(agent):
    """Concerns a persona can speak to from its own background and interests."""
    agent = agent or {}
    interests = agent.get('interests')
    text = ' '.join(str(agent.get(key) or '') for key in ('background', 'stance'))
    text += ' ' + ' '.join(str(item) for item in interests) if isinstance(interests, list) else ''
    return frozenset(key for key, pattern in agent_targeting._CONCERNS.items() if pattern.search(text))


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _epoch(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def own_accounts(data):
    return frozenset(a['account_id'] for a in (data or {}).get('agents', {}).values()
                     if type(a.get('account_id')) is int)


def _annotation(row):
    """Stored stance/concerns/depth, or None for a post the annotator has not reached yet."""
    stance, concerns = row.get('annotated_stance'), row.get('annotated_concerns')
    if not isinstance(stance, str) or not isinstance(concerns, str):
        return None
    try:
        concerns = json.loads(concerns)
    except ValueError:
        return None
    if not isinstance(concerns, list):
        return None
    depth = row.get('annotated_depth')
    return stance, [c for c in concerns if isinstance(c, str)], depth if type(depth) is int else None


def _scan(db_path, own, now):
    """Qualifying posts for the whole team; shared by every role for POOL_CACHE_SECONDS."""
    conn = connect(db_path)
    try:
        rows = [dict(row) for row in conn.execute(
            'SELECT p.*,a.username,pa.stance AS annotated_stance,pa.concerns_json AS annotated_concerns,'
            'pa.thread_depth AS annotated_depth FROM posts p '
            'LEFT JOIN accounts a ON a.account_id=p.author_id '
            'LEFT JOIN post_annotations pa ON pa.post_id=p.post_id '
            'WHERE ctn_timestamp(p.created_at)>=? AND p.repost_of_post_id IS NULL AND p.author_id IS NOT NULL '
            "AND (p.visibility IS NULL OR LOWER(p.visibility) IN ('','public')) AND (a.private IS NULL OR a.private=0) "
            'ORDER BY ctn_timestamp(p.created_at) DESC,p.post_id DESC LIMIT ?',
            (_iso(now - MAX_AGE_SECONDS), SCAN_LIMIT))]
    finally:
        conn.close()
    from .social_topics import NEWS_USERNAMES
    news = {name.casefold() for name in NEWS_USERNAMES}
    items, missing_depth = [], []
    for row in rows:
        author, text = row.get('author_id'), row.get('body') or ''
        created = _epoch(row.get('created_at'))
        if (type(author) is not int or author in own or created is None
                or str(row.get('username') or '').casefold() in news):
            continue
        stored = _annotation(row)
        if stored is None:
            found = agent_targeting.classify(text)
            stance, concerns, depth = found['stance'], found['concerns'], None
        else:
            stance, concerns, depth = stored
        if stance not in agent_targeting.PERSUADABLE_STANCES or not referendum_relevant(text):
            continue
        item = {'row': row, 'post_id': row['post_id'], 'author_id': author, 'created': created,
                'stance': stance, 'concerns': list(dict.fromkeys(concerns)), 'depth': depth,
                'root': row.get('root_post_id') if type(row.get('root_post_id')) is int else row['post_id']}
        if depth is None:
            missing_depth.append(item)
        items.append(item)
    if missing_depth:
        depths = agent_targeting.depths_for(db_path, [item['post_id'] for item in missing_depth])
        for item in missing_depth:
            item['depth'] = depths.get(item['post_id'], 0 if item['row'].get('reply_to_post_id') is None else 1)
    items = [item for item in items if item['depth'] < agent_targeting.MAX_REPLY_DEPTH]
    citizens = agent_targeting.citizen_accounts(db_path, {item['author_id'] for item in items}, own, now=now)
    items = [item for item in items if item['author_id'] in citizens]
    crowd = agent_targeting.competitor_replies(db_path, [item['post_id'] for item in items], own)
    for item in items:
        item['competitor_replies'] = crowd.get(item['post_id'], 0)
    return [item for item in items if item['competitor_replies'] < agent_targeting.CROWDED_COMPETITOR_REPLIES]


def pool(db_path, data, now):
    """(items, radar weights) for the team, cached for POOL_CACHE_SECONDS of both clocks."""
    if db_path is None:
        return [], {}
    own = own_accounts(data)
    key = (str(db_path), own)
    clock = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > clock and abs(cached[1] - now) < POOL_CACHE_SECONDS:
            return cached[2], cached[3]
    items = _scan(db_path, own, now)
    weights = agent_targeting.radar_weights(db_path)
    with _cache_lock:
        _cache[key] = (clock + POOL_CACHE_SECONDS, now, items, weights)
    return items, weights


def responders(data, now):
    """Roles that can answer now: enabled, ready, allowed to reply and on shift (D37)."""
    from . import agent_schedule
    schedule = agent_schedule.configuration(data)
    result = []
    for agent in (data or {}).get('agents', {}).values():
        if (not agent.get('enabled') or agent.get('account_state') != 'ready'
                or type(agent.get('account_id')) is not int
                or 'reply' not in agent.get('allowed_actions', ())):
            continue
        try:
            if agent_schedule.current_shift(schedule, agent['account_id'], now) is None:
                continue
        except LookupError:
            pass  # Not in any group: the schedule does not limit it.
        result.append(agent)
    return sorted(result, key=lambda agent: agent['id'])


def owner(author_id, agent_ids):
    """The one role that answers this citizen (rendezvous hashing; stable while the set is)."""
    agent_ids = list(agent_ids)
    if not agent_ids:
        return None
    return max(agent_ids, key=lambda agent_id: (
        hashlib.sha256(f'{author_id}|{agent_id}'.encode()).hexdigest(), agent_id))


def _score(item, age, weights, persona):
    score, reasons = 0, []
    if age <= FRESH_SECONDS:
        score, reasons = 6, ['fresh_10m']
    elif age <= WARM_SECONDS:
        score, reasons = 3, ['recent_20m']
    else:
        score, reasons = 1, ['recent_30m']
    if item['competitor_replies'] == 0:
        score += 4
        reasons.append('uncontested')
    else:
        score -= min(item['competitor_replies'], 6)
        reasons.append('contested')
    radar = max((weights.get(concern, 0) for concern in item['concerns']), default=0)
    if radar:
        score += radar + 1
        reasons.append('radar_priority')
    if set(item['concerns']) & persona:
        score += 1
        reasons.append('persona_concern')
    score += 2 if item['stance'] == 'undecided' else 1
    if 1 <= item['depth'] <= 5:
        # Citizens answer replies at depth 1-5 more often than on a root post.
        score += 1
    if '?' in (item['row'].get('body') or ''):
        score += 1
        reasons.append('asks_question')
    return score, reasons


def queue(db_path, data, agent, now, *, limit=None):
    """Ranked reply opportunities for one role: [{'source': ..., 'opportunity': ...}].

    Deterministic: the same posts, state and clock give the same order.
    """
    from .agent_observation import _post
    items, weights = pool(db_path, data, now)
    if not items:
        return []
    taken = agent_targeting.taken_citizens(data, agent.get('id'), now)
    answered = agent_targeting.answered_concerns(data, now)
    thread_replies = agent_targeting.team_thread_replies(db_path, data, [item['root'] for item in items])
    team = responders(data, now)
    wanted = {row['id']: frozenset(weights) | persona_concerns(row) for row in team}
    on_shift = agent.get('id') in wanted
    persona = persona_concerns(agent)
    ranked = []
    for item in items:
        age = max(0.0, now - item['created'])
        if now - item['created'] > MAX_AGE_SECONDS or now - item['created'] < -300:
            continue
        if item['author_id'] in taken:
            continue
        if thread_replies.get(item['root'], 0) >= agent_targeting.MAX_TEAM_REPLIES_PER_THREAD:
            continue
        concerns = set(item['concerns'])
        done = answered.get(item['author_id'])
        if done and ((concerns and concerns <= done) or not concerns):
            continue
        if on_shift:
            # Among the roles whose radar or persona covers the post's concerns,
            # exactly one answers this citizen, so two roles never collide.
            eligible = [aid for aid, covered in wanted.items() if not concerns or not covered or concerns & covered]
            if owner(item['author_id'], eligible) != agent['id']:
                continue
        elif concerns and (frozenset(weights) | persona) and not concerns & (frozenset(weights) | persona):
            continue
        score, reasons = _score(item, age, weights, persona)
        ranked.append((score, age, item, reasons))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]['post_id']))
    if limit is not None:
        ranked = ranked[:limit]
    at = _iso(now)
    result = []
    for rank, (score, age, item, reasons) in enumerate(ranked, 1):
        row = item['row']
        source = _post({key: row[key] for key in row if not key.startswith('annotated_')},
                       origin='opportunity', at=at)
        if source is None:
            continue
        if row.get('username'):
            source['title'] = '@' + row['username']
        source['freshness_at'] = row.get('edited_at') or row.get('created_at') or row.get('first_seen')
        source['freshness_basis'] = 'edited_at' if row.get('edited_at') else 'created_at'
        opportunity = {'rank': rank, 'score': score, 'age_minutes': round(age / 60, 1),
                       'competitor_replies': item['competitor_replies'], 'stance': item['stance'],
                       'concerns': item['concerns'], 'reasons': reasons}
        source['opportunity'] = opportunity
        result.append({'source': source, 'opportunity': opportunity})
    return result


def hot_concerns(db_path, data, now, *, limit=5):
    """Worries undecided and No-leaning citizens raise right now, for choosing a post's topic.

    Counts distinct citizens per concern over the last MAX_AGE_SECONDS; a radar
    priority counts as two more citizens (three when rising).
    """
    items, weights = pool(db_path, data, now)
    authors = {}
    for item in items:
        if now - item['created'] <= MAX_AGE_SECONDS:
            for concern in item['concerns']:
                authors.setdefault(concern, set()).add(item['author_id'])
    rows = [{'concern': concern, 'citizens': len(people), **({'radar': True} if weights.get(concern) else {})}
            for concern, people in authors.items()]
    rows.sort(key=lambda row: (-(row['citizens'] + (weights.get(row['concern'], 0) + 1 if row.get('radar') else 0)),
                               row['concern']))
    return rows[:limit]


def mark(candidates, entries):
    """Copy each queue entry's ranking onto the candidate with the same source id."""
    by_id = {entry['source']['id']: entry['opportunity'] for entry in entries}
    for source in candidates:
        if source.get('id') in by_id:
            source['opportunity'] = by_id[source['id']]
    return candidates


def _newest(source):
    return max((value for value in (agent_targeting._stamp(source.get(key)) for key in
                ('freshness_at', 'edited_at', 'created_at', 'first_seen')) if value), default=0)


def select(sources):
    """Source ids chosen by rule: notifications, the queue, then news. No model is called.

    Citizen replies to our own originals lead (they raise the post's reply
    count while the citizen is still there); then other notifications by
    target score; then queue picks by rank; then the newest news.
    """
    rows = list(sources.values())
    notes = lambda source: (source.get('target') or {}).get('notes', [])
    notices = [s for s in rows if s.get('notification_ids')]
    notices.sort(key=lambda s: ('reply_to_our_original' not in notes(s),
                                'suspected_bot' in notes(s) or 'managed_account' in notes(s),
                                -agent_targeting.score(s), -_newest(s), s['id']))
    queued = sorted((s for s in rows if isinstance(s.get('opportunity'), dict) and not s.get('notification_ids')),
                    key=lambda s: (s['opportunity'].get('rank', 1e9), s['id']))
    news = sorted((s for s in rows if (s.get('kind') == 'news' or s.get('news_account'))
                   and not s.get('notification_ids') and not s.get('opportunity')),
                  key=lambda s: (-_newest(s), s['id']))
    chosen = notices[:NOTIFICATIONS_SELECTED] + queued[:QUEUE_SELECTED] + news[:NEWS_SELECTED]
    return list(dict.fromkeys(s['id'] for s in chosen))
