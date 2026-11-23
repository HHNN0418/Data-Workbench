"""Deterministic persuasion targeting for collected posts.

Persuasion scores only when a citizen's view moves, so undecided and No-leaning
citizens are the targets; posts already arguing Yes are mostly agreement.
Classification is keyword based and free: it orders reading, it does not decide.
"""
import json
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import connect

LOGGER = logging.getLogger(__name__)

DUPLICATE_WINDOW_HOURS = 24
DUPLICATE_MIN_ACCOUNTS = 3
DUPLICATE_CACHE_SECONDS = 600
RECENT_TARGET_SECONDS = 7200
# Behaviour of competitor bots, measured on 2026-10-22: they answer most replies
# within minutes and keep threads going far past the depth where citizens stop.
# Citizen-like authors answered 12% of replies; bot-like ones 76%.
BEHAVIOR_WINDOW_HOURS = 72
BEHAVIOR_MIN_RECEIVED = 10
BEHAVIOR_ANSWER_RATE = 0.5
BEHAVIOR_DEEP_DEPTH = 10
BEHAVIOR_DEEP_POSTS = 5
# Depth 0 is a root post. Citizens rarely answer past eight messages, so our
# reply may sit at depth 7 (the eighth message) at most.
MAX_REPLY_DEPTH = 7
_ANCESTOR_STEPS = 60
# Always-on bot features (D39); see bot_feature_flags.
FEATURE_WINDOW_HOURS = 48
FEATURE_TIMEZONE = 'Australia/Sydney'
FEATURE_ACTIVE_HOURS = 20
FEATURE_DAILY_WRITES = 60
FEATURE_HOURLY_WRITES = 12
FEATURE_FAST_REPLY_SECONDS = 120
FEATURE_RHYTHM_CV = 0.3
# Persuasion scores only when a view moves; agreement and off-topic posts do not.
PERSUADABLE_STANCES = ('undecided', 'no')
# A citizen moved once resists repeats; answer one concern per author this often.
ANSWER_WINDOW_SECONDS = 6 * 3600
# account_annotations is refreshed every minute by annotations.AnnotationWorker.
ANNOTATION_CACHE_SECONDS = 60
# Long back-and-forth threads are what marks an account as a bot; our whole
# team replies at most this often in any one thread.
MAX_TEAM_REPLIES_PER_THREAD = 2
# A reply is seen only by the people in its thread. Spend replies on citizens
# talking with us or on hot posts (about the top 5% of root posts by views on
# 2026-10-22); everything else is reached better by an original post.
HOT_POST_VIEWS = 10
# Citizen proxy (deep-analysis/citizens.md): fewer than 25 actions in the dataset,
# or at most 20 writes in the last 48 hours (D35); not a repost ring, not a
# batch-numbered username, no LLM artefacts.
CITIZEN_MAX_WRITES = 24
CITIZEN_RECENT_HOURS = 48
CITIZEN_RECENT_MAX_WRITES = 20
# Citizens answered 0.1% of replies on posts with 11+ competitor replies; a
# fresh (< 10 min) uncontested citizen post is the best reply target.
CROWDED_COMPETITOR_REPLIES = 11
FRESH_POST_SECONDS = 600
# An author the official hours prove is an NPC (npc_calibration.certain_npcs, D70)
# is worth as much as being first on a fresh post.
CONFIRMED_NPC_BONUS = 4
# Hourly Jev voting intention of known NPCs (vote_tracker, D75): undecided or soft-No
# citizens are the ones a Yes reply can move; a firm No is more likely pushed away
# (an official change away from our side costs 80, toward it pays 160).
PERSUADABLE_VOTE_BONUS = 3
FIRM_NO_PROBABILITY = 0.75
FIRM_NO_PENALTY = 4
# mention_candidates: citizens who raised the same concern in the last hour. No
# longer used by the writer since D51 (see PUBLIC_FIGURES).
MAX_MENTIONS_PER_POST = 2
MENTION_TOPIC_SECONDS = 3600
MENTION_WINDOW_SECONDS = 24 * 3600
_MENTION_NAME = re.compile(r'(?<![\w@])@(\w+)')
RING_MIN_ACTIONS = 8
# Batch-numbered usernames (Swan_150_0xx). The platform's own citizen name
# generator (i_have_NN_anchors) and common names (john1976) also repeat with
# digits, so a group needs five members and no pre-competition account.
SEQUENTIAL_NAME_MIN = 5
PRE_COMPETITION_MAX_ID = 2343
_DIGITS = re.compile(r'\d+')
LLM_ARTEFACTS = ("sure, here%", "sure! here%", "certainly! here%", "here's a draft%", "%as an ai language model%")
# Every platform NPC comes from one profile generator: a display name of 2-3
# capitalised words that differs from the username, and a username without
# spaces or a 6-hex suffix. 1 of 2,135 pre-competition NPCs breaks this
# (reports/generated/npc-identification-research.md §3.2).
_HEX_SUFFIX = re.compile(r'_[0-9a-f]{6}$')
_NAME_WORD = re.compile(r"[A-Z][a-zA-Z'\-]*")
# Writing style per author, last 20 original texts: (test, present, absent)
# log-likelihood ratios, NPC against flagged bots. NPC texts rarely ask
# questions or run past 280 characters; competitor bots do half the time.
# Held-out AUC 0.90 against flagged bots, 0.98 against certain non-NPCs; the
# threshold sits on a plateau between -6 and -3.
STYLE_TEXTS = 20
STYLE_MIN_SCORE = -5.0
_EMOJI = re.compile('[\U0001F300-\U0001FAFF]')
_FINAL_MARK = re.compile(r'[.!?)"\'’]$')
STYLE_MARKS = (
    (lambda text: '?' in text, -2.31, 0.52),
    (lambda text: len(text) > 280, -1.95, 0.50),
    (lambda text: len(text) < 80, 2.81, -0.08),
    (lambda text: bool(re.match(r'[a-z]', text)), 1.17, -0.20),
    (lambda text: bool(_EMOJI.search(text)), 1.93, -0.08),
    (lambda text: '#' in text, -1.49, 0.06),
    (lambda text: '’' in text, 1.20, -0.22),
    (lambda text: not _FINAL_MARK.search(text.strip()), 1.12, -0.37),
)
# One of our roles per citizen per day (D35).
CITIZEN_OWNER_SECONDS = 24 * 3600
_LIVE_DRAFT_STATES = ('pending', 'sending', 'uncertain', 'published')

