"""Blue-team report candidates: ranks posts, never reports them.

Everything here reads collected public posts inside one read-only transaction
(PostgreSQL ``BEGIN READ ONLY``; SQLite ``PRAGMA query_only``). It never
submits a report, never calls a competition API or a language model, and never
writes to the database. Wiring into the server and UI is a separate step.

Competition rules used (official, summarised):

* A post the moderator rules inauthentic pays 25 Blue points to the first
  reporting team and 5 to the next four; reporting our own posts pays nothing.
* The moderator is wrong about 1 in 10 either way. A "genuine" ruling suspends
  all reporting by our team for 6 hours. Verified accounts are always ruled
  correctly, so they are never candidates.
* Paid reports against the same competitor team per Sydney day: 5 at full
  rate, 5 at half rate, then 10%. Teams are not observable, so accounts that
  share identical texts or post them at the same time form a coordination
  cluster (connected components of a shared-behaviour graph, after Pacheco et
  al., ICWSM 2021) and the cap is applied per cluster.
* Frivolous reports are a platform violation, so precision matters far more
  than volume.

The precision estimate ``p`` is a log-odds sum of independent evidence
families. Its weights are engineering priors, not calibrated against moderator
rulings (none have been observed yet), and a single family can never reach a
reportable ``p`` on its own.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from . import agent_targeting
from .account_screening import _coordinated, _signature
from .db import connect, timestamp_key

# Our managed accounts, in addition to team_accounts and the Agent store.
OWN_ACCOUNT_IDS = frozenset({5554, 5832, 6464, 6494, 6503})

CANDIDATE_WINDOW_HOURS = 24
BEHAVIOR_WINDOW_HOURS = agent_targeting.BEHAVIOR_WINDOW_HOURS
SHARED_MIN_ACCOUNTS = agent_targeting.DUPLICATE_MIN_ACCOUNTS
COTIME_SECONDS = 120  # fixed inside account_screening._coordinated
SELF_REPEAT_MIN = 3
SELF_REPEAT_STRONG = 5
LATENCY_MIN_ANSWERS = 5
LATENCY_FAST_SECONDS = 60
LATENCY_QUICK_SECONDS = 180
DEEP_RATIO_MIN = 0.3
UNAVAILABLE_HTTP = (403, 404, 410)
EXCERPT_CHARS = 120
# Citizens forward chain messages ("copy-paste this"); sharing one is not
# evidence of competitor control and does not link accounts into a cluster.
CHAIN_TEXT = re.compile(r'copy[\s-]*(?:and[\s-]*|&\s*)?paste|copy this|share this|pass (?:it|this) on|repost this', re.I)

PRIOR_LOG_ODDS = -2.5  # p = 0.076 before any evidence
# Log-odds per evidence rule. Within a family only the strongest rule counts,
# so correlated measurements of the same behaviour are not added twice.
LOG_ODDS = {
    'behaviour_bot_rule': ('behaviour', 2.0),
    'deep_threads': ('behaviour', 1.2),
    'fast_answers': ('latency', 1.2),
    'quick_answers': ('latency', 0.6),
    # Shared text and style groups measure the same generator's writing, so they are one family (D100).
    'shared_text_post': ('writing', 1.8),
    'shared_text_author': ('writing', 1.0),
    'chain_text': ('writing', 0.2),
    # style_groups.py: the account sits in a 3+ account style group; with _hidden, 2+ other members
    # have a confirmed hidden post (risk_assessment). Engineering priors (style-detection research §6.2).
    'style_group': ('writing', 1.0),
    'style_group_hidden': ('writing', 1.6),
    'cotimed_post': ('co_timing', 1.0),
    'cotimed_author': ('co_timing', 0.6),
    'self_repeat_strong': ('self_repeat', 1.3),
    'self_repeat': ('self_repeat', 0.8),
    'deep_post': ('thread_depth', 0.3),
    # Replied to us in an hour the official log scored no NPC replies, so not
    # an NPC (npc_calibration); every non-NPC outside our team is
    # competitor-controlled. p = 0.90 alone, the moderator's own error rate.
    'official_non_npc': ('official_calibration', 4.7),
}
# style_group_hidden needs this many other members of the group with a confirmed hidden post.
STYLE_HIDDEN_MIN = 2
# What candidates() reads of style_groups.account_evidence: all a child process needs.
STYLE_FIELDS = ('style_group', 'group_size', 'group_hidden_accounts', 'possible_teammate', 'linked_to_ours')
# candidates() costs ~17 s CPU on live data (24 Oct); the backend runs it in a child process.
CHILD_TIMEOUT_SECONDS = 300
_ROOT = Path(__file__).resolve().parent.parent
# Posts by probable unregistered teammates (style_groups possible_teammate) returned for display
# only, beyond ``limit``; Blue never plans or reports them.
TEAMMATE_POSTS = 40
# Ground truth rather than an engineering prior: exempt from SINGLE_FAMILY_CAP.
UNCAPPED_FAMILIES = frozenset({'official_calibration'})
# Families at or above this weight count as independent support.
SUPPORT_LOG_ODDS = 0.6
SINGLE_FAMILY_CAP = 0.6

REWARD_FIRST = 25
REWARD_LATER = 5
MODERATOR_ERROR = 0.1
DEFAULT_PENALTY = 40.0  # approximate value of 6 lost reporting hours
DEFAULT_THRESHOLD = 0.85
FULL_RATE_PER_CLUSTER = 5
REPORTS_PER_MINUTE = 3
SYDNEY = 'Australia/Sydney'
# Referendum side of a candidate's cluster from agent_targeting.classify: a side
# needs this many sided posts and at least twice the other side's count.
SIDE_MIN_POSTS = 2
SIDE_RATIO = 2
# The plan takes No-side candidates first: reporting Yes-side bots weakens our side (D49).
SIDE_PLAN_ORDER = {'no': 0, 'mixed': 1, 'unknown': 1, 'yes': 2}


# --------------------------------------------------------------------------
# Read-only access
# --------------------------------------------------------------------------

@contextmanager
def read_only(source):
    """Yield a connection that cannot write, and always roll back.

    ``source`` is a database path/URL (opened and closed here) or an open
    connection (borrowed; must not be inside a transaction and keeps its
    previous SQLite ``query_only`` setting afterwards).
    """
    owned = isinstance(source, (str, Path))
    if not owned and source.in_transaction:
        # Never roll back a caller's open transaction.
        raise ValueError('read-only access needs a connection outside a transaction')
    conn = connect(source) if owned else source
    postgres = getattr(conn, 'dialect', None) == 'postgresql'
    restore = None
    try:
        if postgres:
            conn.execute('BEGIN READ ONLY')
        else:
            restore = conn.execute('PRAGMA query_only').fetchone()[0]
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            if owned:
                conn.close()
            elif restore is not None:
                conn.execute('PRAGMA query_only=' + str(int(restore)))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _now(value):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc)
    key = timestamp_key(value)
    if key is None:
        raise ValueError('now must be a datetime, epoch seconds or ISO timestamp')
    return datetime.fromisoformat(key.replace('Z', '+00:00'))


def _key(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _epoch(key):
    return datetime.fromisoformat(key.replace('Z', '+00:00')).timestamp() if key else None


def _chunks(values, size=500):
    values = sorted(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _has_table(conn, name):
    if getattr(conn, 'dialect', None) == 'postgresql':
        return bool(conn.execute('SELECT to_regclass(?) IS NOT NULL', (name,)).fetchone()[0])
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def sydney_day(now=None):
    """Competition day (Australia/Sydney) for ``now``."""
    return _now(now).astimezone(ZoneInfo(SYDNEY)).date().isoformat()


def expected_value(p, penalty=DEFAULT_PENALTY, *, rate=1.0, reward=REWARD_FIRST):
    """EV of one report: reward*P(ruled inauthentic) - penalty*P(ruled genuine)."""
    upheld = (1 - MODERATOR_ERROR) * p + MODERATOR_ERROR * (1 - p)
    return reward * rate * upheld - penalty * (1 - upheld)


def precision(rules):
    """Precision estimate from fired rule names (see LOG_ODDS)."""
    families = {}
    for rule in rules:
        family, weight = LOG_ODDS[rule]
        families[family] = max(families.get(family, 0.0), weight)
    p = 1 / (1 + math.exp(-(PRIOR_LOG_ODDS + sum(families.values()))))
    support = sum(weight >= SUPPORT_LOG_ODDS for weight in families.values())
    capped = support < 2 and not UNCAPPED_FAMILIES & set(families)
    return (min(p, SINGLE_FAMILY_CAP) if capped else p), support


# --------------------------------------------------------------------------
# Data loading (all inside one read-only transaction)
# --------------------------------------------------------------------------

def _own_accounts(conn):
    own = set(OWN_ACCOUNT_IDS)
    own.update(row[0] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1'))
    from .agent_control import PREFIX
    row = conn.execute('SELECT value FROM meta WHERE key=?', (PREFIX + 'agents',)).fetchone()
    if row:
        try:
            agents = json.loads(row[0])
        except (TypeError, ValueError):
            agents = {}
        if isinstance(agents, dict):
            own.update(a['account_id'] for a in agents.values()
                       if isinstance(a, dict) and type(a.get('account_id')) is int)
    return own


def _unavailable(conn):
    """Posts whose latest counter request found them hidden or deleted."""
    if not _has_table(conn, 'counter_sampling'):
        return set()
    return {row[0] for row in conn.execute(
        'SELECT post_id FROM counter_sampling WHERE attempt_http IN (' + ','.join('?' for _ in UNAVAILABLE_HTTP) +
        ') AND (sampled_at IS NULL OR attempted_at>=sampled_at)', UNAVAILABLE_HTTP)}


def _behaviour(conn, since, until):
    """Per-author reply behaviour, using agent_targeting's bot rule and constants.

    Mirrors ``agent_targeting.behavior_bots`` on this connection and window,
    and also measures reply latency and the deep-thread ratio.
    """
    rows = [(row[0], row[1], row[2], _epoch(row[3])) for row in conn.execute(
        'SELECT post_id,author_id,reply_to_post_id,ctn_timestamp(created_at) FROM posts '
        'WHERE ctn_timestamp(created_at)>=? AND ctn_timestamp(created_at)<=? '
        'AND author_id IS NOT NULL AND repost_of_post_id IS NULL', (since, until))]
    authors = {post_id: author for post_id, author, _, _ in rows}
    parents = {post_id: parent for post_id, _, parent, _ in rows}
    times = {post_id: at for post_id, _, _, at in rows}
    depths = agent_targeting._depths(conn, dict(parents))
    for chunk in _chunks({p for p in parents.values() if p and p not in authors}):
        authors.update((row[0], row[1]) for row in conn.execute(
            'SELECT post_id,author_id FROM posts WHERE post_id IN (' + ','.join('?' for _ in chunk) + ')', tuple(chunk)))
    first_answer = {}
    for post_id, author, parent, at in rows:
        if parent:
            key, at = (parent, author), math.inf if at is None else at
            if key not in first_answer or at < first_answer[key][0]:
                first_answer[key] = (at, post_id)
    stats = defaultdict(lambda: {'posts': 0, 'received': 0, 'answers': 0, 'deep': 0, 'latencies': []})
    answer_latency = {}
    for post_id, author, parent, at in rows:
        item = stats[author]
        item['posts'] += 1
        if depths.get(post_id, 0) >= agent_targeting.BEHAVIOR_DEEP_DEPTH:
            item['deep'] += 1
        target = authors.get(parent) if parent else None
        if target is None or target == author:
            continue
        stats[target]['received'] += 1
        answer = first_answer.get((post_id, target))
        if answer:
            stats[target]['answers'] += 1
            if at is not None and answer[0] != math.inf:
                latency = max(0.0, answer[0] - at)
                stats[target]['latencies'].append(latency)
                answer_latency[answer[1]] = latency
    for author, item in stats.items():
        received = item['received']
        item['answer_rate'] = item['answers'] / received if received else None
        item['median_latency'] = median(item['latencies']) if item['latencies'] else None
        item['deep_ratio'] = item['deep'] / item['posts'] if item['posts'] else 0.0
        item['bot_rule'] = received >= agent_targeting.BEHAVIOR_MIN_RECEIVED and (
            item['answers'] / received >= agent_targeting.BEHAVIOR_ANSWER_RATE
            or item['deep'] >= agent_targeting.BEHAVIOR_DEEP_POSTS)
    return dict(stats), depths, answer_latency


def _recent_posts(conn, since, until):
    return [{'post_id': row[0], 'author_id': row[1], 'kind': row[2], 'reply_to_post_id': row[3],
             'body': row[4] or '', 'visibility': row[5], 'created_at': row[6], 'at': _epoch(row[6])}
            for row in conn.execute(
                'SELECT post_id,author_id,kind,reply_to_post_id,body,visibility,ctn_timestamp(created_at) FROM posts '
                'WHERE ctn_timestamp(created_at)>=? AND ctn_timestamp(created_at)<=? '
                'AND author_id IS NOT NULL AND repost_of_post_id IS NULL', (since, until))]


def _accounts(conn, author_ids):
    result = {}
    for chunk in _chunks(author_ids):
        for row in conn.execute('SELECT account_id,username,verified,private FROM accounts WHERE account_id IN ('
                                + ','.join('?' for _ in chunk) + ')', tuple(chunk)):
            result[row[0]] = {'username': row[1], 'verified': row[2], 'private': row[3]}
    return result


def _observed_actions(conn, author_ids):
    """All stored posts, replies and reposts per author: a lower bound on platform actions."""
    result = {}
    for chunk in _chunks(author_ids):
        result.update((row[0], row[1]) for row in conn.execute(
            'SELECT author_id,COUNT(*) FROM posts WHERE author_id IN (' + ','.join('?' for _ in chunk) + ') '
            'GROUP BY author_id', tuple(chunk)))
    return result


# --------------------------------------------------------------------------
# Shared texts and coordination clusters
# --------------------------------------------------------------------------

class _Components:
    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, items):
        items = list(items)
        for other in items[1:]:
            a, b = self.find(items[0]), self.find(other)
            if a != b:
                self.parent[max(a, b)] = min(a, b)


def _texts(posts):
    """Shared, co-timed and self-repeated texts in the candidate window.

    Coordination graph: accounts are linked when they posted the same
    non-chain text (>= SHARED_MIN_ACCOUNTS accounts in the window) or posted
    one within COTIME_SECONDS of each other. Connected components are the
    clusters used for the per-team daily cap.
    """
    events = defaultdict(list)
    signature_of, chain = {}, set()
    for post in posts:
        signature = _signature(post['body'], len(post['body']))
        if not signature:
            continue
        signature_of[post['post_id']] = signature
        if CHAIN_TEXT.search(post['body']):
            chain.add(signature)
        if post['at'] is not None:
            events[signature].append((post['at'], post['author_id'], post['post_id']))
    authors_of = defaultdict(set)
    repeats = defaultdict(int)
    for post in posts:
        signature = signature_of.get(post['post_id'])
        if signature:
            authors_of[signature].add(post['author_id'])
            repeats[(post['author_id'], signature)] += 1
    shared = {s for s, authors in authors_of.items() if len(authors) >= SHARED_MIN_ACCOUNTS}
    coordination = _coordinated({s: list(events[s]) for s in shared if s not in chain})
    components = _Components()
    for signature in shared - chain:
        components.union(authors_of[signature])
    shared_authors = defaultdict(set)
    for signature in shared:
        for author in authors_of[signature]:
            shared_authors[author].add(signature)
    self_repeat = defaultdict(int)
    for (author, _), count in repeats.items():
        self_repeat[author] = max(self_repeat[author], count)
    return {'signature_of': signature_of, 'chain': chain, 'shared': shared, 'authors_of': authors_of,
            'shared_authors': shared_authors, 'coordination': coordination, 'components': components,
            'self_repeat': self_repeat}


def _clusters(texts, authors):
    """(cluster_id, accounts in cluster) for each author; singletons are 'a<id>'."""
    components = texts['components']
    sizes = defaultdict(int)
    for node in list(components.parent):
        sizes[components.find(node)] += 1
    result = {}
    for author in authors:
        root = components.find(author)
        size = sizes.get(root, 1)
        result[author] = (f'c{root}' if size > 1 else f'a{author}', size)
    return result


def _side(counts):
    """'yes', 'no', 'mixed' or 'unknown' from {'yes': n, 'no': n} sided-post counts."""
    yes, no = counts['yes'], counts['no']
    if yes + no < SIDE_MIN_POSTS:
        return 'unknown'
    if no >= SIDE_RATIO * yes:
        return 'no'
    if yes >= SIDE_RATIO * no:
        return 'yes'
    return 'mixed'


def _sides(posts, texts, authors, assess=None):
    """(side, counts) of each author's cluster from the window's posts of all its accounts.

    ``assess(post_id, text)`` is the team's stance source (stance_labels.classifier); keywords without it.
    """
    parent = texts['components'].parent
    find = texts['components'].find
    root_of = lambda author: find(author) if author in parent else author
    roots = {root_of(author) for author in authors}
    counts = defaultdict(lambda: {'yes': 0, 'no': 0})
    for post in posts:
        root = root_of(post['author_id'])
        if root in roots:
            stance = (assess(post['post_id'], post['body']) if assess else agent_targeting.classify(post['body']))['stance']
            if stance in ('yes', 'no'):
                counts[root][stance] += 1
    return {author: (_side(counts[root_of(author)]), dict(counts[root_of(author)])) for author in authors}


# --------------------------------------------------------------------------
# Evidence and ranking
# --------------------------------------------------------------------------

def _evidence(post, behaviour, depth, answer_latency, texts, calibrated=frozenset(), style=None):
    author = post['author_id']
    stats = behaviour.get(author)
    items = []

    def add(rule, detail, **values):
        items.append({'rule': rule, 'family': LOG_ODDS[rule][0], 'log_odds': LOG_ODDS[rule][1],
                      'detail': detail, **values})

    if author in calibrated:
        add('official_non_npc', '官方小时校准：在官方记为 0 条 NPC 回复的小时里回复过我们，确定不是 NPC')
    group = (style or {}).get(author)
    if group and group.get('id'):
        detail = f"style group {group['id']}: {group['size']} accounts write alike, {group['others_hidden']} others with hidden posts"
        if not group['counts']:
            # The citizen proxy calls this author a citizen: shown, never scored (style research §6.2).
            items.append({'rule': 'style_group_info', 'family': 'info', 'log_odds': 0.0,
                          'detail': detail + ' (citizen proxy: not scored)', 'group_size': group['size']})
        elif group['others_hidden'] >= STYLE_HIDDEN_MIN:
            add('style_group_hidden', detail, group_size=group['size'], others_hidden=group['others_hidden'])
        else:
            add('style_group', detail, group_size=group['size'], others_hidden=group['others_hidden'])

    if stats:
        if stats['bot_rule']:
            add('behaviour_bot_rule', f"agent_targeting bot rule: answered {stats['answers']}/{stats['received']} "
                f"replies, {stats['deep']} posts at depth>={agent_targeting.BEHAVIOR_DEEP_DEPTH} "
                f"in {BEHAVIOR_WINDOW_HOURS} h", answer_rate=round(stats['answer_rate'], 3),
                received=stats['received'], deep_posts=stats['deep'])
        if stats['deep'] >= agent_targeting.BEHAVIOR_DEEP_POSTS and stats['deep_ratio'] >= DEEP_RATIO_MIN:
            add('deep_threads', f"deep-thread ratio {stats['deep_ratio']:.0%} ({stats['deep']}/{stats['posts']} "
                f"posts at depth>={agent_targeting.BEHAVIOR_DEEP_DEPTH})", deep_ratio=round(stats['deep_ratio'], 3))
        latency = stats['median_latency']
        if latency is not None and len(stats['latencies']) >= LATENCY_MIN_ANSWERS:
            if latency <= LATENCY_FAST_SECONDS:
                add('fast_answers', f"median reply latency {latency:.0f} s over {len(stats['latencies'])} answers",
                    median_latency_seconds=round(latency, 1))
            elif latency <= LATENCY_QUICK_SECONDS:
                add('quick_answers', f"median reply latency {latency:.0f} s over {len(stats['latencies'])} answers",
                    median_latency_seconds=round(latency, 1))
    signature = texts['signature_of'].get(post['post_id'])
    if signature in texts['shared']:
        accounts = len(texts['authors_of'][signature])
        if signature in texts['chain']:
            add('chain_text', f'chain-message text posted by {accounts} accounts in {CANDIDATE_WINDOW_HOURS} h',
                accounts=accounts)
        else:
            add('shared_text_post', f'identical text posted by {accounts} accounts in {CANDIDATE_WINDOW_HOURS} h',
                accounts=accounts)
    other = {s for s in texts['shared_authors'].get(author, ()) if s != signature and s not in texts['chain']}
    if other:
        add('shared_text_author', f'author posted {len(other)} other text(s) shared by >={SHARED_MIN_ACCOUNTS} accounts',
            texts=len(other))
    coordinated = texts['coordination'].get(author)
    if coordinated and coordinated['post_ids']:
        if post['post_id'] in coordinated['post_ids']:
            add('cotimed_post', f'same text as >={SHARED_MIN_ACCOUNTS - 1} other accounts within {COTIME_SECONDS} s')
        else:
            add('cotimed_author', f"author posted {len(coordinated['post_ids'])} text(s) in sync with "
                f">={SHARED_MIN_ACCOUNTS - 1} other accounts within {COTIME_SECONDS} s")
    repeat = texts['self_repeat'].get(author, 0)
    if repeat >= SELF_REPEAT_STRONG:
        add('self_repeat_strong', f'author posted one identical text {repeat} times in {CANDIDATE_WINDOW_HOURS} h',
            repeats=repeat)
    elif repeat >= SELF_REPEAT_MIN:
        add('self_repeat', f'author posted one identical text {repeat} times in {CANDIDATE_WINDOW_HOURS} h',
            repeats=repeat)
    if depth >= agent_targeting.BEHAVIOR_DEEP_DEPTH:
        add('deep_post', f'post sits at thread depth {depth}', depth=depth)
    if post['post_id'] in answer_latency:
        # Informational: this post answered a reply; latency is already scored per author.
        items.append({'rule': 'answer_latency', 'family': 'info', 'log_odds': 0.0,
                      'detail': f"answered a reply after {answer_latency[post['post_id']]:.0f} s",
                      'latency_seconds': round(answer_latency[post['post_id']], 1)})
    return items


def _style(conn, db_path, authors, evidence=None):
    """{author: {'id', 'size', 'others_hidden', 'counts', 'possible_teammate', 'linked_to_ours'}} from style_groups.

    style_groups keeps its result in memory (no database); empty before its first
    round. A child process has no such memory, so its parent passes ``evidence``
    ({account_id: STYLE_FIELDS}) instead. ``id`` is None for an author in no
    group; ``others_hidden`` counts the group's other members with a confirmed
    hidden post (risk_assessment); ``counts`` is False when the citizen proxy
    calls the author a citizen or the group holds one of ours, so style alone
    never puts such an author in a plan.
    """
    from . import style_groups
    from .risk_assessment import account_risk
    if evidence is None:
        found = style_groups.account_evidence(sorted(authors))
    else:
        found = {author: evidence[author] for author in authors if author in evidence}
    if not found:
        return {}
    grouped = {author for author, entry in found.items() if entry.get('style_group')}
    hidden = account_risk(conn, grouped) if grouped else {}
    try:
        citizens = agent_targeting.citizen_accounts(db_path if isinstance(db_path, (str, Path)) else conn, grouped)
    except Exception as exc:
        # Unknown means citizen here: style is then shown but never scored.
        logging.getLogger(__name__).warning('Blue style evidence: citizen proxy unavailable (%s)', type(exc).__name__)
        citizens = frozenset(grouped)
    result = {}
    for author, entry in found.items():
        mine = 1 if (hidden.get(author) or {}).get('hidden') else 0
        result[author] = {'id': entry.get('style_group'), 'size': int(entry.get('group_size') or 0),
                          'others_hidden': max(0, int(entry.get('group_hidden_accounts') or 0) - mine),
                          'counts': author not in citizens and not entry.get('possible_teammate'),
                          'possible_teammate': bool(entry.get('possible_teammate')),
                          'linked_to_ours': int(entry.get('linked_to_ours') or 0)}
    return result


class _Stages:
    """Wall and thread-CPU seconds per stage into ``into`` (a dict), when one is given."""

    def __init__(self, into):
        self.into, self._wall, self._cpu = into, time.monotonic(), time.thread_time()

    def __call__(self, name):
        if self.into is None:
            return
        wall, cpu = time.monotonic(), time.thread_time()
        self.into[name] = {'wall': round(wall - self._wall, 3), 'cpu': round(cpu - self._cpu, 3)}
        self._wall, self._cpu = wall, cpu


def candidates(db_path, now=None, limit=50, *, penalty=DEFAULT_PENALTY, per_author=2, timing=None,
               style_evidence=None):
    """Ranked post-level report candidates; reads only, never reports.

    ``db_path`` is a database path/URL or an open connection outside a
    transaction. Excludes our own accounts, verified accounts, reposts, posts
    found hidden/deleted, posts outside the last 24 hours and authors without
    evidence. Each item carries evidence, a coordination cluster, its style
    group (``style_group``), the uncalibrated precision estimate ``p`` and the
    expected value ``ev`` of a first report at full rate. At most
    ``per_author`` posts per author. Posts of probable unregistered teammates
    (``possible_teammate``) come after the ``limit`` others, at most
    TEAMMATE_POSTS, for display only. ``timing``, a dict, receives each stage's
    wall and CPU seconds; ``style_evidence`` replaces style_groups' memory (_style).
    """
    if type(limit) is not int or limit < 1:
        raise ValueError('limit must be a positive integer')
    stage = _Stages(timing)
    now_dt = _now(now)
    until = _key(now_dt)
    since = _key(now_dt - timedelta(hours=CANDIDATE_WINDOW_HOURS))
    behaviour_since = _key(now_dt - timedelta(hours=max(BEHAVIOR_WINDOW_HOURS, CANDIDATE_WINDOW_HOURS)))
    with read_only(db_path) as conn:
        own = _own_accounts(conn)
        unavailable = _unavailable(conn)
        stage('read_own_unavailable')
        behaviour, depths, answer_latency = _behaviour(conn, behaviour_since, until)
        stage('behaviour')
        posts = _recent_posts(conn, since, until)
        accounts = _accounts(conn, {p['author_id'] for p in posts})
        stage('read_posts_accounts')
        # Our own and verified accounts are neither candidates nor graph nodes.
        excluded = own | {author for author, account in accounts.items() if account.get('verified')}
        texts = _texts([p for p in posts if p['author_id'] not in excluded])
        stage('texts')
        from . import npc_calibration
        calibrated = npc_calibration.certain_non_npcs(db_path if isinstance(db_path, (str, Path)) else conn) - excluded
        stage('calibration')
        style = _style(conn, db_path, {p['author_id'] for p in posts} - excluded, style_evidence)
        stage('style_citizens')
        ranked = []
        for post in posts:
            author = post['author_id']
            account = accounts.get(author, {})
            if (author in excluded or post['post_id'] in unavailable
                    or str(post['visibility'] or 'public').lower() not in ('', 'public') or not post['body'].strip()):
                continue
            evidence = _evidence(post, behaviour, depths.get(post['post_id'], 0), answer_latency, texts, calibrated,
                                 style)
            rules = [item['rule'] for item in evidence if item['rule'] in LOG_ODDS]
            if not rules:
                continue
            p, support = precision(rules)
            weight = sum(LOG_ODDS[rule][1] for rule in rules)
            ranked.append((post, account, evidence, p, support, weight))
        stage('evidence')
        # p saturates once several families fire; then more evidence, then newer posts.
        ranked.sort(key=lambda item: (-item[3], -item[4], -item[5], -(item[0]['at'] or 0), item[0]['post_id']))
        chosen, teammates, per = [], [], defaultdict(int)
        for item in ranked:
            author = item[0]['author_id']
            if per[author] >= per_author:
                continue
            flagged = (style.get(author) or {}).get('possible_teammate')
            if flagged and len(teammates) >= TEAMMATE_POSTS or not flagged and len(chosen) >= limit:
                continue
            per[author] += 1
            (teammates if flagged else chosen).append(item)
            if len(chosen) >= limit and len(teammates) >= TEAMMATE_POSTS:
                break
        chosen += teammates
        actions = _observed_actions(conn, {item[0]['author_id'] for item in chosen})
        stage('select_actions')
    clusters = _clusters(texts, {item[0]['author_id'] for item in chosen})
    sided = [p for p in posts if p['author_id'] not in excluded]
    from . import stance_labels
    assess = (stance_labels.classifier(db_path, [p['post_id'] for p in sided])
              if isinstance(db_path, (str, Path)) else None)
    sides = _sides(sided, texts, {item[0]['author_id'] for item in chosen}, assess)
    stage('clusters_sides')
    if timing is not None:
        timing['posts'], timing['authors_with_style'] = len(posts), len(style)
    result = []
    for post, account, evidence, p, support, _ in chosen:
        author = post['author_id']
        cluster_id, cluster_size = clusters[author]
        side, side_counts = sides[author]
        verified = account.get('verified') if account else None
        body = ' '.join(post['body'].split())
        upheld = (1 - MODERATOR_ERROR) * p + MODERATOR_ERROR * (1 - p)
        group = style.get(author) or {}
        result.append({
            'post_id': post['post_id'], 'author_id': author, 'username': account.get('username'),
            'created_at': post['created_at'], 'kind': post['kind'], 'reply_to_post_id': post['reply_to_post_id'],
            'excerpt': body[:EXCERPT_CHARS - 1] + '…' if len(body) > EXCERPT_CHARS else body,
            'cluster_id': cluster_id, 'cluster_size': cluster_size,
            'side': side, 'side_counts': side_counts,
            'evidence': evidence, 'support': support,
            'p': round(p, 4), 'p_genuine_ruling': round(1 - upheld, 4),
            'ev': round(expected_value(p, penalty), 2),
            'verified': None if verified is None else bool(verified),
            'observed_actions': actions.get(author, 0),
            'style_group': group.get('id'), 'possible_teammate': bool(group.get('possible_teammate')),
            'linked_to_ours': group.get('linked_to_ours', 0),
        })
    stage('result')
    return result


def style_snapshot():
    """style_groups' evidence for every listed account, cut to STYLE_FIELDS: what a child candidates() needs."""
    from . import style_groups
    found = style_groups.account_evidence(style_groups.listed_accounts())
    return {str(account_id): {key: entry.get(key) for key in STYLE_FIELDS} for account_id, entry in found.items()}


