"""Copy patterns: the wording every account's posts share, over the last 48 hours.

The risk page's 文案识别 section (GET /api/risk/overview, key copy_patterns).
Our hidden posts (24 Oct) share one writing skeleton and the same lines, so
this reads the text of every collected post, from our roles, teammates, other
teams and NPCs alike:

  * shared phrases: after normalising (lower case; links, @mentions,
    apostrophes and punctuation removed) every run of GRAM_WORDS words that
    MIN_ACCOUNTS or more different accounts used. Overlapping phrases used by
    exactly the same accounts that sit next to each other in a post count as
    one longer phrase. Accounts are listed per phrase and never merged across
    phrases: chaining phrases into clusters once joined 913 accounts into one
    "team". In the top lists a phrase whose text overlaps a higher one by
    OVERLAP_WORDS words is shown as that one's variant, counts unmerged;
  * coverage: the share of a post's words inside shared phrases. On 24 Oct
    65% of all posts had at least one shared phrase, so coverage, not the
    yes/no flag, tells template posts apart;
  * openings and frames: the first OPENING_WORDS words of each post, and the
    first FRAME_WORDS words of each sentence or clause;
  * fact copies: posts that repeat FACT_WORDS or more consecutive words of a
    fact bank claim (fact_bank's facts table);
  * per account the share of its posts with a shared phrase; for our roles
    those posts against post_visibility (hidden / suspect).

Read-only, for display and risk warnings only (D47): nothing here feeds
generation or writing, and it suggests no wording. CopyPatternMonitor (run by
risk_assessment.RiskMonitor) recomputes every REFRESH_MINUTES with a fresh
connection each round, held only while loading, and keeps the result in
memory; the endpoint and account_evidence only read that cache.

On 24 Oct (139k posts) one round took 20-35 s of CPU and peaked at 450 MB;
run inside the backend, about 200-300 MB stayed allocated afterwards. So the
monitor runs each round in a child Python process at below-normal priority
(refresh_isolated): the backend keeps its GIL, all that memory goes back when
the child exits, and only the JSON result (~180 KB) and the account index
come back. The database URL goes in on stdin, not on the command line.
"""
from __future__ import annotations

import heapq
import itertools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from .db import connect, resolve_database_target, utc_now

LOGGER = logging.getLogger(__name__)

WINDOW_HOURS = 48
GRAM_WORDS = 6
MIN_ACCOUNTS = 3
FACT_WORDS = 8
OPENING_WORDS = 4
FRAME_WORDS = 3
OVERLAP_WORDS = 4
REFRESH_MINUTES = 30
FIRST_RUN_SECONDS = 180
RETRY_SECONDS = 300
CHILD_TIMEOUT_SECONDS = 600
_ROOT = Path(__file__).resolve().parent.parent
TOP_PHRASES = 30
TOP_OUR_PHRASES = 15
TOP_LINES = 20
TOP_OUR_LINES = 10
TOP_FACTS = 15
TOP_ACCOUNTS = 40
TOP_NPC_ACCOUNTS = 10
MEMBERS = 20
OUR_POSTS = 100
ACCOUNT_PHRASES = 5
ACCOUNT_PEERS = 10
MIN_ACCOUNT_POSTS = 3
# Coverage bands for our hidden rates: share of a post's words inside shared phrases.
COVERAGE_BANDS = (0.25, 0.5, 0.75)
# Peers (account_evidence): accounts sharing phrases no calibrated NPC uses and that at most
# this many accounts use; wider phrases are common campaign lines, not a sign of one group.
PEER_MAX_ACCOUNTS = 25
EXCERPT_CHARS = 160
# Pre-filter for phrases that MIN_ACCOUNTS authors use: a saturating author count per hash slot.
FILTER_BITS = 24
# Accounts registered before any team are NPCs (vote_tracker, npc_calibration).
PRE_COMPETITION_MAX_ID = 2343
GROUPS = ('ours', 'teammates', 'others', 'npc')
FACT_STATUSES = ('active', 'superseded', 'retired')

_STRIP = re.compile(r'https?://\S+|www\.\S+|@\w+')
_DASH = re.compile(r'\s-+\s')
# Apostrophes go (don't -> dont); curly quotes and long dashes end a clause like '.'.
_APOSTROPHES = re.compile('[‘’`´]')
_UNICODE_BREAKS = re.compile('[“”–—]')
_WORD = re.compile(r'[a-z0-9]+')
_GAP = r'[^a-z0-9.!?;:,()\[\]"\n]'
_FRAME = re.compile(rf'(?:^|[.!?;:,()\[\]"\n]){_GAP}*([a-z0-9]+){_GAP}+([a-z0-9]+){_GAP}+([a-z0-9]+)')

_CACHE = {}
_LOCK = threading.Lock()