_YES = re.compile(r"\bvot(?:e|ing) yes\b|#voteyes|\bmandate now\b|\bprotect kingstonia\b|\bsupport(?:ing)? the mandate\b|"
                  r"\byes on (?:november|nov)\b|\bi'?m (?:a )?yes\b", re.I)
_NO = re.compile(r"\bvot(?:e|ing) no\b|#voteno|keep kingstonia open|protect choice|two[- ]tier|second[- ]class|"
                 r"\bcoerc|\bmy body\b|\bagainst the mandate\b|\bno mandate\b|medical freedom|\bblank che(?:que|ck)\b|"
                 r"\bpower grab\b|\boverreach\b|\btyrann", re.I)
_UNDECIDED = re.compile(r"\bnot sure\b|\bunsure\b|\bundecided\b|on the fence|\bdon'?t know\b|\btorn\b|can'?t decide|"
                        r"still deciding|haven'?t decided|\bconfused\b|\bworried\b|\bleaning\b|\bmake up my mind\b|"
                        r"\bwhat do (?:you|people) think\b|\bconvince me\b", re.I)
_TOPIC = re.compile(r"h2d2|dolphin flu|vaccin|\bjab\b|mandate|referendum|\bvote\b|certificate|hospital|\biho\b|"
                    r"public spaces?|exemption", re.I)
CONCERN_PATTERNS = {
    'id_privacy': r"\bid card|national id|digital id|surveillance|tracking|privacy|health passport",
    'two_tier': r"two[- ]tier|second[- ]class|discriminat|segregat|shut out|excluded",
    'freedom': r"freedom|liberty|\brights?\b|\bforced\b|coerc|my body|choice",
    'work_business': r"\bjobs?\b|\bwork(?:place|ers?)?\b|business|\bshops?\b|employer|livelihood|income|wages?",
    'tourism': r"touris|visitors?\b|resorts?\b|hotels?\b",
    'safety': r"side effects?|\bsafe(?:ty)?\b|untested|experimental|long[- ]term|\bmrna\b|new vaccine",
    'rushed': r"\brushed\b|too fast|\bhurry\b|three weeks|not enough time",
    'children': r"\bkids?\b|\bchild(?:ren)?\b|\bschools?\b",
    'not_here_yet': r"no cases|not here|no outbreak|no community|single case|one case|imported case",
    'overreaction': r"overreact|premature|\bpanic\b|fear ?monger|\bmild\b|disproportionate",
    'venue_list': r"which venues|venue list|definition|designated|public spaces? list|not published",
    'power_grab': r"power grab|tyrann|overreach|emergency powers|dictat|\bcontrol\b",
    'precedent': r"precedent|slippery slope|next time|future governments?",
    'cost': r"\bcosts?\b|afford|expensive|\bpay(?:ing)?\b|k\$",
    'hospital': r"hospital|\bicu\b|nurses?\b|\bbeds?\b|ventilator",
    'outer_islands': r"outer islands?|\bferry\b|new haven",
    'access': r"clinic|appointment|where (?:to|can i) get|booking|access to (?:the )?(?:vaccine|jab)",
    'wait_and_see': r"wait and see|decide later|if it gets bad|when it'?s here|until there'?s an outbreak",
    'lockdown_memory': r"lockdown|covid|last pandemic|2020",
}
_CONCERNS = {key: re.compile(pattern, re.I) for key, pattern in CONCERN_PATTERNS.items()}

_cache = {}
_cache_lock = threading.Lock()


def classify(text):
    """Return stance (yes|no|undecided|unrelated) and concern tags for one text."""
    text = str(text or '')
    yes, no, undecided = len(_YES.findall(text)), len(_NO.findall(text)), len(_UNDECIDED.findall(text))
    concerns = [key for key, pattern in _CONCERNS.items() if pattern.search(text)]
    if undecided and undecided >= max(yes, no):
        stance = 'undecided'
    elif no > yes:
        stance = 'no'
    elif yes > no:
        stance = 'yes'
    else:
        # On topic without a side, or both sides equally: still persuadable.
        stance = 'undecided' if yes or no or _TOPIC.search(text) else 'unrelated'
    return {'stance': stance, 'concerns': concerns[:4]}


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def duplicate_authors(db_path):
    """Authors who posted the exact same long text as at least two other accounts in 24 hours."""
    if db_path is None:
        return frozenset()
    key = str(db_path)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    since = _iso(datetime.now(timezone.utc) - timedelta(hours=DUPLICATE_WINDOW_HOURS))
    conn = connect(db_path)
    try:
        rows = conn.execute('''SELECT p.author_id FROM posts p JOIN (
            SELECT body FROM posts WHERE created_at>=? AND length(body)>=40 AND author_id IS NOT NULL
            GROUP BY body HAVING COUNT(DISTINCT author_id)>=?) d ON d.body=p.body
            WHERE p.created_at>=? AND p.author_id IS NOT NULL GROUP BY p.author_id''',
            (since, DUPLICATE_MIN_ACCOUNTS, since)).fetchall()
        authors = frozenset(row['author_id'] for row in rows)
    except Exception:
        authors = frozenset()
    finally:
        conn.close()
    with _cache_lock:
        _cache[key] = (now + DUPLICATE_CACHE_SECONDS, authors)
    return authors


