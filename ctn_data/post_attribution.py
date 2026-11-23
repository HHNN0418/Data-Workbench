"""Effect attribution for our posts and replies (P1-2).

capture() records each published post's features before completed drafts age
out; settle() adds the citizen reaction after SETTLE_HOURS; arm_stats() and
enrich() turn settled results into Thompson-sampled priorities for the radar.

Persuasion itself is only reported per team and hour, so the reward is a proxy
built from per-post citizen reactions with the official base weights.
"""
import json
import logging
import random
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import agent_targeting
from .db import begin_write, connect, utc_now

_LOG = logging.getLogger(__name__)
SETTLE_HOURS = 6
WINDOW_HOURS = 48
INTERVAL_SECONDS = 600
MIN_ARM_POSTS = 10
SUCCESS_REWARD = 15
ACTIONS = ('post', 'reply')
LOCAL = ZoneInfo('Australia/Sydney')
# Official base points per citizen action; a reply leaning Yes stands in for a view moving our way.
WEIGHTS = {'npc_replies': 15, 'reposts': 10, 'likes': 5, 'reply_yes': 20}
PUBLIC = "(p.visibility IS NULL OR LOWER(p.visibility) IN ('','public')) AND (a.private IS NULL OR a.private=0)"


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except ValueError:
        return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _own_accounts(conn):
    return {row['account_id'] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}


def _not_citizens(db_path, authors):
    """The given authors who are not calibrated citizens: agent_targeting.citizen_accounts decides.

    The deep analysis (citizens.md) found 66 recorded "citizen replies" were 15 under the
    calibrated proxy: bots and heavy-volume competitor accounts must not train the radar.
    """
    authors = {value for value in authors if type(value) is int}
    return authors - agent_targeting.citizen_accounts(db_path, authors)


def capture(db_path, agent_state, *, now=None):
    """Record features of newly published posts and replies; returns the number added."""
    drafts = [d for d in (agent_state or {}).get('drafts', {}).values()
              if d.get('state') == 'published' and d.get('action') in ACTIONS and type(d.get('post_id')) is int]
    if not drafts:
        return 0
    conn = connect(db_path)
    try:
        known = {row['post_id'] for row in conn.execute(
            'SELECT post_id FROM post_attribution WHERE post_id IN (' + ','.join('?' for _ in drafts) + ')',
            tuple(d['post_id'] for d in drafts))}
        fresh = [d for d in drafts if d['post_id'] not in known]
        if not fresh:
            return 0
        targets = {d['target_post_id'] for d in fresh if type(d.get('target_post_id')) is int}
        bodies = {}
        if targets:
            bodies = {row['post_id']: row for row in conn.execute(
                'SELECT post_id,author_id,body FROM posts WHERE post_id IN (' + ','.join('?' for _ in targets) + ')',
                tuple(targets))}
    finally:
        conn.close()
    bots = _not_citizens(db_path, {d.get('target_author_id') or (bodies[d['target_post_id']]['author_id']
                                   if d.get('target_post_id') in bodies else None) for d in fresh})
    depths = agent_targeting.thread_depths(db_path, sorted(targets)) if targets else {}
    from . import stance_labels
    assess = stance_labels.classifier(db_path, sorted(targets))
    captured = _iso(now or datetime.now(timezone.utc))
    rows = []
    for draft in fresh:
        published = _parse(draft.get('published_at')) or _parse(draft.get('created_at'))
        if published is None:
            continue
        target = bodies.get(draft.get('target_post_id'))
        author = draft.get('target_author_id') or (target['author_id'] if target else None)
        rows.append((draft['post_id'], draft['agent_id'], draft.get('id'), _iso(published), draft['action'],
                     draft.get('concern') if draft.get('concern') not in (None, 'none') else None,
                     int(bool(draft.get('research_brief_id')) and draft.get('pipeline') == 'staged'),
                     json.dumps(list(draft.get('fact_ids') or [])), draft.get('target_post_id'), author,
                     assess(draft['target_post_id'], target['body'])['stance'] if target else None,
                     None if author is None else int(author in bots),
                     depths.get(draft.get('target_post_id')), len(draft.get('body') or ''),
                     published.astimezone(LOCAL).hour, draft.get('pipeline') or 'single', captured))
    conn = connect(db_path)
    try:
        with conn:
            begin_write(conn, 'post_attribution')
            for row in rows:
                conn.execute('''INSERT INTO post_attribution(post_id,agent_id,draft_id,published_at,action,concern,
                    used_radar,fact_ids_json,target_post_id,target_author_id,target_stance,target_bot,thread_depth,
                    body_chars,hour_local,pipeline,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT DO NOTHING''', row)
    finally:
        conn.close()
    return len(rows)