def _json_default(value):
    from decimal import Decimal
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f'{type(value).__name__} is not JSON serialisable')


def candidates_isolated(db_path, *, limit=50, now=None, timeout=CHILD_TIMEOUT_SECONDS, started=None):
    """candidates() in a child Python process at below-normal priority; returns (items, timing).

    The backend keeps its GIL and CPU (~17 s a round on live data, 24 Oct). The
    child only reads the database: every PostgreSQL session it opens is read-only
    (PGOPTIONS), and it sends no platform request; Blue's availability GETs stay
    in the parent. The database URL, ``limit``, ``now`` (tests) and
    style_snapshot() go in as JSON on stdin. ``started`` receives the process.
    """
    wall = time.monotonic()
    request = json.dumps({'db': str(db_path), 'limit': limit, 'now': None if now is None else _key(_now(now)),
                          'style': style_snapshot()}, separators=(',', ':')).encode('utf-8')
    env = {**os.environ, 'PGOPTIONS': (os.environ.get('PGOPTIONS', '') + ' -c default_transaction_read_only=on').strip()}
    options = ({'creationflags': subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW}
               if os.name == 'nt' else {'preexec_fn': lambda: os.nice(10)})
    proc = subprocess.Popen([sys.executable, '-m', 'ctn_data.blue_reports'], cwd=str(_ROOT), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **options)
    if started is not None:
        started(proc)
    try:
        out, err = proc.communicate(request, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f'Blue 候选子进程超过 {timeout} 秒，已终止') from None
    if proc.returncode:
        detail = err.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(f'Blue 候选子进程失败（{proc.returncode}）：{detail[-1] if detail else ""}'[:300])
    payload = json.loads(out.decode('utf-8'))
    timing = payload['timing']
    timing.update(isolated=True, child_wall=round(time.monotonic() - wall, 3), request_bytes=len(request))
    return payload['items'], timing