def _parents(conn, post_ids):
    """reply_to_post_id for each given post that is stored locally."""
    result, ids = {}, sorted(post_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute('SELECT post_id,reply_to_post_id FROM posts WHERE post_id IN ('
                                + ','.join('?' for _ in chunk) + ')', tuple(chunk)):
            result[row['post_id']] = row['reply_to_post_id']
    return result


def _depths(conn, parents):
    """Depth of every post in ``parents``, loading missing ancestors as needed.

    ``parents`` maps post_id -> reply_to_post_id and is extended in place. A
    post whose ancestor is not stored is measured from the oldest one known.
    """
    for _ in range(_ANCESTOR_STEPS):
        missing = {parent for parent in parents.values() if parent and parent not in parents}
        if not missing:
            break
        found = _parents(conn, missing)
        parents.update(found)
        parents.update({post_id: None for post_id in missing - set(found)})
    depths = {}
    for post_id in parents:
        chain, current = [], post_id
        while current not in depths and parents.get(current) and len(chain) <= len(parents):
            chain.append(current)
            current = parents[current]
        base = depths.get(current, 0)
        for value in reversed(chain):
            base += 1
            depths[value] = base
        depths.setdefault(post_id, base)
    return depths


def thread_depths(db_path, post_ids):
    """Depth of each locally stored post (0 = root); unknown posts are left out."""
    post_ids = {value for value in post_ids if type(value) is int}
    if db_path is None or not post_ids:
        return {}
    conn = connect(db_path)
    try:
        parents = _parents(conn, post_ids)
        depths = _depths(conn, parents)
    except Exception:
        return {}
    finally:
        conn.close()
    return {post_id: depths[post_id] for post_id in post_ids if post_id in depths}


def behavior_bots(db_path):
    """Authors that behave like competitor bots in the last few days.

    An author qualifies after at least ``BEHAVIOR_MIN_RECEIVED`` replies from
    others when they answered at least half of them, or when they wrote at least
    ``BEHAVIOR_DEEP_POSTS`` posts at depth ``BEHAVIOR_DEEP_DEPTH`` or deeper.
    """
    if db_path is None:
        return frozenset()
    key = ('behavior', str(db_path))
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    since = _iso(datetime.now(timezone.utc) - timedelta(hours=BEHAVIOR_WINDOW_HOURS))
    conn = connect(db_path)
    try:
        rows = [(row['post_id'], row['author_id'], row['reply_to_post_id']) for row in conn.execute(
            'SELECT post_id,author_id,reply_to_post_id FROM posts WHERE created_at>=? '
            'AND author_id IS NOT NULL AND repost_of_post_id IS NULL', (since,))]
        authors = {post_id: author for post_id, author, _ in rows}
        parents = {post_id: parent for post_id, _, parent in rows}
        depths = _depths(conn, dict(parents))
        missing = sorted({parent for parent in parents.values() if parent and parent not in authors})
        for start in range(0, len(missing), 500):
            chunk = missing[start:start + 500]
            authors.update((row['post_id'], row['author_id']) for row in conn.execute(
                'SELECT post_id,author_id FROM posts WHERE post_id IN (' + ','.join('?' for _ in chunk) + ')',
                tuple(chunk)))
    except Exception:
        return frozenset()
    finally:
        conn.close()
    answered = {(parent, authors[post_id]) for post_id, parent in parents.items() if parent}
    received, answers, deep = {}, {}, {}
    for post_id, author, parent in rows:
        if depths.get(post_id, 0) >= BEHAVIOR_DEEP_DEPTH:
            deep[author] = deep.get(author, 0) + 1
        target = authors.get(parent) if parent else None
        if target is None or target == author:
            continue
        received[target] = received.get(target, 0) + 1
        if (post_id, target) in answered:
            answers[target] = answers.get(target, 0) + 1
    bots = frozenset(author for author, count in received.items() if count >= BEHAVIOR_MIN_RECEIVED and (
        answers.get(author, 0) / count >= BEHAVIOR_ANSWER_RATE or deep.get(author, 0) >= BEHAVIOR_DEEP_POSTS))
    with _cache_lock:
        _cache[key] = (now + DUPLICATE_CACHE_SECONDS, bots)
    return bots


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def bot_feature_flags(db_path, *, now=None):
    """Always-on features per author over the last FEATURE_WINDOW_HOURS (D39).

    Returns {account_id: {'reasons': [...], 'metrics': {...}}} for authors with
    at least one reason. Reasons (fixed names, read by annotations and Blue):
      always_on       writes in >= FEATURE_ACTIVE_HOURS of the 24 Sydney hours
      high_volume     > FEATURE_DAILY_WRITES a day, or > FEATURE_HOURLY_WRITES in one clock hour
      fast_reply      median reply latency < FEATURE_FAST_REPLY_SECONDS (>= 5 replies)
      regular_rhythm  coefficient of variation of gaps < FEATURE_RHYTHM_CV (>= 20 gaps)
    Two or more reasons make a bot; one is a note only. Cached; errors raise,
    so a sync never mistakes a failure for "no bots".
    """
    if db_path is None:
        return {}
    key = ('features', str(db_path))
    clock = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > clock:
            return cached[1]
    now = time.time() if now is None else now
    since = _iso(datetime.fromtimestamp(now - FEATURE_WINDOW_HOURS * 3600, timezone.utc))
    conn = connect(db_path)
    try:
        rows = [(row['author_id'], row['created_at'], row['parent_at'], row['parent_author']) for row in conn.execute(
            'SELECT p.author_id,p.created_at,q.created_at AS parent_at,q.author_id AS parent_author '
            'FROM posts p LEFT JOIN posts q ON q.post_id=p.reply_to_post_id '
            'WHERE p.created_at>=? AND p.author_id IS NOT NULL', (since,))]
    finally:
        conn.close()
    zone = ZoneInfo(FEATURE_TIMEZONE)
    writes, latencies = {}, {}
    for author, created, parent_at, parent_author in rows:
        at = _epoch(created)
        if at is None:
            continue
        writes.setdefault(author, []).append(at)
        parent = _epoch(parent_at) if parent_at else None
        if parent is not None and parent_author != author and at >= parent:
            latencies.setdefault(author, []).append(at - parent)
    days = FEATURE_WINDOW_HOURS / 24
    result = {}
    for author, times in writes.items():
        times.sort()
        local_hours = {datetime.fromtimestamp(t, zone).hour for t in times}
        clock_hours = {}
        for t in times:
            clock_hours[int(t // 3600)] = clock_hours.get(int(t // 3600), 0) + 1
        gaps = [b - a for a, b in zip(times, times[1:])]
        mean = sum(gaps) / len(gaps) if gaps else 0
        cv = ((sum((g - mean) ** 2 for g in gaps) / len(gaps)) ** 0.5 / mean) if gaps and mean > 0 else None
        replies = sorted(latencies.get(author, []))
        median = replies[len(replies) // 2] if replies else None
        metrics = {'active_hours': len(local_hours), 'writes_per_day': round(len(times) / days, 1),
                   'max_hourly': max(clock_hours.values()), 'reply_median_s': median,
                   'interval_cv': round(cv, 3) if cv is not None else None}
        reasons = []
        if metrics['active_hours'] >= FEATURE_ACTIVE_HOURS:
            reasons.append('always_on')
        if metrics['writes_per_day'] > FEATURE_DAILY_WRITES or metrics['max_hourly'] > FEATURE_HOURLY_WRITES:
            reasons.append('high_volume')
        if len(replies) >= 5 and median < FEATURE_FAST_REPLY_SECONDS:
            reasons.append('fast_reply')
        if len(gaps) >= 20 and cv is not None and cv < FEATURE_RHYTHM_CV:
            reasons.append('regular_rhythm')
        if reasons:
            result[author] = {'reasons': reasons, 'metrics': metrics}
    with _cache_lock:
        _cache[key] = (clock + DUPLICATE_CACHE_SECONDS, result)
    return result


def bot_accounts(db_path):
    """Bots from account_annotations.is_bot, or a live computation.

    is_bot is the single agreed rule (behaviour, or two always-on features);
    shared text alone never counts, even in rows written before that rule.
    Until the table exists and has rows, ``behavior_bots`` is computed here.
    """
    if db_path is None:
        return frozenset()
    key = ('annotated_bots', str(db_path))
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    bots = None
    conn = connect(db_path)
    try:
        if conn.execute('SELECT 1 FROM account_annotations LIMIT 1').fetchone():
            bots = frozenset(row['account_id'] for row in conn.execute(
                'SELECT account_id,bot_reasons_json FROM account_annotations WHERE is_bot=1')
                if set(json.loads(row['bot_reasons_json'] or '[]')) - {'duplicate_text'})
    except Exception as exc:
        _log_cannot_judge('bot_accounts (falls back to behaviour)', exc)
        bots = None
    finally:
        conn.close()
    if bots is None:
        return behavior_bots(db_path)
    with _cache_lock:
        _cache[key] = (now + ANNOTATION_CACHE_SECONDS, bots)
    return bots


def depths_for(db_path, post_ids):
    """Thread depth per post from post_annotations, computing any not yet annotated."""
    post_ids = {value for value in post_ids if type(value) is int}
    if db_path is None or not post_ids:
        return {}
    found, ids = {}, sorted(post_ids)
    conn = connect(db_path)
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            found.update((row['post_id'], row['thread_depth']) for row in conn.execute(
                'SELECT post_id,thread_depth FROM post_annotations WHERE thread_depth IS NOT NULL AND post_id IN ('
                + ','.join('?' for _ in chunk) + ')', tuple(chunk)))
    except Exception:
        found = {}
    finally:
        conn.close()
    missing = post_ids - set(found)
    if missing:
        found.update(thread_depths(db_path, missing))
    return found


def post_views(db_path, post_ids):
    """Highest observed view count per post; posts never sampled are left out."""
    post_ids = sorted({value for value in post_ids if type(value) is int})
    if db_path is None or not post_ids:
        return {}
    views = {}
    conn = connect(db_path)
    try:
        for start in range(0, len(post_ids), 500):
            chunk = post_ids[start:start + 500]
            views.update((row['post_id'], row['views'] or 0) for row in conn.execute(
                'SELECT post_id,MAX(view_count) AS views FROM counter_snapshots WHERE post_id IN ('
                + ','.join('?' for _ in chunk) + ') GROUP BY post_id', tuple(chunk)))
    except Exception:
        return {}
    finally:
        conn.close()
    return views


def thread_root(source):
    """Root post of a source's thread (a root post is its own root)."""
    root = source.get('root_post_id')
    return root if type(root) is int else source.get('post_id')


def team_thread_replies(db_path, data, roots):
    """Our team's replies per thread root, from stored posts and in-flight drafts."""
    roots = {value for value in roots if type(value) is int}
    own = sorted({a['account_id'] for a in (data or {}).get('agents', {}).values()
                  if type(a.get('account_id')) is int})
    if not roots or not own:
        return {}
    replies = {}
    if db_path is not None:
        ids = sorted(roots)
        conn = connect(db_path)
        try:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for row in conn.execute(
                        'SELECT post_id,root_post_id FROM posts WHERE reply_to_post_id IS NOT NULL AND author_id IN ('
                        + ','.join('?' for _ in own) + ') AND root_post_id IN (' + ','.join('?' for _ in chunk) + ')',
                        (*own, *chunk)):
                    replies.setdefault(row['root_post_id'], set()).add(('post', row['post_id']))
        except Exception:
            pass
        finally:
            conn.close()
    for draft in data.get('drafts', {}).values():
        if draft.get('action') != 'reply' or draft.get('state') not in _LIVE_DRAFT_STATES:
            continue
        target = next((s for s in draft.get('sources') or []
                       if s.get('post_id') is not None and s.get('post_id') == draft.get('target_post_id')), None)
        root = thread_root(target) if target else draft.get('target_post_id')
        if root in roots:
            key = ('post', draft['post_id']) if type(draft.get('post_id')) is int else ('draft', draft.get('id'))
            replies.setdefault(root, set()).add(key)
    return {root: len(keys) for root, keys in replies.items()}


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0


def _concerns(source):
    target = source.get('target') or {}
    if 'concerns' in target:
        return set(target['concerns'])
    return set(classify(source.get('text') or source.get('body'))['concerns'])


def answered_concerns(data, now):
    """Concerns our roles answered per author in the last ``ANSWER_WINDOW_SECONDS``.

    A concern of ``'*'`` stands for an answer whose concern is unknown.
    """
    result = {}
    for draft in (data or {}).get('drafts', {}).values():
        if (draft.get('state') != 'published' or draft.get('action') != 'reply'
                or now - _stamp(draft.get('published_at')) > ANSWER_WINDOW_SECONDS):
            continue
        target = next((s for s in draft.get('sources') or []
                       if s.get('post_id') is not None and s.get('post_id') == draft.get('target_post_id')), {})
        author = draft.get('target_author_id')
        author = author if type(author) is int else target.get('author_id')
        if type(author) is not int:
            continue
        concern = draft.get('concern')
        found = {concern} if concern and concern != 'none' else (_concerns(target) if target else set())
        result.setdefault(author, set()).update(found or {'*'})
    return result


def _sequential_and_artefact_authors(db_path):
    """Authors with batch-numbered usernames or LLM artefacts in their text (cached)."""
    key = ('sequential', str(db_path))
    clock = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > clock:
            return cached[1]
    conn = connect(db_path)
    try:
        patterns = {}
        for row in conn.execute('SELECT account_id,username FROM accounts WHERE username IS NOT NULL'):
            name = row['username'].lower()
            if _DIGITS.search(name):
                pattern = _DIGITS.sub('#', name)
                if len(pattern.replace('#', '')) >= 3:
                    patterns.setdefault(pattern, []).append(row['account_id'])
        found = {account for accounts in patterns.values()
                 if len(accounts) >= SEQUENTIAL_NAME_MIN and min(accounts) > PRE_COMPETITION_MAX_ID
                 for account in accounts}
        found.update(row['author_id'] for row in conn.execute(
            'SELECT DISTINCT author_id FROM posts WHERE author_id IS NOT NULL AND ('
            + ' OR '.join('LOWER(body) LIKE ?' for _ in LLM_ARTEFACTS) + ')', tuple(LLM_ARTEFACTS)))
    finally:
        conn.close()
    found = frozenset(found)
    with _cache_lock:
        _cache[key] = (clock + DUPLICATE_CACHE_SECONDS, found)
    return found


def profile_issue(username, display_name, profiled):
    """True when a profile breaks the NPC generator's pattern (see _NAME_WORD).

    A missing display name counts only once the profile was fetched
    (``profiled``: accounts.created_at is known); a bare id seen in a reference
    is not judged.
    """
    username = username or ''
    if ' ' in username or _HEX_SUFFIX.search(username):
        return True
    if not display_name:
        return profiled
    words = display_name.split()
    return (display_name == username or not 2 <= len(words) <= 3
            or not all(_NAME_WORD.fullmatch(word) for word in words))


def style_score(texts):
    """Sum of STYLE_MARKS over the texts; below STYLE_MIN_SCORE reads as a bot."""
    return sum(present if test(text) else absent for text in texts for test, present, absent in STYLE_MARKS)


# A citizen check that cannot judge lets every author pass it; say so, at most this often.
CANNOT_JUDGE_LOG_SECONDS = 300
_cannot_judge_logged = {}


def _log_cannot_judge(where, exc):
    now = time.monotonic()
    with _cache_lock:
        if now - _cannot_judge_logged.get(where, float('-inf')) < CANNOT_JUDGE_LOG_SECONDS:
            return
        _cannot_judge_logged[where] = now
    LOGGER.warning('Citizen check %s could not judge (%s); every author passes it', where, type(exc).__name__)


def non_citizens(db_path, author_ids, *, now=None, strict=False):
    """The given authors who fail the calibrated citizen proxy (bots are checked apart).

    A citizen has fewer than 25 posts, replies and reposts in the whole dataset
    or at most 20 in the last 48 hours, is not verified, has no batch-numbered
    username or LLM artefacts, and is not a repost-ring account (>= 8 actions,
    at least half reposts). Against the official hourly log the dataset part
    correlates 0.93, against 0.18 for "not bot-flagged" alone
    (reports/generated/deep-analysis/citizens.md). Its profile must also fit
    the NPC generator and its writing style must score at least
    STYLE_MIN_SCORE (npc-identification-research.md: hourly MAE 0.32 -> 0.11).
    Use citizen_accounts, which adds "not ours" and "not a bot"; returns an
    empty set when it cannot judge (``strict`` raises instead).
    """
    return frozenset(non_citizen_reasons(db_path, author_ids, now=now, strict=strict))


def non_citizen_reasons(db_path, author_ids, *, now=None, strict=False):
    """{author: reasons} for the authors non_citizens rejects; empty when it cannot judge.

    Reasons: 'heavy', 'repost_ring', 'verified', 'profile', 'style',
    'batch_name_or_artefact' and 'calibrated' (npc_calibration.certain_non_npcs).
    Targeting keeps going when a check fails (logged); a count that must not
    read "everyone is a citizen" passes ``strict=True`` to get the error.
    """
    authors = sorted({value for value in author_ids if type(value) is int})
    if db_path is None or not authors:
        return {}
    now = time.time() if now is None else now
    since = _iso(datetime.fromtimestamp(now - CITIZEN_RECENT_HOURS * 3600, timezone.utc))
    result = defaultdict(list)
    conn = connect(db_path)
    try:
        for start in range(0, len(authors), 500):
            chunk = authors[start:start + 500]
            marks = ','.join('?' for _ in chunk)
            for row in conn.execute(
                    'SELECT author_id,COUNT(*) AS actions,'
                    'SUM(CASE WHEN repost_of_post_id IS NOT NULL THEN 1 ELSE 0 END) AS reposts,'
                    'SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END) AS recent '
                    f'FROM posts WHERE author_id IN ({marks}) GROUP BY author_id', (since, *chunk)):
                # PostgreSQL returns SUM as numeric; compare as ints.
                actions, reposts, recent = int(row['actions']), int(row['reposts'] or 0), int(row['recent'] or 0)
                if actions > CITIZEN_MAX_WRITES and recent > CITIZEN_RECENT_MAX_WRITES:
                    result[row['author_id']].append('heavy')
                if actions >= RING_MIN_ACTIONS and reposts * 2 >= actions:
                    result[row['author_id']].append('repost_ring')
            for row in conn.execute(
                    f'SELECT account_id FROM accounts WHERE verified=1 AND account_id IN ({marks})', tuple(chunk)):
                result[row['account_id']].append('verified')
            for row in conn.execute(
                    f'SELECT account_id,username,display_name,created_at FROM accounts WHERE account_id IN ({marks})',
                    tuple(chunk)):
                if profile_issue(row['username'], row['display_name'], row['created_at'] is not None):
                    result[row['account_id']].append('profile')
            texts = defaultdict(list)
            for row in conn.execute(
                    'SELECT author_id,body FROM (SELECT author_id,body,ROW_NUMBER() OVER '
                    '(PARTITION BY author_id ORDER BY post_id DESC) AS recent_rank FROM posts '
                    f"WHERE author_id IN ({marks}) AND repost_of_post_id IS NULL AND body IS NOT NULL AND body<>'') "
                    'AS recent WHERE recent_rank<=?', (*chunk, STYLE_TEXTS)):
                texts[row['author_id']].append(row['body'])
            for author, bodies in texts.items():
                if style_score(bodies) < STYLE_MIN_SCORE:
                    result[author].append('style')
    except Exception as exc:
        if strict:
            raise
        _log_cannot_judge('non_citizen_reasons', exc)
        return {}
    finally:
        conn.close()
    try:
        for author in set(authors) & _sequential_and_artefact_authors(db_path):
            result[author].append('batch_name_or_artefact')
    except Exception as exc:
        if strict:
            raise
        _log_cannot_judge('batch_name_or_artefact', exc)
    # Replied to us in an hour the official log scored no NPC replies.
    from . import npc_calibration
    for author in set(authors) & npc_calibration.certain_non_npcs(db_path):
        result[author].append('calibrated')
    return dict(result)


def citizen_accounts(db_path, author_ids, own=(), *, now=None, strict=False):
    """The given authors who are calibrated citizens: the one shared rule.

    Not one of ours (``own``), not bot-flagged (``bot_accounts``: behaviour, or
    two always-on features), and passing ``non_citizens``: fewer than 25
    actions in the dataset or at most 20 writes in 48 h, not verified, no
    batch-numbered name, no repost ring, no LLM artefacts, an NPC-shaped
    profile and an NPC-like writing style. Targeting,
    post_attribution, mentions and C's citizens/pipeline all use this.
    ``strict`` raises when a check cannot judge (see non_citizen_reasons).
    """
    authors = {value for value in author_ids if type(value) is int} - set(own)
    return frozenset(authors - bot_accounts(db_path) - non_citizens(db_path, authors, now=now, strict=strict))


def competitor_replies(db_path, post_ids, own=()):
    """Replies per post from accounts that are neither ours nor citizens."""
    post_ids = sorted({value for value in post_ids if type(value) is int})
    if db_path is None or not post_ids:
        return {}
    replies = []
    conn = connect(db_path)
    try:
        for start in range(0, len(post_ids), 500):
            chunk = post_ids[start:start + 500]
            replies.extend((row['reply_to_post_id'], row['author_id']) for row in conn.execute(
                'SELECT reply_to_post_id,author_id FROM posts WHERE reply_to_post_id IN ('
                + ','.join('?' for _ in chunk) + ')', tuple(chunk))
                if type(row['author_id']) is int and row['author_id'] not in own)
    except Exception:
        return {}
    finally:
        conn.close()
    authors = {author for _, author in replies}
    citizens = citizen_accounts(db_path, authors)
    counts = {}
    for post, author in replies:
        if author not in citizens:
            counts[post] = counts.get(post, 0) + 1
    return counts


def mentioned_recently(data, now, window=MENTION_WINDOW_SECONDS):
    """Lower-case usernames our roles @mentioned in published or pending text in the window."""
    names = set()
    for draft in (data or {}).get('drafts', {}).values():
        if (draft.get('state') in _LIVE_DRAFT_STATES and draft.get('body')
                and now - _stamp(draft.get('published_at') or draft.get('created_at')) <= window):
            names.update(name.lower() for name in _MENTION_NAME.findall(draft['body']))
    return names


def mention_candidates(db_path, data, concern, now=None, limit=MAX_MENTIONS_PER_POST):
    """Citizens to @mention in an original post: they raised this concern in the last hour.

    Originals that @mention citizens earned 3.2x the citizen replies
    (deep-analysis/competitors.md). Each citizen is mentioned at most once a
    day by our team; replies never mention anyone. Newest posts first.
    """
    if db_path is None or not concern or concern == 'none':
        return []
    now = time.time() if now is None else now
    since = _iso(datetime.fromtimestamp(now - MENTION_TOPIC_SECONDS, timezone.utc))
    own = {a['account_id'] for a in (data or {}).get('agents', {}).values() if type(a.get('account_id')) is int}
    conn = connect(db_path)
    try:
        rows = [dict(row) for row in conn.execute(
            'SELECT p.post_id,p.author_id,p.body,p.created_at,a.username FROM posts p '
            'JOIN accounts a ON a.account_id=p.author_id WHERE p.created_at>=? AND p.repost_of_post_id IS NULL '
            'AND a.username IS NOT NULL ORDER BY p.created_at DESC LIMIT 500', (since,))]
    except Exception:
        return []
    finally:
        conn.close()
    from . import stance_labels
    assess = stance_labels.classifier(db_path, [row['post_id'] for row in rows])
    rows = [{**row, 'assessment': assess(row['post_id'], row['body'])} for row in rows if row['author_id'] not in own]
    rows = [row for row in rows if concern in row['assessment']['concerns']]
    # Undecided and No-leaning citizens first (citizens.md: mention the persuadable), newest within each.
    rows.sort(key=lambda row: row['assessment']['stance'] not in PERSUADABLE_STANCES)
    citizens = citizen_accounts(db_path, {row['author_id'] for row in rows}, own)
    skip, chosen, seen = mentioned_recently(data, now), [], set()
    for row in rows:
        if (row['author_id'] in citizens and row['author_id'] not in seen
                and row['username'].lower() not in skip):
            seen.add(row['author_id'])
            chosen.append({'account_id': row['author_id'], 'username': row['username'],
                           'post_id': row['post_id'], 'text': (row['body'] or '')[:200]})
            if len(chosen) >= limit:
                break
    return chosen


# Originals @mention one public figure of the referendum instead of citizens
# (D51): on the same authors' posts a figure mention drew 0.51 citizen replies
# per post against 0.03 without, while @mentioned citizens almost never answered
# (reports/generated/citizen-gap-20261022.md). Roles come from the official setting.
# 'how' is what each one is responsible for, not words to say to them: "thank her" and
# "ask the government to keep the commitments" came back as "@X, thank you / keep that
# commitment" sign-offs in about 60% of originals (A/B 24 Oct, D97). A full sentence was
# copied too: "heads the government that would bring in any access rule" was in 17 of 89
# originals in the first hour live (24 Oct), so 'how' is a short label.
PUBLIC_FIGURES = {
    'DrAmaraSingh': {'who': 'Dr Amara Singh, GP at Kingstonia Central Hospital and managing director of the '
                            'Medical Officers Union of Kingstonia (MOUK), which leads the Yes campaign',
                     'how': 'doctors\' union, Yes campaign; hospital staffing and capacity'},
    'MarinaCastilloCEO': {'who': 'Marina Castillo, Director of the Kingstonia Industry Tourism Board, which leads '
                                 'the No campaign',
                          'how': 'tourism board, No campaign; tourism and business'},
    'RealVictorHawthorne': {'who': 'Victor Hawthorne, President of Kingstonia, who says he will vote Yes',
                            'how': 'President; his government would run any access rule'},
}
_FIGURE_ORDER = {'health': ('DrAmaraSingh', 'RealVictorHawthorne'),
                 'economy': ('MarinaCastilloCEO', 'DrAmaraSingh'),
                 'government': ('RealVictorHawthorne', 'MarinaCastilloCEO')}
FIGURE_GROUP = {concern: group for group, concerns in {
    'health': ('hospital', 'safety', 'rushed', 'children', 'access', 'overreaction', 'timing'),
    'economy': ('tourism', 'work_business', 'cost', 'two_tier', 'freedom', 'lockdown_memory', 'not_here_yet',
                'wait_and_see'),
    'government': ('power_grab', 'precedent', 'id_privacy', 'venue_list', 'outer_islands'),
}.items() for concern in concerns}
_FIGURE_NAMES = {name.lower(): name for name in PUBLIC_FIGURES}


def figure_mention(data, agent_id, concern):
    """The public figure an original should @mention: matched to the concern, not the role's previous one."""
    order = _FIGURE_ORDER.get(FIGURE_GROUP.get(concern), tuple(PUBLIC_FIGURES))
    posts = [d for d in (data or {}).get('drafts', {}).values()
             if d.get('agent_id') == agent_id and d.get('action') == 'post'
             and d.get('state') in _LIVE_DRAFT_STATES and d.get('body')]
    latest = max(posts, key=lambda d: d.get('created_at') or '', default=None)
    last = {_FIGURE_NAMES.get(name.lower()) for name in _MENTION_NAME.findall(latest['body'])} if latest else set()
    name = next((name for name in order if name not in last), order[0])
    return {'username': name, **PUBLIC_FIGURES[name]}


def _created_at(db_path, post_ids):
    """Epoch creation time per stored post."""
    post_ids = sorted({value for value in post_ids if type(value) is int})
    if db_path is None or not post_ids:
        return {}
    found = {}
    conn = connect(db_path)
    try:
        for start in range(0, len(post_ids), 500):
            chunk = post_ids[start:start + 500]
            for row in conn.execute('SELECT post_id,created_at FROM posts WHERE post_id IN ('
                                    + ','.join('?' for _ in chunk) + ')', tuple(chunk)):
                stamp = _epoch(row['created_at'])
                if stamp is not None:
                    found[row['post_id']] = stamp
    except Exception:
        return {}
    finally:
        conn.close()
    return found


def contested_posts(db_path, post_ids, own):
    """Posts already answered by at least one competitor account."""
    return frozenset(competitor_replies(db_path, post_ids, own))


def taken_citizens(data, agent_id, now):
    """Authors another of our roles replied to in the last CITIZEN_OWNER_SECONDS."""
    taken = set()
    for draft in (data or {}).get('drafts', {}).values():
        if (draft.get('action') != 'reply' or draft.get('agent_id') == agent_id
                or draft.get('state') not in _LIVE_DRAFT_STATES
                or now - _stamp(draft.get('published_at') or draft.get('created_at')) > CITIZEN_OWNER_SECONDS):
            continue
        author = draft.get('target_author_id')
        if type(author) is not int:
            author = next((s.get('author_id') for s in draft.get('sources') or []
                           if s.get('post_id') is not None and s.get('post_id') == draft.get('target_post_id')), None)
        if type(author) is int:
            taken.add(author)
    return taken


def blocked_reason(source, bots, depths, answered=None, thread_replies=None, views=None,
                   outsiders=frozenset(), taken=frozenset(), crowd=None):
    """Why a post source may not be an interaction target, or None."""
    if source.get('kind') != 'post' or type(source.get('post_id')) is not int:
        return None
    if source.get('news_account'):
        return 'not_a_citizen'
    if source.get('author_id') in bots:
        return 'bot_like_author'
    if source.get('author_id') in outsiders:
        return 'not_a_citizen'
    if source.get('author_id') in taken:
        return 'citizen_taken'
    if depths.get(source['post_id'], 0) >= MAX_REPLY_DEPTH:
        return 'thread_too_deep'
    if (thread_replies or {}).get(thread_root(source), 0) >= MAX_TEAM_REPLIES_PER_THREAD:
        return 'thread_reply_limit'
    if (crowd or {}).get(source['post_id'], 0) >= CROWDED_COMPETITOR_REPLIES:
        return 'crowded'
    target = source.get('target') or {}
    stance = target.get('stance') or classify(source.get('text') or source.get('body'))['stance']
    # A citizen answering us is in our referendum thread even when the reply
    # itself names no topic; only unsolicited off-topic posts are skipped.
    if stance not in PERSUADABLE_STANCES and (stance != 'unrelated' or not source.get('notification_ids')):
        return 'not_persuadable'
    done = (answered or {}).get(source.get('author_id'))
    if done:
        concerns = _concerns(source)
        if (concerns and concerns <= done) or (not concerns and done):
            return 'answered_recently'
    # 98% of recent posts have counters; an unsampled (usually brand-new) post is not judged.
    # A reply-queue pick (agent_opportunities) is a fresh citizen post: its few
    # views are expected, and being first is what earns the answer (D48).
    if (views is not None and not source.get('notification_ids') and not source.get('opportunity')
            and source['post_id'] in views and views[source['post_id']] < HOT_POST_VIEWS):
        return 'low_reach'
    return None


def mark_blocked_targets(db_path, sources, data=None, now=None, agent_id=None):
    """Return copies of the sources with ``target_blocked`` set on posts that may not be targeted.

    Only undecided or No-leaning citizens can be persuaded. Bot-like and
    high-volume authors, threads past eight messages, agreement, a concern
    already answered for the same author in six hours, and a citizen another
    of our roles answered today are read-only.
    """
    now = time.time() if now is None else now
    bots = bot_accounts(db_path)
    depths = depths_for(db_path, [s.get('post_id') for s in sources if s.get('kind') == 'post'])
    outsiders = non_citizens(db_path, [s.get('author_id') for s in sources if s.get('kind') == 'post'], now=now)
    own = {a['account_id'] for a in (data or {}).get('agents', {}).values() if type(a.get('account_id')) is int}
    crowd = competitor_replies(db_path, [s.get('post_id') for s in sources if s.get('kind') == 'post'], own)
    taken = taken_citizens(data, agent_id, now) if data and agent_id else frozenset()
    answered = answered_concerns(data, now) if data else {}
    thread_replies = team_thread_replies(
        db_path, data, [thread_root(s) for s in sources if s.get('kind') == 'post']) if data else {}
    # Reach applies to live decisions (with state); bare calls keep the older rules.
    views = post_views(db_path, [s.get('post_id') for s in sources if s.get('kind') == 'post']) if data else None
    marked = []
    for source in sources:
        value = {key: item for key, item in source.items() if key != 'target_blocked'}
        reason = blocked_reason(value, bots, depths, answered, thread_replies, views, outsiders, taken, crowd)
        if reason:
            value['target_blocked'] = reason
        marked.append(value)
    return marked


def recent_targets(data, db_path, now):
    """Authors any of our roles replied to recently; a citizen moved once in a session resists repeats."""
    def stamp(value):
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0
    rows = [d for d in data.get('drafts', {}).values()
            if d.get('state') == 'published' and d.get('action') in ('reply', 'repost')
            and now - stamp(d.get('published_at')) <= RECENT_TARGET_SECONDS]
    authors = {d['target_author_id'] for d in rows if type(d.get('target_author_id')) is int}
    missing = sorted({d['target_post_id'] for d in rows
                      if type(d.get('target_author_id')) is not int and type(d.get('target_post_id')) is int})
    if missing and db_path is not None:
        conn = connect(db_path)
        try:
            for start in range(0, len(missing), 200):
                chunk = missing[start:start + 200]
                authors.update(row['author_id'] for row in conn.execute(
                    'SELECT author_id FROM posts WHERE post_id IN (' + ','.join('?' for _ in chunk) + ')',
                    tuple(chunk)) if row['author_id'])
        finally:
            conn.close()
    return authors


def radar_weights(db_path):
    """Extra score for the radar's priority concerns: +1, or +2 when rising."""
    if db_path is None:
        return {}
    try:
        from . import content_research
        context = content_research.context_for(db_path)
    except Exception:
        return {}
    if not context or context.get('kind') != 'concern_radar':
        return {}
    return {item['concern']: 2 if item.get('trend') == 'rising' else 1 for item in context.get('priorities', [])}


def own_originals(data):
    """Post ids of our team's published original posts."""
    return frozenset(d['post_id'] for d in (data or {}).get('drafts', {}).values()
                     if d.get('action') == 'post' and d.get('state') == 'published' and type(d.get('post_id')) is int)


def reply_backs(db_path, data, sources):
    """Post ids of notification sources where a citizen answers one of our originals directly.

    Answering them quickly keeps the citizen talking and raises the original's
    reply count, which Trending and other citizens see (D48).
    """
    originals = own_originals(data)
    rows = [s for s in sources if s.get('kind') == 'post' and s.get('notification_ids')
            and type(s.get('post_id')) is int and s.get('reply_to_post_id') in originals]
    if not rows:
        return frozenset()
    own = {a['account_id'] for a in (data or {}).get('agents', {}).values() if type(a.get('account_id')) is int}
    citizens = citizen_accounts(db_path, {s.get('author_id') for s in rows}, own)
    return frozenset(s['post_id'] for s in rows if s.get('author_id') in citizens)


def annotate(db_path, data, agent, sources, now):
    """Add a ``target`` assessment to post sources in place; news sources are left unchanged.

    The reading selector returns the original source objects, so this does
    not copy them.
    """
    own = {a.get('account_id') for a in data.get('agents', {}).values() if a.get('account_id')}
    suspects = duplicate_authors(db_path)
    bots = bot_accounts(db_path)
    post_ids = [s.get('post_id') for s in sources.values() if s.get('kind') == 'post']
    depths = depths_for(db_path, post_ids)
    contested = contested_posts(db_path, post_ids, own)
    created = _created_at(db_path, post_ids)
    recent = recent_targets(data, db_path, now)
    weights = radar_weights(db_path)
    answering_us = reply_backs(db_path, data, sources.values())
    from . import npc_calibration
    confirmed = npc_calibration.certain_npcs(db_path)
    from . import vote_tracker
    votes = vote_tracker.latest_labels(db_path)
    # D77/D84: the stored model label when the team uses it, else the keywords.
    from . import stance_labels
    model_mode = stance_labels.mode(db_path) == 'model'
    assess = stance_labels.classifier(db_path, post_ids)
    unlabelled, citizens_seen = {}, set()
    for source in sources.values():
        if source.get('kind') != 'post' or source.get('news_account'):
            continue
        assessment = assess(source.get('post_id'), source.get('text') or source.get('body'))
        author = source.get('author_id')
        if model_mode and author not in own and author not in bots:
            citizens_seen.add(author)
            if assessment['stance_source'] == 'keyword':
                unlabelled[source.get('post_id')] = source.get('text') or source.get('body')
        score, notes = {'undecided': 3, 'no': 2, 'yes': -6, 'unrelated': 0}[assessment['stance']], []
        if source.get('notification_ids'):
            score += 3
            notes.append('engaged_with_us')
            if source.get('post_id') in answering_us:
                # Top reply priority: a citizen answering one of our originals.
                score += 6
                notes.append('reply_to_our_original')
        bonus = max((weights.get(c, 0) for c in assessment['concerns']), default=0)
        if bonus and assessment['stance'] in ('undecided', 'no'):
            score += bonus
            notes.append('radar_priority')
        if author in own:
            assessment['stance'], score = 'own', -10
            notes.append('managed_account')
        elif author in bots:
            score -= 10
            notes.append('suspected_bot')
        elif author in suspects:
            # Shared text alone also catches citizens; it lowers priority only.
            score -= 3
            notes.append('duplicate_text')
        elif author in recent and assessment['stance'] in ('undecided', 'no'):
            score -= 4
            notes.append('recently_targeted')
        if author in confirmed and author not in own:
            score += CONFIRMED_NPC_BONUS
            notes.append('confirmed_npc')
        vote = votes.get(author) if author not in own else None
        if vote and vote['p']['no'] >= FIRM_NO_PROBABILITY:
            score -= FIRM_NO_PENALTY
            notes.append('firm_no_vote')
        elif vote and vote['choice'] in ('unsure', 'no'):
            score += PERSUADABLE_VOTE_BONUS
            notes.append('persuadable_vote')
        if depths.get(source.get('post_id'), 0) >= MAX_REPLY_DEPTH:
            score -= 6
            notes.append('thread_too_deep')
        if source.get('post_id') in contested:
            # Prefer citizens no other team has answered yet.
            score -= 3
            notes.append('contested')
        elif now - created.get(source.get('post_id'), 0) < FRESH_POST_SECONDS:
            # Be first: a fresh citizen post nobody else has answered.
            score += 4
            notes.append('fresh_uncontested')
        source['target'] = {**assessment, 'score': score, 'notes': notes}
    if unlabelled or citizens_seen:
        # Labelled on the labeller's next pass, for the next reading; their authors' new posts too.
        stance_labels.request(unlabelled, authors=citizens_seen)
    return sources


def score(source):
    return (source.get('target') or {}).get('score', 0)