def settle(db_path, *, now=None):
    """Fill in the citizen reaction for rows older than SETTLE_HOURS; returns the number settled."""
    now = now or datetime.now(timezone.utc)
    cutoff = _iso(now - timedelta(hours=SETTLE_HOURS))
    conn = connect(db_path)
    try:
        due = conn.execute('SELECT post_id,published_at FROM post_attribution WHERE outcome_at IS NULL '
                           'AND published_at<=? ORDER BY published_at LIMIT 500', (cutoff,)).fetchall()
        if not due:
            return 0
        excluded = _own_accounts(conn)
        ids = [row['post_id'] for row in due]
        marks = ','.join('?' for _ in ids)
        replies = conn.execute('SELECT p.post_id,p.reply_to_post_id,p.author_id,p.body,p.created_at FROM posts p '
                               'LEFT JOIN accounts a ON a.account_id=p.author_id WHERE p.reply_to_post_id IN ('
                               + marks + ') AND ' + PUBLIC, tuple(ids)).fetchall()
        counters = {}
        for row in conn.execute('SELECT post_id,observed_at,like_count,repost_count FROM counter_snapshots '
                                'WHERE post_id IN (' + marks + ') ORDER BY observed_at', tuple(ids)):
            counters.setdefault(row['post_id'], []).append(row)
    finally:
        conn.close()
    excluded |= _not_citizens(db_path, {reply['author_id'] for reply in replies} - excluded)
    from . import stance_labels
    assess = stance_labels.classifier(db_path, [reply['post_id'] for reply in replies])
    updates = []
    for row in due:
        limit = _iso(_parse(row['published_at']) + timedelta(hours=SETTLE_HOURS))
        stance = {'yes': 0, 'undecided': 0, 'no': 0}
        authors = set()
        for reply in replies:
            if (reply['reply_to_post_id'] == row['post_id'] and reply['author_id'] not in excluded
                    and (reply['created_at'] or '') <= limit):
                authors.add(reply['author_id'])
                label = assess(reply['post_id'], reply['body'])['stance']
                if label in stance:
                    stance[label] += 1
        snapshots = counters.get(row['post_id'], [])
        within = [s for s in snapshots if (s['observed_at'] or '') <= limit] or snapshots[:1]
        latest = within[-1] if within else None
        likes = (latest['like_count'] or 0) if latest else 0
        reposts = (latest['repost_count'] or 0) if latest else 0
        npc = len(authors)
        reward = (WEIGHTS['npc_replies'] * npc + WEIGHTS['reposts'] * reposts + WEIGHTS['likes'] * likes
                  + WEIGHTS['reply_yes'] * stance['yes'])
        updates.append((_iso(now), npc, stance['yes'], stance['undecided'], stance['no'], likes, reposts, reward,
                        row['post_id']))
    conn = connect(db_path)
    try:
        with conn:
            begin_write(conn, 'post_attribution')
            for update in updates:
                conn.execute('''UPDATE post_attribution SET outcome_at=?,npc_replies=?,reply_yes=?,reply_undecided=?,
                    reply_no=?,likes=?,reposts=?,reward=? WHERE post_id=? AND outcome_at IS NULL''', update)
    finally:
        conn.close()
    return len(updates)


def arm_stats(db_path, *, now=None, hours=WINDOW_HOURS):
    """Settled results per (concern, action) arm over the last `hours`."""
    now = now or datetime.now(timezone.utc)
    conn = connect(db_path)
    try:
        rows = conn.execute('''SELECT concern,action,COUNT(*) AS posts,
            SUM(CASE WHEN reward>=? THEN 1 ELSE 0 END) AS successes, AVG(reward) AS mean_reward,
            SUM(npc_replies) AS npc_replies, SUM(reply_yes) AS reply_yes
            FROM post_attribution WHERE outcome_at IS NOT NULL AND concern IS NOT NULL AND published_at>=?
            GROUP BY concern,action''', (SUCCESS_REWARD, _iso(now - timedelta(hours=hours)))).fetchall()
    finally:
        conn.close()
    # PostgreSQL returns SUM(bigint) as numeric (Decimal); cast so the stats stay JSON-serialisable.
    return {(row['concern'], row['action']): {
        'posts': int(row['posts']), 'successes': int(row['successes'] or 0),
        'success_rate': round(int(row['successes'] or 0) / int(row['posts']), 3) if row['posts'] else 0,
        'mean_reward': round(float(row['mean_reward'] or 0), 1), 'npc_replies': int(row['npc_replies'] or 0),
        'reply_yes': int(row['reply_yes'] or 0)} for row in rows}


def enrich(db_path, context, *, rng=None, now=None):
    """Add measured results to the radar's priorities and, with enough data, reorder them by a Thompson draw."""
    if not context or context.get('kind') != 'concern_radar' or not context.get('priorities'):
        return context
    try:
        stats = arm_stats(db_path, now=now)
    except Exception as exc:
        _LOG.warning('Attribution stats unavailable: %s', type(exc).__name__)
        return context
    rng = rng or random.Random()
    draws = {}
    for item in context['priorities']:
        measured = {action: stats[(item['concern'], action)] for action in ACTIONS if (item['concern'], action) in stats}
        if measured:
            item['measured'] = {action: {key: value[key] for key in ('posts', 'success_rate', 'mean_reward')}
                                for action, value in measured.items()}
        enough = [value for value in measured.values() if value['posts'] >= MIN_ARM_POSTS]
        if enough:
            draws[item['concern']] = max(rng.betavariate(1 + v['successes'], 1 + v['posts'] - v['successes'])
                                         for v in enough)
    # Concerns without enough data keep their radar position after the measured ones.
    if draws:
        context['priorities'].sort(key=lambda item: -draws.get(item['concern'], -1))
        context['priorities_order'] = 'thompson'
    return context


def start(controller):
    """Background capture and settle loop owned by the Agent controller's stop event."""
    def loop():
        delay = 60
        while not controller._stop.wait(delay):
            delay = INTERVAL_SECONDS
            try:
                capture(controller.db_path, controller.store.read())
                settle(controller.db_path)
            except Exception as exc:
                _LOG.warning('Post attribution iteration failed: %s', type(exc).__name__)
    thread = threading.Thread(target=loop, name='ctn-post-attribution', daemon=True)
    thread.start()
    return thread