def _child():
    """python -m ctn_data.blue_reports: {'db', 'limit', 'style'} JSON on stdin, {'items', 'timing'} on stdout."""
    request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    style = {int(account_id): entry for account_id, entry in request['style'].items()}
    timing, cpu = {}, time.process_time()
    items = candidates(request['db'], request.get('now'), limit=request['limit'], timing=timing, style_evidence=style)
    timing['child_cpu'] = round(time.process_time() - cpu, 3)
    sys.stdout.buffer.write(json.dumps({'items': items, 'timing': timing}, ensure_ascii=False, separators=(',', ':'),
                                       default=_json_default).encode('utf-8'))
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Daily plan
# --------------------------------------------------------------------------

def _reported(already):
    """Normalise prior reports today into (per-cluster counts, post ids, per-author counts)."""
    clusters, posts, authors = defaultdict(int), set(), defaultdict(int)
    if not already:
        return clusters, posts, authors
    if isinstance(already, Mapping):
        for cluster_id, count in already.items():
            clusters[str(cluster_id)] += int(count)
        return clusters, posts, authors
    for item in already:
        if isinstance(item, Mapping):
            if item.get('cluster_id') is not None:
                clusters[str(item['cluster_id'])] += 1
            # Style groups ('S' ids) share the same daily full-rate count, beside the text cluster.
            if item.get('style_group'):
                clusters[str(item['style_group'])] += 1
            if item.get('post_id') is not None:
                posts.add(item['post_id'])
            if item.get('author_id') is not None:
                authors[item['author_id']] += 1
        else:
            posts.add(item)
    return clusters, posts, authors