def cache_key(db_path):
    """One key per database however the caller spells it (Path, string or URL): the backend's
    RiskMonitor fills the cache and blue_view reads it."""
    return resolve_database_target(db_path)


def _key(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value


def normal(text):
    """Lower case without links, @mentions and apostrophes, accents folded; punctuation kept for clauses."""
    text = _STRIP.sub(' ', str(text or '').lower())
    if '-' in text:
        text = _DASH.sub('.', text)
    text = text.replace("'", '')
    if not text.isascii():
        text = _UNICODE_BREAKS.sub('.', _APOSTROPHES.sub('', text))
        if not text.isascii():
            text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return text


def words(text):
    """The normalised words of a text."""
    return _WORD.findall(normal(text))


def frames(normalised):
    """The first FRAME_WORDS words of each sentence or clause of a normal() text."""
    return [' '.join(frame) for frame in _FRAME.findall(normalised)]


def _indices():
    return array('i')


def _grams(found, size):
    """Hashes of every run of ``size`` consecutive words, in order."""
    if len(found) < size:
        return ()
    return map(hash, zip(*[found[index:] for index in range(size)]))


def _runs(grams, members):
    """[start, end] gram positions of each consecutive run of grams in ``members``."""
    runs = []
    for position, gram in enumerate(grams):
        if gram in members:
            if runs and runs[-1][1] == position - 1:
                runs[-1][1] = position
            else:
                runs.append([position, position])
    return runs


def _coverage(positions, grams):
    """Share of a post's words inside the shared grams at ``positions`` (ascending)."""
    covered, reach = 0, 0
    for position in positions:
        end = position + GRAM_WORDS
        if end > reach:
            covered += end - max(position, reach)
            reach = end
    return covered / (grams + GRAM_WORDS - 1) if grams else 0.0


def _shingles(text):
    found = text.split()
    return {' '.join(found[index:index + OVERLAP_WORDS]) for index in range(len(found) - OVERLAP_WORDS + 1)}


def _excerpt(body):
    text = ' '.join(str(body or '').split())
    return text[:EXCERPT_CHARS - 1] + '…' if len(text) > EXCERPT_CHARS else text


def _rate(part, whole):
    return round(part / whole, 4) if whole else None


def _load(db_path, since, until):
    """Everything the computation reads, on one connection that is closed before any work starts."""
    from .risk_assessment import all_roles
    conn = connect(db_path)
    try:
        roles = all_roles(conn)
        team = {row[0] for row in conn.execute('SELECT account_id FROM team_accounts')} - set(roles)
        posts = [tuple(row) for row in conn.execute(
            'SELECT post_id,author_id,kind,created_at,body FROM posts WHERE created_at>=? AND created_at<=? '
            "AND author_id IS NOT NULL AND body IS NOT NULL AND body<>'' ORDER BY author_id,post_id",
            (since, until))]
        names = {row[0]: row[1] for row in conn.execute(
            'SELECT account_id,username FROM accounts WHERE account_id IN '
            '(SELECT DISTINCT author_id FROM posts WHERE created_at>=? AND created_at<=?)', (since, until))}
        status = {row[0]: row[1] for row in conn.execute(
            "SELECT post_id,status FROM post_visibility WHERE status IN ('hidden','suspect')")}
        marks = ','.join('?' for _ in FACT_STATUSES)
        facts = [tuple(row) for row in conn.execute(
            f'SELECT id,status,claim FROM facts WHERE status IN ({marks}) ORDER BY id', FACT_STATUSES)]
    finally:
        conn.close()
    return roles, team, posts, names, status, facts


def _calibrated_npcs(db_path):
    from . import npc_calibration
    try:
        return set(npc_calibration.certain_npcs(db_path))
    except Exception:
        return set()


def _fact_grams(facts):
    """({FACT_WORDS-gram hash: fact ids}, GRAM_WORDS-gram hashes of every claim, for a quick pre-check)."""
    long, short = defaultdict(set), set()
    for fact_id, _, claim in facts:
        found = words(claim)
        for gram in _grams(found, FACT_WORDS):
            long[gram].add(fact_id)
        short.update(_grams(found, GRAM_WORDS))
    return long, short


class _Text:
    """One pass over the posts (sorted by author): grams, openings, frames, fact copies, shared grams.

    Memory matters here (the backend process keeps running): per-post data are
    arrays, openings and frames are keyed by hash (their text is read back from
    a post only for the rows shown), and author sets are built only for grams
    that MIN_ACCOUNTS authors use.
    """

    def __init__(self, posts, facts):
        mask, counts = (1 << FILTER_BITS) - 1, bytearray(1 << FILTER_BITS)
        slot = mask.__and__
        fact_long, fact_short = _fact_grams(facts)
        self.grams, self.ranges, self.fact_hits = [], [], {}
        # {hash: first post index, or an array of post indices once a second post has it}
        self.openings, self.frames = {}, {}

        def count(union):
            for place in map(slot, union):
                try:
                    counts[place] += 1
                except ValueError:  # saturated at 255
                    pass

        def add(table, key, index):
            before = table.get(key)
            if before is None:
                table[key] = index
            elif type(before) is int:
                table[key] = array('i', (before, index))
            else:
                before.append(index)

        author, start, union = None, 0, set()
        for index, (_, author_id, _, _, body) in enumerate(posts):
            if author_id != author:
                if author is not None:
                    count(union)
                    self.ranges.append((author, start, index))
                author, start, union = author_id, index, set()
            text = normal(body)
            found = _WORD.findall(text)
            grams = array('q', _grams(found, GRAM_WORDS))
            self.grams.append(grams)
            union.update(grams)
            if len(found) >= OPENING_WORDS:
                add(self.openings, hash(tuple(found[:OPENING_WORDS])), index)
            for frame in set(map(hash, _FRAME.findall(text))):
                add(self.frames, frame, index)
            if fact_short and not fact_short.isdisjoint(grams):
                ids = {fact_id for gram in _grams(found, FACT_WORDS) for fact_id in fact_long.get(gram, ())}
                if ids:
                    self.fact_hits[index] = sorted(ids)
        if author is not None:
            count(union)
            self.ranges.append((author, start, len(posts)))
        union = None

        # Exact author counts for the grams the filter let through (it only over-counts),
        # then author sets for the grams MIN_ACCOUNTS authors use.
        flags = counts.translate(bytes(int(value >= MIN_ACCOUNTS) for value in range(256)))
        counts, candidates, tally = None, [], Counter()
        for _, start, end in self.ranges:
            union = set(self.grams[start]).union(*self.grams[start + 1:end])
            found = array('q', itertools.compress(union, map(flags.__getitem__, map(slot, union))))
            candidates.append(found)
            tally.update(found)
        union = None
        qualified = {gram for gram, authors in tally.items() if authors >= MIN_ACCOUNTS}
        tally, authors_of = None, defaultdict(list)
        for (author, _, _), found in zip(self.ranges, candidates):
            for gram in itertools.compress(found, map(qualified.__contains__, found)):
                authors_of[gram].append(author)
        candidates = qualified = None
        # Sorted tuples, a third of a frozenset's size; equal author sets compare equal.
        self.shared = {gram: tuple(sorted(authors)) for gram, authors in authors_of.items()}
        authors_of = None
        self.posting, self.hits = defaultdict(_indices), {}
        self.coverage = array('f', bytes(4 * len(posts)))
        contains = self.shared.__contains__
        for index, grams in enumerate(self.grams):
            positions = list(itertools.compress(range(len(grams)), map(contains, grams)))
            if positions:
                hits = self.hits[index] = array('q', set(map(grams.__getitem__, positions)))
                for gram in hits:
                    self.posting[gram].append(index)
                self.coverage[index] = _coverage(positions, len(grams))

    def families(self):
        """[(grams, accounts)]: shared grams with the same accounts that follow each other in a post."""
        by_accounts = defaultdict(list)
        for gram, accounts in self.shared.items():
            by_accounts[accounts].append(gram)
        found = []
        for accounts, grams in by_accounts.items():
            pending = set(grams)
            while pending:
                gram = min(pending)
                pending.discard(gram)
                family = {gram}
                if pending:
                    sample = self.grams[self.posting[gram][0]]
                    for start, end in _runs(sample, pending | {gram}):
                        run = {sample[position] for position in range(start, end + 1)}
                        if gram in run:
                            family |= run
                            break
                    pending -= family
                found.append((frozenset(family) if len(family) > 1 else (gram,), accounts))
        return found


def compute(db_path, now=None, *, npc=None):
    """(the copy pattern view, the per-account index behind account_evidence). Read-only.

    ``npc`` replaces the calibrated NPC set (tests); accounts up to
    PRE_COMPETITION_MAX_ID always count as NPCs.
    """
    now = _now(now)
    since, until = _key(now - timedelta(hours=WINDOW_HOURS)), _key(now)
    clock = time.monotonic()
    roles, team, posts, names, status, facts = _load(db_path, since, until)
    load_seconds = time.monotonic() - clock
    calibrated = _calibrated_npcs(db_path) if npc is None else set(npc)
    text = _Text(posts, facts)
    authors = {author for author, _, _ in text.ranges}
    npcs = (calibrated | {author for author in authors if author <= PRE_COMPETITION_MAX_ID}) - set(roles) - team
    group_of = {author: 'ours' if author in roles else 'teammates' if author in team else
                'npc' if author in npcs else 'others' for author in authors}

    families = text.families()
    family_of = {gram: index for index, (grams, _) in enumerate(families) for gram in grams}
    # Post indices of each family, ascending (posting arrays are filled in index order).
    family_posts = [text.posting[next(iter(grams))] if len(grams) == 1 else
                    array('i', sorted(set().union(*(text.posting[gram] for gram in grams)))) for grams, _ in families]
    npc_free = [npcs.isdisjoint(accounts) for _, accounts in families]
    post_families = {index: array('i', {family_of[gram] for gram in hits}) for index, hits in text.hits.items()}
    author_of = [post[1] for post in posts]
    group_code = bytes(GROUPS.index(group_of[author]) for author in author_of)
    is_ours = bytes(code == 0 for code in group_code)

    def accounts_of(indices):
        return set(map(author_of.__getitem__, indices))

    shared_openings = {key for key, indices in text.openings.items()
                       if type(indices) is not int and len(indices) >= MIN_ACCOUNTS
                       and len(accounts_of(indices)) >= MIN_ACCOUNTS}

    def opening_text(_, index):
        return ' '.join(words(posts[index][4])[:OPENING_WORDS])

    def frame_text(key, index):
        return next((' '.join(frame) for frame in _FRAME.findall(normal(posts[index][4])) if hash(frame) == key), '')

    def breakdown(indices):
        posts_by = Counter(map(group_code.__getitem__, indices))
        accounts_by = Counter(map(group_of.__getitem__, accounts_of(indices)))
        return {group: {'posts': posts_by.get(code, 0), 'accounts': accounts_by.get(group, 0)}
                for code, group in enumerate(GROUPS)}

    def example(index):
        post_id, author, kind, created_at, body = posts[index]
        return {'post_id': post_id, 'account_id': author, 'username': names.get(author), 'group': group_of[author],
                'kind': kind, 'created_at': created_at, 'excerpt': _excerpt(body)}

    def latest_by_group(indices):
        latest = {}
        for index in indices:
            group = group_of[posts[index][1]]
            if group not in latest or posts[index][3] > posts[latest[group]][3]:
                latest[group] = index
        return latest

    post_words, phrase_text = {}, {}

    def phrase(family, index=None):
        """The family's longest run of words in post ``index`` (else in its first post)."""
        if index is None and family in phrase_text:
            return phrase_text[family]
        where = family_posts[family][0] if index is None else index
        if where not in post_words:
            post_words[where] = words(posts[where][4])
        runs = _runs(text.grams[where], families[family][0]) or [[0, 0]]
        start, end = max(runs, key=lambda run: (run[1] - run[0], -run[0]))
        value = ' '.join(post_words[where][start:end + GRAM_WORDS])
        if index is None:
            phrase_text[family] = value
        return value

    def distinct(order, limit, build):
        """Rows for families in ``order``; one overlapping a listed row's text counts as its variant."""
        rows, seen = [], []
        for family in order:
            if len(rows) >= limit:
                break
            row = build(family)
            shingles = _shingles(row['phrase'])
            owner = next((at for at, known in enumerate(seen) if known & shingles), None)
            if owner is None:
                rows.append({**row, 'variants': 0})
                seen.append(shingles)
            else:
                rows[owner]['variants'] += 1
        return rows

    def phrase_row(family):
        accounts, indices = families[family][1], family_posts[family]
        latest = latest_by_group(indices)
        chosen = [latest[group] for group in ('ours', 'npc', 'others', 'teammates') if group in latest][:2]
        uses = Counter(posts[index][1] for index in indices)
        members = sorted(accounts, key=lambda account: (-uses[account], account))
        return {'phrase': phrase(family, chosen[0]), 'grams': len(families[family][0]), 'posts': len(indices),
                'accounts': len(accounts), 'npc_free': npc_free[family], 'groups': breakdown(indices),
                'examples': [example(index) for index in chosen],
                'members': [{'account_id': account, 'username': names.get(account), 'group': group_of[account],
                             'posts': uses[account]} for account in members[:MEMBERS]],
                'members_total': len(accounts)}

    order = sorted(range(len(families)), key=lambda family: (-len(families[family][1]), -len(family_posts[family]),
                                                            family_posts[family][0]))
    phrases = distinct(order, TOP_PHRASES, phrase_row)

    # Accounts: posts with a shared phrase, with one no calibrated NPC uses, with a fact copy; coverage.
    stats, account_families = {}, {}
    for author, start, end in text.ranges:
        used = Counter()
        shared = no_npc = copied = 0
        covered = 0.0
        fact_ids = set()
        for index in range(start, end):
            found = post_families.get(index)
            if found:
                shared += 1
                no_npc += any(npc_free[family] for family in found)
                covered += text.coverage[index]
                used.update(found)
            if index in text.fact_hits:
                copied += 1
                fact_ids.update(text.fact_hits[index])
        count = end - start
        stats[author] = {'account_id': author, 'username': names.get(author), 'group': group_of[author],
                         'posts': count, 'shared_posts': shared, 'shared_no_npc_posts': no_npc,
                         'fact_posts': copied, 'share': round(shared / count, 3), 'coverage': round(covered / count, 3),
                         'phrases': len(used), 'fact_ids': sorted(fact_ids)}
        if used:
            account_families[author] = used
    by_group = {}
    for group in GROUPS:
        rows = [row for row in stats.values() if row['group'] == group]
        total, shared = sum(row['posts'] for row in rows), sum(row['shared_posts'] for row in rows)
        by_group[group] = {'accounts': len(rows), 'with_shared': sum(1 for row in rows if row['shared_posts']),
                           'posts': total, 'shared_posts': shared,
                           'shared_no_npc_posts': sum(row['shared_no_npc_posts'] for row in rows),
                           'fact_posts': sum(row['fact_posts'] for row in rows),
                           'share': round(shared / total, 3) if total else None,
                           'coverage': round(sum(row['coverage'] * row['posts'] for row in rows) / total, 3)
                           if total else None}

    def public(row):
        return {key: value for key, value in row.items() if key != 'fact_ids'}

    def top(group, limit):
        rows = [row for row in stats.values()
                if row['group'] == group and row['shared_posts'] and row['posts'] >= MIN_ACCOUNT_POSTS]
        rows.sort(key=lambda row: (-row['shared_posts'], -row['coverage'], row['account_id']))
        return [public(row) for row in rows[:limit]]

    teammates = sorted((public(row) for row in stats.values() if row['group'] == 'teammates'),
                       key=lambda row: (-row['coverage'], -row['posts'], row['account_id']))

    # Openings and frames: platform-wide ones (MIN_ACCOUNTS accounts or more) and ours.
    def lines(table, text_of):
        found = []
        for key, indices in table.items():
            if type(indices) is not int and len(indices) >= MIN_ACCOUNTS:
                found.append((key, indices, len(accounts_of(indices)), sum(map(is_ours.__getitem__, indices))))
        texts = {}

        def row(item):
            key, indices, accounts, _ = item
            if key not in texts:
                texts[key] = text_of(key, indices[0])
            return {'text': texts[key], 'posts': len(indices), 'accounts': accounts, 'groups': breakdown(indices)}

        shared = sorted((item for item in found if item[2] >= MIN_ACCOUNTS), key=lambda item: (-len(item[1]), -item[2], item[1][0]))
        ours = sorted((item for item in found if item[3]), key=lambda item: (-item[3], -len(item[1]), item[1][0]))
        return [row(item) for item in shared[:TOP_LINES]], [row(item) for item in ours[:TOP_OUR_LINES]]

    openings, our_openings = lines(text.openings, opening_text)
    frame_rows, our_frames = lines(text.frames, frame_text)

    by_fact = defaultdict(list)
    for index, fact_ids in text.fact_hits.items():
        for fact_id in fact_ids:
            by_fact[fact_id].append(index)
    claims = {fact_id: (state, claim) for fact_id, state, claim in facts}
    fact_rows = []
    for fact_id, indices in by_fact.items():
        latest = latest_by_group(indices)
        chosen = next(latest[group] for group in ('ours', 'npc', 'others', 'teammates') if group in latest)
        fact_rows.append({'fact_id': fact_id, 'status': claims[fact_id][0], 'claim': _excerpt(claims[fact_id][1]),
                          'posts': len(indices), 'accounts': len({posts[index][1] for index in indices}),
                          'groups': breakdown(indices), 'example': example(chosen)})
    fact_rows.sort(key=lambda row: (-row['posts'], row['fact_id']))

    # Our roles: template posts (a shared phrase or a fact copy) against post_visibility.
    splits = (('shared', 'template', '带共用短语或照抄事实'), ('shared', 'plain', '都不带'),
              ('coverage', 'c0', '共用短语占全文 < 25%'), ('coverage', 'c25', '25–50%'),
              ('coverage', 'c50', '50–75%'), ('coverage', 'c75', '≥ 75%'),
              ('fact', 'fact', '照抄事实原句'), ('fact', 'no_fact', '不照抄事实'),
              ('opening', 'shared_opening', f'开头 {OPENING_WORDS} 个词 ≥{MIN_ACCOUNTS} 个账号用过'),
              ('opening', 'own_opening', '开头不和别人相同'))
    tallies = {key: [0, 0, 0] for _, key, _ in splits}
    ours_rows, our_posts, ours_family_posts = [], [], Counter()
    for author, start, end in text.ranges:
        if group_of[author] != 'ours':
            continue
        row = stats[author]
        hidden = suspect = template = 0
        for index in range(start, end):
            post_id, _, kind, created_at, body = posts[index]
            state = status.get(post_id)
            found = post_families.get(index) or ()
            ours_family_posts.update(found)
            fact_ids = text.fact_hits.get(index) or []
            coverage = text.coverage[index]
            band = sum(coverage >= edge for edge in COVERAGE_BANDS)
            if index not in post_words:
                post_words[index] = words(body)
            opening = post_words[index][:OPENING_WORDS]
            shared_opening = len(opening) == OPENING_WORDS and hash(tuple(opening)) in shared_openings
            keys = ('template' if found or fact_ids else 'plain', ('c0', 'c25', 'c50', 'c75')[band],
                    'fact' if fact_ids else 'no_fact', 'shared_opening' if shared_opening else 'own_opening')
            for key in keys:
                tally = tallies[key]
                tally[0] += 1
                tally[1] += state == 'hidden'
                tally[2] += state == 'suspect'
            hidden += state == 'hidden'
            suspect += state == 'suspect'
            if found or fact_ids:
                template += 1
                best = min(found, key=lambda family: (-len(families[family][1]), family)) if found else None
                our_posts.append({'post_id': post_id, 'account_id': author, 'name': roles[author]['name'], 'kind': kind,
                                  'created_at': created_at, 'status': state, 'coverage': round(coverage, 3),
                                  'phrase': phrase(best, index) if best is not None else None,
                                  'phrase_accounts': len(families[best][1]) if best is not None else None,
                                  'phrases': len(found), 'fact_ids': fact_ids, 'excerpt': _excerpt(body)})
        ours_rows.append({**public(row), 'name': roles[author]['name'], 'role_state': roles[author]['state'],
                          'template_posts': template, 'template_share': round(template / row['posts'], 3),
                          'hidden': hidden, 'suspect': suspect})
    ours_rows.sort(key=lambda row: (-row['coverage'], -row['posts'], row['account_id']))
    rank = {'hidden': 0, 'suspect': 1}
    our_posts.sort(key=lambda row: (row['created_at'] or '', row['post_id']), reverse=True)
    our_posts.sort(key=lambda row: rank.get(row['status'], 2))
    hidden_rates = [{'split': split, 'key': key, 'label': label, 'posts': tallies[key][0], 'hidden': tallies[key][1],
                     'suspect': tallies[key][2], 'hidden_rate': _rate(tallies[key][1], tallies[key][0]),
                     'flagged_rate': _rate(tallies[key][1] + tallies[key][2], tallies[key][0])}
                    for split, key, label in splits]

    def our_phrase_row(family):
        indices = family_posts[family]
        return {'phrase': phrase(family, latest_by_group(indices).get('ours')), 'posts': len(indices),
                'accounts': len(families[family][1]), 'npc_free': npc_free[family], 'groups': breakdown(indices)}

    our_order = sorted(ours_family_posts, key=lambda family: (-ours_family_posts[family], -len(families[family][1]),
                                                              family))
    our_phrases = distinct(our_order, TOP_OUR_PHRASES, our_phrase_row)

    # The per-account index behind account_evidence (kept in memory, not served).
    index = {'generated_at': _key(now), 'stats': stats, 'families': {}, 'accounts': {}, 'peers': {}}
    for author, used in account_families.items():
        def rank(family):
            return not npc_free[family], -used[family], len(families[family][1]), family

        # The first few by rank are almost always enough for ACCOUNT_PHRASES distinct ones.
        preferred = heapq.nsmallest(ACCOUNT_PHRASES * 6, used, key=rank)
        if len(preferred) < len(used):
            preferred = itertools.chain(preferred, sorted(set(used) - set(preferred), key=rank))
        chosen, seen = [], set()
        for family in preferred:
            shingles = _shingles(phrase(family))
            if shingles & seen:
                continue
            seen |= shingles
            chosen.append((family, used[family]))
            if len(chosen) >= ACCOUNT_PHRASES:
                break
        index['accounts'][author] = chosen
        # Peers: the accounts sharing the most narrow NPC-free phrases with it, each phrase counted on its own.
        together = Counter()
        for family in used:
            if npc_free[family] and len(families[family][1]) <= PEER_MAX_ACCOUNTS:
                together.update(families[family][1])
        together.pop(author, None)
        if together:
            index['peers'][author] = heapq.nsmallest(ACCOUNT_PEERS, together.items(), key=lambda item: (-item[1], item[0]))
    for family in {family for found in index['accounts'].values() for family, _ in found}:
        index['families'][family] = (phrase(family), len(families[family][1]), npc_free[family])

    shared_posts = len(text.hits)
    result = {
        'status': 'ready',
        'generated_at': _key(now),
        'window': {'hours': WINDOW_HOURS, 'from': since, 'to': until},
        'rules': {'gram_words': GRAM_WORDS, 'min_accounts': MIN_ACCOUNTS, 'fact_words': FACT_WORDS,
                  'opening_words': OPENING_WORDS, 'frame_words': FRAME_WORDS, 'overlap_words': OVERLAP_WORDS,
                  'refresh_minutes': REFRESH_MINUTES, 'pre_competition_max_id': PRE_COMPETITION_MAX_ID,
                  'peer_max_accounts': PEER_MAX_ACCOUNTS},
        'posts': len(posts), 'accounts': len(authors),
        'shared': {'grams': len(text.shared), 'phrases': len(families), 'posts': shared_posts,
                   'share': round(shared_posts / len(posts), 3) if posts else None,
                   'npc_free_phrases': sum(npc_free)},
        'by_group': by_group,
        'phrases': phrases,
        'openings': openings, 'frames': frame_rows,
        'facts': {'facts': len(facts), 'posts': len(text.fact_hits), 'groups': breakdown(text.fact_hits),
                  'top': fact_rows[:TOP_FACTS], 'facts_copied': len(fact_rows)},
        'accounts_view': {'teammates': teammates, 'npc': {**by_group['npc'], 'top': top('npc', TOP_NPC_ACCOUNTS)},
                          'others': {**by_group['others'], 'top': top('others', TOP_ACCOUNTS)}},
        'ours': {'totals': {**by_group['ours'], 'template_posts': tallies['template'][0]},
                 'roles': ours_rows, 'hidden_rates': hidden_rates, 'phrases': our_phrases,
                 'openings': our_openings, 'frames': our_frames,
                 'posts': our_posts[:OUR_POSTS], 'posts_total': len(our_posts)},
        'run': {'load_seconds': round(load_seconds, 2)},
        'caveats': [
            f'共用短语：文本归一化（小写，去掉链接、@、撇号和标点）后连续 {GRAM_WORDS} 个词，被 ≥{MIN_ACCOUNTS} 个不同账号用过。'
            '账号按短语分组，不跨短语合并；列表里和上面某条有 '
            f'{OVERLAP_WORDS} 个词重叠的短语算作它的相近片段，只计数、不合并账号',
            '覆盖率：帖子里落在共用短语里的词占全文的比例。全平台大多数帖子都至少带一条共用短语，所以看覆盖率比看「带不带」更能区分模板帖',
            f'已校准 NPC：官方小时数据证实的 NPC（npc_calibration），加上比赛开始前注册的账号（id ≤ {PRE_COMPETITION_MAX_ID}）。'
            '「别队」是我方、队友、已校准 NPC 以外的所有账号，里面也有没校准到的 NPC',
            f'照抄事实：连续 ≥{FACT_WORDS} 个词和事实库原文相同（在用、已取代、已退役的事实）',
            f'隐藏率只看近 {WINDOW_HOURS} 小时我方角色的帖子，隐藏和待复核来自 post_visibility；样本小，只能看出相关，不能证明原因',
            '只做识别和风险提示，不接进生成或写作流程（D47）'],
    }
    return result, index


def refresh(db_path, now=None, *, npc=None):
    """Recompute and replace the cached view for ``db_path``; returns the view."""
    wall, cpu = time.monotonic(), time.thread_time()
    result, index = compute(db_path, now, npc=npc)
    result['run'].update(cpu_seconds=round(time.thread_time() - cpu, 2), wall_seconds=round(time.monotonic() - wall, 2),
                         finished_at=utc_now())
    with _LOCK:
        _CACHE[cache_key(db_path)] = {'result': result, 'index': index, 'last_error': None}
    return result


def _restore(index):
    """The account index after a JSON round trip: integer keys again."""
    def ints(table):
        return {int(key): value for key, value in table.items()}
    return {'generated_at': index['generated_at'], **{key: ints(index[key]) for key in
                                                      ('stats', 'families', 'accounts', 'peers')}}


def refresh_isolated(db_path, *, started=None, timeout=CHILD_TIMEOUT_SECONDS):
    """refresh() in a child Python process at below-normal priority; returns the view.

    The backend keeps its GIL and gets back all the memory of a round (see the
    module docstring). ``started`` receives the process (CopyPatternMonitor.stop kills it).
    """
    wall = time.monotonic()
    options = ({'creationflags': subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW}
               if os.name == 'nt' else {'preexec_fn': lambda: os.nice(10)})
    proc = subprocess.Popen([sys.executable, '-m', 'ctn_data.copy_patterns'], cwd=str(_ROOT), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    if started is not None:
        started(proc)
    try:
        out, err = proc.communicate((str(db_path) + '\n').encode('utf-8'), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f'文案识别子进程超过 {timeout} 秒，已终止') from None
    if proc.returncode:
        detail = err.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(f'文案识别子进程失败（{proc.returncode}）：{detail[-1] if detail else ""}'[:300])
    cpu = time.thread_time()
    payload = json.loads(out.decode('utf-8'))
    result, index = payload['result'], _restore(payload['index'])
    result['run'].update(parse_cpu_seconds=round(time.thread_time() - cpu, 2), isolated=True,
                         wall_seconds=round(time.monotonic() - wall, 2), finished_at=utc_now())
    with _LOCK:
        _CACHE[cache_key(db_path)] = {'result': result, 'index': index, 'last_error': None}
    return result


def _child():
    """python -m ctn_data.copy_patterns: database URL on stdin, {'result', 'index'} as UTF-8 JSON on stdout."""
    db_path = sys.stdin.readline().strip()
    cpu = time.process_time()
    result, index = compute(db_path)
    result['run']['cpu_seconds'] = round(time.process_time() - cpu, 2)
    sys.stdout.buffer.write(json.dumps({'result': result, 'index': index}, ensure_ascii=False,
                                       separators=(',', ':')).encode('utf-8'))
    sys.stdout.flush()


def _failed(db_path, exc):
    with _LOCK:
        entry = _CACHE.setdefault(cache_key(db_path), {})
        entry['last_error'] = {'at': utc_now(), 'error': f'{type(exc).__name__}: {exc}'[:300]}


def cached(db_path):
    """The last computed view (``status`` 'pending' before the first), with the last error if any."""
    with _LOCK:
        entry = dict(_CACHE.get(cache_key(db_path)) or {})
    view = dict(entry.get('result') or {'status': 'pending', 'rules': {'refresh_minutes': REFRESH_MINUTES}})
    view['last_error'] = entry.get('last_error')
    return view


def account_evidence(db_path, account_ids, *, phrases=ACCOUNT_PHRASES, peers=ACCOUNT_PEERS):
    """From the cache only: each account's shared wording, for the Blue account cards.

    Returns {account_id: {'template_share', 'phrases', 'peers', 'copied_facts', ...}} for the
    requested accounts that posted in the window, {} before the first computation:
      * template_share: share (0-1) of its posts with a shared phrase; coverage: mean share of
        its posts' words inside shared phrases;
      * phrases: up to ``phrases`` of its shared phrases, [{'phrase', 'accounts', 'npc_free',
        'posts'}], phrases no calibrated NPC uses first, then the ones it used most, no two
        overlapping by OVERLAP_WORDS words;
      * peers: up to ``peers`` (at most ACCOUNT_PEERS) account ids sharing with it the most
        phrases that no calibrated NPC uses and at most PEER_MAX_ACCOUNTS accounts use
        (peer_phrases: {id: count}); each phrase is its own group, nothing is merged transitively;
      * copied_facts: its posts that copy a fact claim (fact_ids: which facts);
      * also account_id, username, group, posts, shared_posts, shared_no_npc_posts, generated_at.
    Never touches the database.
    """
    with _LOCK:
        index = (_CACHE.get(cache_key(db_path)) or {}).get('index')
    if not index:
        return {}
    stats, families = index['stats'], index['families']
    found = {}
    for account_id in account_ids:
        row = stats.get(account_id) if type(account_id) is int else None
        if row is None:
            continue
        together = dict(index['peers'].get(account_id, ())[:peers])
        ranked = list(together)
        found[account_id] = {
            'account_id': account_id, 'username': row['username'], 'group': row['group'], 'posts': row['posts'],
            'shared_posts': row['shared_posts'], 'shared_no_npc_posts': row['shared_no_npc_posts'],
            'template_share': row['share'], 'coverage': row['coverage'],
            'copied_facts': row['fact_posts'], 'fact_ids': list(row['fact_ids']),
            'phrases': [{'phrase': families[family][0], 'accounts': families[family][1],
                         'npc_free': families[family][2], 'posts': uses}
                        for family, uses in index['accounts'].get(account_id, ())[:phrases]],
            'peers': ranked, 'peer_phrases': {other: together[other] for other in ranked},
            'generated_at': index['generated_at']}
    return found


class CopyPatternMonitor:
    """One background thread: a round every REFRESH_MINUTES, in a child process unless ``isolated`` is False."""

    def __init__(self, db_path, *, first_run_seconds=FIRST_RUN_SECONDS, interval_seconds=REFRESH_MINUTES * 60,
                 retry_seconds=RETRY_SECONDS, isolated=True):
        self.db_path, self.first_run_seconds, self.isolated = db_path, first_run_seconds, isolated
        self.interval_seconds, self.retry_seconds = interval_seconds, retry_seconds
        self._stop = threading.Event()
        self._thread = None
        self._proc = None

    def _started(self, proc):
        self._proc = proc

    def _run(self):
        wait = self.first_run_seconds
        while not self._stop.wait(wait):
            try:
                result = refresh_isolated(self.db_path, started=self._started) if self.isolated else refresh(self.db_path)
                LOGGER.info('Copy patterns: %s posts, %s s CPU', result['posts'], result['run']['cpu_seconds'])
                wait = self.interval_seconds
            except Exception as exc:
                if self._stop.is_set():
                    break
                LOGGER.error('Copy pattern refresh failed (%s); retrying in %s s', type(exc).__name__, self.retry_seconds)
                _failed(self.db_path, exc)
                wait = self.retry_seconds
            finally:
                self._proc = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-copy-patterns', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=30)


if __name__ == '__main__':
    _child()