def daily_plan(candidates, already_reported_today=None, *, threshold=DEFAULT_THRESHOLD,
               full_rate_per_cluster=FULL_RATE_PER_CLUSTER, max_per_author=2, now=None):
    """Choose which candidates to report today; a plan only, nothing is reported.

    ``already_reported_today`` is either ``{cluster_id: count}`` or an iterable
    of report records (mappings with ``cluster_id``/``post_id``/``author_id``,
    or bare post ids) for the current Sydney day. Candidates at or above
    ``threshold`` are taken No side first (``SIDE_PLAN_ORDER``), then in
    descending ``p``; planning stops at the first one below ``threshold``. A
    cluster, and separately a style group, gets at most ``full_rate_per_cluster``
    reports per day (reduced-rate reports are never planned); verified or
    unknown-verification authors, probable teammates and non-positive EV are
    skipped, and items get rate-limit slots (3 per minute).
    """
    clusters, posts, authors = _reported(already_reported_today)
    ordered = sorted(candidates, key=lambda c: (c['p'] < threshold, SIDE_PLAN_ORDER.get(c.get('side'), 1), -c['p']))
    items, skipped, stop = [], [], 'exhausted'
    for candidate in ordered:
        if candidate['p'] < threshold:
            stop = 'below_threshold'
            break
        reason = None
        if candidate['post_id'] in posts:
            reason = 'already_reported'
        elif candidate.get('verified') is not False:
            reason = 'verified_or_unknown'
        elif candidate['ev'] <= 0:
            reason = 'non_positive_ev'
        elif candidate.get('possible_teammate'):
            reason = 'possible_teammate'
        elif clusters[candidate['cluster_id']] >= full_rate_per_cluster:
            reason = 'cluster_daily_cap'
        elif candidate.get('style_group') and clusters[candidate['style_group']] >= full_rate_per_cluster:
            reason = 'style_group_daily_cap'
        elif authors[candidate['author_id']] >= max_per_author:
            reason = 'author_daily_cap'
        if reason:
            skipped.append({'post_id': candidate['post_id'], 'cluster_id': candidate['cluster_id'], 'reason': reason})
            continue
        clusters[candidate['cluster_id']] += 1
        if candidate.get('style_group'):
            clusters[candidate['style_group']] += 1
        authors[candidate['author_id']] += 1
        posts.add(candidate['post_id'])
        items.append({**candidate, 'plan_rank': len(items) + 1,
                      'cluster_report_number': clusters[candidate['cluster_id']],
                      'slot_minute': len(items) // REPORTS_PER_MINUTE})
    return {'day': sydney_day(now), 'threshold': threshold, 'items': items, 'skipped': skipped,
            'stop_reason': stop, 'expected_value': round(sum(item['ev'] for item in items), 2),
            'note': 'Plan only: nothing has been reported.'}


if __name__ == '__main__':
    _child()
