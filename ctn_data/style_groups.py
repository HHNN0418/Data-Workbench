"""Style groups (文风分组): accounts whose posts read as written by one generator, over 48 hours.

Method d1L of reports/generated/style-detection-research-20261024.md (§3, §6.1),
the variant the user picked, with numpy and scipy:

  * universe: accounts with MIN_TEXTS or more non-repost texts in the window;
    an account's document is its newest TEXTS_PER_ACCOUNT texts;
  * text: NFKC, links become '↗' and @mentions '@', whitespace collapsed, case
    and punctuation kept, one space padded on each side;
  * features: the character 3-, 4- and 5-grams of each text (never across two
    texts), hashed into 2**HASH_BITS slots by a fixed integer hash (FNV-1a and
    the splitmix64 finaliser; Python's hash() changes per process and moved the
    research threshold between 0.161 and 0.172), sublinear TF x smoothed IDF,
    L2-normalised per account;
  * NPC labels: accounts registered before any team (id <= PRE_COMPETITION_MAX_ID)
    and npc_calibration.certain_npcs, minus our roles and team accounts. A
    fixed-seed hash of the account id splits them into halves: background B
    (the centroid and the local contrast; B accounts with BACKGROUND_MIN_TEXTS
    texts join the centroid) and calibration C (the edge threshold);
  * similarity: cosine after subtracting the B centroid, then the local NPC
    contrast s' = s - max(m_u, m_v), m_u the mean of u's LOCAL_K highest
    similarities to B accounts (missing ones count 0);
  * threshold theta: the largest s' over C x C pairs, recalibrated every round;
  * groups: an edge joins two cores (accounts with CORE_MIN or more others above
    theta) that are in each other's top MUTUAL_K and above theta; a group is a
    connected component of MIN_GROUP or more accounts, its id 'S' + the smallest
    listed account id.

Output, for spotting other teams' coordinated accounts on the Blue and risk
pages only (D47): verified accounts, our roles (meta agent_control_v1:agents),
team_accounts and NPC-labelled accounts stay in the graph but are never listed.
Safety rule: every listed member of a group that also holds one of our roles or
team accounts gets possible_teammate=True with TEAMMATE_NOTE, so Blue does not
take a probable unregistered teammate for a report target. team_accounts is
never changed here.

Read-only. StyleGroupMonitor recomputes every REFRESH_MINUTES, by default in a
child Python process at below-normal priority with BLAS_THREADS BLAS threads:
the child connects, reads, closes the connection, computes and exits, so the
backend keeps its GIL and gets all the memory back. The result stays in this
module's memory; account_evidence and cached() only read it. A failed round
keeps the previous result and records the error.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import zlib
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect, iter_rows, utc_now

LOGGER = logging.getLogger(__name__)

WINDOW_HOURS = 48
MIN_TEXTS = 5
BACKGROUND_MIN_TEXTS = 3
TEXTS_PER_ACCOUNT = 40
GRAM_SIZES = (3, 4, 5)
HASH_BITS = 21
LOCAL_K = 3
MUTUAL_K = 20
CORE_MIN = 3
MIN_GROUP = 3
# Accounts registered before any team are NPCs (vote_tracker, npc_calibration, copy_patterns).
PRE_COMPETITION_MAX_ID = 2343
SPLIT_SEED = 20261024
# A max-of-null threshold needs a large calibration half (report §8.1: >= 250 accounts); below
# CALIBRATION_FLOOR there is no usable threshold, the round fails and the previous result stays.
MIN_CALIBRATION_ACCOUNTS = 250
CALIBRATION_FLOOR = 30
# A threshold this low means the calibration NPCs barely resemble each other: worth a look.
THETA_FLOOR = 0.08
PEERS = 10
EXAMPLES = 2
GROUP_MEMBERS = 50
EXCERPT_CHARS = 160
REFRESH_MINUTES = 60
# copy_patterns runs 3 minutes after start and every 30 minutes; 10 minutes keeps the two apart.
FIRST_RUN_SECONDS = 600
RETRY_SECONDS = 600
# account_evidence stops serving a result this old (three missed rounds).
EXPIRE_MINUTES = 180
CHILD_TIMEOUT_SECONDS = 1200
BLAS_THREADS = 2
# The account x account product: features at least this share of accounts use go through
# dense BLAS blocks of about DENSE_BLOCK_BYTES, the rest through a sparse product.
DENSE_SHARE = 1 / 16
DENSE_MIN_DF = 64
DENSE_BLOCK_BYTES = 64 * 2 ** 20
ROW_BLOCK = 512
TEAMMATE_NOTE = '与我方文风同组，可能是未登记的队友'
_ROOT = Path(__file__).resolve().parent.parent

_URL = re.compile(r'https?://\S+|www\.\S+')
_MENTION = re.compile(r'@\w[\w.]*')
_FNV_OFFSET = 0xcbf29ce484222325
_FNV_PRIME = 0x100000001b3
_MIX = (0xbf58476d1ce4e5b9, 0x94d049bb133111eb)

_STATE = {}
_LOCK = threading.Lock()


def _key(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _chunks(values, size=500):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _excerpt(body):
    text = ' '.join(str(body or '').split())
    return text[:EXCERPT_CHARS - 1] + '…' if len(text) > EXCERPT_CHARS else text


def char_text(text):
    """A text as the n-grams see it: NFKC, links '↗', @mentions '@', single spaces, padded."""
    text = unicodedata.normalize('NFKC', str(text or ''))
    text = _MENTION.sub('@', _URL.sub('↗', text))
    return ' ' + ' '.join(text.split()) + ' '


def calibration_half(account_id):
    """0: background half (centroid, local contrast), 1: calibration half (threshold). Fixed per account."""
    return zlib.crc32(f'{SPLIT_SEED}:{account_id}'.encode()) & 1


def gram_slots(texts):
    """The hash slot of every character n-gram (GRAM_SIZES) of each text; n-grams never span two texts.

    Stable across processes and machines: FNV-1a over the code points, the
    n-gram length in the seed, the splitmix64 finaliser, the top HASH_BITS bits.
    """
    import numpy as np
    parts = [char_text(text) for text in texts]
    if not parts:
        return np.empty(0, dtype=np.int64)
    codes = np.frombuffer(''.join(parts).encode('utf-32-le'), dtype=np.uint32).astype(np.uint64)
    owner = np.repeat(np.arange(len(parts), dtype=np.int32), [len(part) for part in parts])
    prime, mix1, mix2 = np.uint64(_FNV_PRIME), np.uint64(_MIX[0]), np.uint64(_MIX[1])
    found = []
    for size in GRAM_SIZES:
        count = len(codes) - size + 1
        if count <= 0:
            continue
        value = np.full(count, _FNV_OFFSET ^ size, dtype=np.uint64)
        for offset in range(size):
            value ^= codes[offset:offset + count]
            value *= prime
        value = value[owner[:count] == owner[size - 1:size - 1 + count]]
        value ^= value >> np.uint64(30)
        value *= mix1
        value ^= value >> np.uint64(27)
        value *= mix2
        value ^= value >> np.uint64(31)
        found.append((value >> np.uint64(64 - HASH_BITS)).astype(np.int64))
    return np.concatenate(found) if found else np.empty(0, dtype=np.int64)


@contextmanager
def _snapshot(conn):
    """On PostgreSQL one read-only snapshot, unless the caller already holds a transaction."""
    if getattr(conn, 'dialect', None) != 'postgresql' or conn.in_transaction:
        yield
        return
    conn.raw.execute('BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ')
    try:
        yield
    finally:
        conn.raw.rollback()


def load(conn, now=None, *, npc=None):
    """Everything a round reads (read-only): window texts, our accounts, names, verified, hidden, NPC labels.

    ``npc`` replaces npc_calibration.certain_npcs (tests); accounts up to
    PRE_COMPETITION_MAX_ID always count as NPCs.
    """
    from . import npc_calibration
    from .risk_assessment import all_roles
    now = _now(now)
    since, until = _key(now - timedelta(hours=WINDOW_HOURS)), _key(now)
    clock = time.monotonic()
    texts = defaultdict(list)
    with _snapshot(conn):
        with iter_rows(conn, 'SELECT post_id,author_id,body FROM posts WHERE ctn_timestamp(created_at)>=? '
                             'AND ctn_timestamp(created_at)<=? AND author_id IS NOT NULL AND body IS NOT NULL '
                             'AND (kind IS NULL OR kind<>?)', (since, until, 'repost'), batch_size=5000) as rows:
            for post_id, author_id, body in rows:
                if body.strip():
                    texts[author_id].append((post_id, body))
        roles = set(all_roles(conn))
        team = {row[0] for row in conn.execute('SELECT account_id FROM team_accounts')}
        own = set(npc_calibration.team_accounts(conn))
        usernames, verified = {}, {}
        for chunk in _chunks(sorted(texts)):
            marks = ','.join('?' for _ in chunk)
            for account_id, username, flag in conn.execute(
                    f'SELECT account_id,username,verified FROM accounts WHERE account_id IN ({marks})', tuple(chunk)):
                usernames[account_id] = username
                verified[account_id] = bool(flag)
        hidden = {row[0]: int(row[1]) for row in conn.execute(
            "SELECT account_id,COUNT(*) FROM post_visibility WHERE status='hidden' GROUP BY account_id")}
        # Last: certain_npcs swallows its own errors, which would abort a PostgreSQL snapshot.
        calibrated = set(npc_calibration.certain_npcs(conn)) if npc is None else set(npc)
    return {'generated_at': _key(now), 'since': since, 'until': until, 'texts': dict(texts), 'roles': roles,
            'team': team, 'own': own, 'usernames': usernames, 'verified': verified, 'hidden': hidden,
            'calibrated': calibrated, 'load_seconds': round(time.monotonic() - clock, 2)}


class _Stages:
    def __init__(self):
        self.times, self._mark = {}, (time.perf_counter(), time.process_time())

    def __call__(self, name):
        wall, cpu = time.perf_counter(), time.process_time()
        self.times[name] = [round(wall - self._mark[0], 2), round(cpu - self._mark[1], 2)]
        self._mark = (wall, cpu)


def _account_matrix(documents, np):
    """CSR parts (indptr, slot indices, raw counts) of the hashed n-gram counts, one row per document."""
    indptr, slots, counts = [0], [], []
    for texts in documents:
        found, freq = np.unique(gram_slots(texts), return_counts=True)
        slots.append(found.astype(np.int32))
        counts.append(freq.astype(np.float32))
        indptr.append(indptr[-1] + len(found))
    return (np.asarray(indptr, dtype=np.int64), np.concatenate(slots) if slots else np.empty(0, np.int32),
            np.concatenate(counts) if counts else np.empty(0, np.float32))


def _row_sums(owner, weights, rows, np, step=1 << 22):
    """Per-row sums of ``weights(start, end)`` over entry chunks, so no float64 copy of every entry is made."""
    total = np.zeros(rows)
    for start in range(0, len(owner), step):
        end = min(start + step, len(owner))
        total += np.bincount(owner[start:end], weights=weights(start, end), minlength=rows)
    return total


def _shared_columns(parts, n, np, sp):
    """The universe's entries as a CSC matrix of the features two or more accounts use, frequent ones first.

    ``parts`` (a dict with 'indices', 'values', 'owner') is emptied as soon as
    the matrix exists. A feature only one account uses adds nothing off the
    diagonal of X Xᵀ, so it is dropped.
    """
    indices = parts.pop('indices')
    df = np.bincount(indices, minlength=1 << HASH_BITS)
    cut = max(DENSE_MIN_DF, int(n * DENSE_SHARE))
    dense, light = np.flatnonzero(df >= cut), np.flatnonzero((df >= 2) & (df < cut))
    remap = np.full(1 << HASH_BITS, -1, dtype=np.int32)
    remap[dense] = np.arange(len(dense), dtype=np.int32)
    remap[light] = len(dense) + np.arange(len(light), dtype=np.int32)
    column = remap[indices]
    del indices, remap, df
    keep = column >= 0
    values, owner = parts.pop('values')[keep], parts.pop('owner')[keep]
    matrix = sp.csc_matrix((values, (owner, column[keep])), shape=(n, len(dense) + len(light)))
    return matrix, {'dense_features': int(len(dense)), 'light_features': int(len(light)), 'dense_cut': cut,
                    'shared_nonzero': int(matrix.nnz)}


def _gram(matrix, dense, n, np, blas):
    """X Xᵀ (n x n float32) from _shared_columns: the first ``dense`` columns by BLAS blocks, the rest sparse.

    The diagonal is left for the caller (rows are unit length).
    """
    total = np.zeros((n, n), dtype=np.float32, order='F')
    step = max(256, int(DENSE_BLOCK_BYTES // (4 * max(n, 1))))
    for start in range(0, dense, step):
        block = matrix[:, start:min(start + step, dense)].toarray()
        # block.T is Fortran-ordered: trans=1 adds block @ block.T to the upper triangle without a copy.
        total = blas.ssyrk(1.0, block.T, beta=1.0, c=total, trans=1, overwrite_c=1)
        block = None
    gram = total + total.T  # the lower triangle of ``total`` is still zero; the diagonal is reset later
    total = None
    if matrix.shape[1] > dense:
        part = matrix[:, dense:]
        rows, columns = part.tocsr(), part.T.tocsr()
        part = None
        for start in range(0, n, ROW_BLOCK):
            gram[start:start + ROW_BLOCK] += (rows[start:start + ROW_BLOCK] @ columns).toarray()
    return gram


def analyse(data, *, holdout=(), _debug=None):
    """(the summary view, the per-account index behind account_evidence) from load()'s data.

    ``holdout``: NPC-labelled accounts to leave out of the NPC halves and count
    in groups instead (the research's held-out NPC check; empty in production).
    ``_debug`` (tests): a dict that receives the universe, the contrasted
    similarity matrix and the calibration rows.
    """
    import numpy as np
    import scipy.sparse as sp
    from scipy.linalg import blas
    from scipy.sparse.csgraph import connected_components
    stages, warnings = _Stages(), []
    texts, usernames, verified, hidden = data['texts'], data['usernames'], data['verified'], data['hidden']
    roles, team, own = set(data['roles']), set(data['team']), set(data['own'])
    # 'ours': our roles and the fixed own accounts; 'team': the other team_accounts rows (teammates).
    ours = roles | (own - team)
    team_only = team - roles
    ours_all = roles | own | team
    holdout = set(holdout)
    counts = {author: len(rows) for author, rows in texts.items()}
    labelled = ({author for author in texts if author <= PRE_COMPETITION_MAX_ID} | set(data['calibrated'])) - ours_all
    npcs = labelled - holdout
    background = {author for author in npcs if calibration_half(author) == 0}
    calibration = npcs - background
    universe = sorted(author for author, count in counts.items() if count >= MIN_TEXTS)
    n = len(universe)
    present = set(universe)
    extra = sorted(author for author in background
                   if author not in present and counts.get(author, 0) >= BACKGROUND_MIN_TEXTS)
    cal_idx = np.array([i for i, author in enumerate(universe) if author in calibration], dtype=np.int64)
    bg_idx = np.array([i for i, author in enumerate(universe) if author in background], dtype=np.int64)
    if len(cal_idx) < max(CALIBRATION_FLOOR, 2):
        raise ValueError(f'only {len(cal_idx)} calibration NPC accounts in the window (need {CALIBRATION_FLOOR})')
    if len(cal_idx) < MIN_CALIBRATION_ACCOUNTS:
        warnings.append(f'校准 NPC 只有 {len(cal_idx)} 个账号（建议 ≥{MIN_CALIBRATION_ACCOUNTS}），阈值可能偏低')
    newest = {author: sorted(texts[author], key=lambda row: -row[0])[:TEXTS_PER_ACCOUNT] for author in universe + extra}
    stages('prepare')

    # 1. Hashed n-gram counts; sublinear TF x smoothed IDF over every row; unit rows.
    rows_all = universe + extra
    indptr, indices, values = _account_matrix(([body for _, body in newest[author]] for author in rows_all), np)
    total_rows = len(rows_all)
    owner = np.repeat(np.arange(total_rows, dtype=np.int32), np.diff(indptr))
    stages('features')
    values = 1 + np.log(values)
    df = np.bincount(indices, minlength=1 << HASH_BITS)
    values *= (np.log((total_rows + 1) / (df + 1)) + 1).astype(np.float32)[indices]
    del df
    norms = np.sqrt(_row_sums(owner, lambda start, end: np.square(values[start:end]), total_rows, np))
    values /= np.maximum(norms, 1e-12).astype(np.float32)[owner]
    # 2. Background centroid (B accounts in the universe and B accounts with fewer texts).
    is_bg = np.zeros(total_rows, dtype=bool)
    is_bg[np.array([i for i, author in enumerate(rows_all) if author in background], dtype=np.int64)] = True
    chosen = is_bg[owner]
    centroid = np.bincount(indices[chosen], weights=values[chosen], minlength=1 << HASH_BITS) / max(int(is_bg.sum()), 1)
    del chosen
    end = int(indptr[n])
    indices, values, owner = indices[:end], values[:end], owner[:end]
    toward = _row_sums(owner, lambda start, end: values[start:end] * centroid[indices[start:end]], n, np)
    centre = float(centroid @ centroid)
    del centroid
    features_nonzero = end
    stages('tfidf_centroid')

    # 3. Cosine after subtracting the centroid.
    parts = {'indices': indices, 'values': values, 'owner': owner}
    del indices, values, owner
    matrix, split = _shared_columns(parts, n, np, sp)
    gram = _gram(matrix, split['dense_features'], n, np, blas)
    del matrix
    stages('gram')
    np.fill_diagonal(gram, 1.0)
    inverse = (1.0 / np.sqrt(np.maximum(1.0 - 2.0 * toward + centre, 1e-9))).astype(np.float32)
    toward = toward.astype(np.float32)
    gram -= toward[:, None]
    gram -= toward[None, :]
    gram += np.float32(centre)
    gram *= inverse[:, None]
    gram *= inverse[None, :]
    np.fill_diagonal(gram, -np.inf)
    # 4. Local NPC contrast.
    local = np.zeros(n, dtype=np.float32)
    if len(bg_idx):
        k = min(LOCAL_K, len(bg_idx))
        top = -np.partition(-gram[:, bg_idx], k - 1, axis=1)[:, :k]
        top[~np.isfinite(top)] = 0
        local = (top.sum(axis=1) / LOCAL_K).astype(np.float32)
        del top
    for start in range(0, n, ROW_BLOCK):
        gram[start:start + ROW_BLOCK] -= np.maximum(local[start:start + ROW_BLOCK, None], local[None, :])
    # 5. Threshold: the largest contrasted similarity of any calibration pair.
    upper = np.triu_indices(len(cal_idx), 1)
    theta = float(gram[np.ix_(cal_idx, cal_idx)][upper].max())
    if theta <= THETA_FLOOR:
        warnings.append(f'阈值 θ={theta:.3f} ≤ {THETA_FLOOR}，校准 NPC 彼此几乎不相似，请核对')
    if _debug is not None:
        _debug.update(universe=list(universe), similarity=gram.copy(), calibration=cal_idx.tolist(),
                      background=bg_idx.tolist(), theta=theta)
    stages('contrast_threshold')

    # 6. Groups: mutual top-MUTUAL_K edges above theta between cores, components of MIN_GROUP or more.
    above = gram > theta
    degree = above.sum(axis=1)
    cores = degree >= CORE_MIN
    k = min(MUTUAL_K, n - 1)
    top = np.empty((n, k), dtype=np.int64)
    for start in range(0, n, ROW_BLOCK):
        top[start:start + ROW_BLOCK] = np.argpartition(-gram[start:start + ROW_BLOCK], k - 1, axis=1)[:, :k]
    nearest = sp.csr_matrix((np.ones(n * k, dtype=np.int8), (np.repeat(np.arange(n), k), top.ravel())), shape=(n, n))
    mutual = nearest.multiply(nearest.T).tocoo()
    left, right = mutual.row, mutual.col
    keep = (left < right) & cores[left] & cores[right] & (gram[left, right] > theta)
    left, right = left[keep], right[keep]
    adjacency = sp.csr_matrix((np.ones(len(left), dtype=np.int8), (left, right)), shape=(n, n))
    _, component = connected_components(adjacency, directed=False)
    sizes = np.bincount(component)
    ours_idx = np.array([i for i, author in enumerate(universe) if author in ours_all], dtype=np.int64)
    linked_ours = above[:, ours_idx].sum(axis=1) if len(ours_idx) else np.zeros(n, dtype=np.int64)
    del above, nearest, mutual, adjacency, top
    stages('grouping')

    # 7. Listing: who is shown, the safety rule, peers and example posts.
    def excluded(author):
        if author in ours:
            return 'ours'
        if author in team_only:
            return 'team'
        if verified.get(author):
            return 'verified'
        if author in labelled:
            return 'npc'
        return None

    def representative(author):
        rows = sorted(newest[author], key=lambda row: (len(' '.join(row[1].split())), row[0]))
        post_id, body = rows[len(rows) // 2]
        return {'post_id': post_id, 'excerpt': _excerpt(body)}

    members_of = defaultdict(list)
    for i in np.flatnonzero(sizes[component] >= MIN_GROUP):
        members_of[int(component[i])].append(int(i))
    groups, possible, index_groups, index_accounts, posts = [], [], {}, {}, {}
    totals = {'ours': 0, 'team': 0, 'verified': 0, 'npc': 0}
    npc_in_groups = {'background': 0, 'calibration': 0}
    holdout_in_groups, groups_with_ours, grouped_accounts = 0, 0, 0
    for idx in members_of.values():
        members = [universe[i] for i in idx]
        grouped_accounts += len(members)
        reasons = {author: excluded(author) for author in members}
        excluded_counts = {name: sum(1 for value in reasons.values() if value == name) for name in totals}
        for name, count in excluded_counts.items():
            totals[name] += count
        npc_in_groups['background'] += sum(1 for author in members if author in background)
        npc_in_groups['calibration'] += sum(1 for author in members if author in calibration)
        holdout_in_groups += sum(1 for author in members if author in holdout)
        flagged = any(author in ours_all for author in members)
        groups_with_ours += flagged
        shown = [i for i in idx if reasons[universe[i]] is None]
        if not shown:
            continue
        inner = gram[np.ix_(shown, shown)]
        inner[~np.isfinite(inner)] = 0
        centrality = inner.sum(axis=1)
        order = [shown[j] for j in np.argsort(-centrality, kind='stable')]
        listed = [universe[i] for i in order]
        group_id = f'S{min(listed)}'
        hidden_accounts = sum(1 for author in listed if hidden.get(author))
        for author in listed:
            posts[author] = representative(author)
        examples = [{**posts[author], 'account_id': author, 'username': usernames.get(author)} for author in listed[:EXAMPLES]]
        mean = float(inner[np.triu_indices(len(shown), 1)].mean()) if len(shown) > 1 else None
        groups.append({'group_id': group_id, 'size': len(members), 'listed': len(listed),
                       'hidden_accounts': hidden_accounts, 'possible_teammate': flagged,
                       'teammate_note': TEAMMATE_NOTE if flagged else None, 'excluded': excluded_counts,
                       'mean_similarity': None if mean is None else round(mean, 4),
                       'members': [{'account_id': author, 'username': usernames.get(author),
                                    'hidden': hidden.get(author, 0)} for author in listed[:GROUP_MEMBERS]],
                       'examples': examples})
        index_groups[group_id] = {'size': len(members), 'listed': len(listed), 'hidden_accounts': hidden_accounts,
                                  'possible_teammate': flagged}
        for i in order:
            row = gram[i, order]
            peers = [order[j] for j in np.argsort(-row, kind='stable') if order[j] != i][:PEERS]
            index_accounts[universe[i]] = {'group': group_id, 'peers': [universe[j] for j in peers],
                                           'linked_to_ours': int(linked_ours[i])}
        if flagged:
            possible.extend({'account_id': author, 'username': usernames.get(author), 'group_id': group_id,
                             'group_size': len(members), 'ours_in_group': excluded_counts['ours'],
                             'team_in_group': excluded_counts['team'], 'hidden': hidden.get(author, 0)}
                            for author in listed)
    for i, author in enumerate(universe):
        if author not in index_accounts and excluded(author) is None:
            index_accounts[author] = {'group': None, 'peers': [], 'linked_to_ours': int(linked_ours[i])}
    groups.sort(key=lambda group: (-group['size'], group['group_id']))
    possible.sort(key=lambda row: (row['group_id'], row['account_id']))
    stages('listing')

    result = {
        'status': 'ready',
        'generated_at': data['generated_at'],
        'window': {'hours': WINDOW_HOURS, 'from': data['since'], 'to': data['until']},
        'method': 'd1L',
        'rules': {'min_texts': MIN_TEXTS, 'texts_per_account': TEXTS_PER_ACCOUNT, 'gram_sizes': list(GRAM_SIZES),
                  'hash_bits': HASH_BITS, 'local_k': LOCAL_K, 'mutual_k': MUTUAL_K, 'core_min': CORE_MIN,
                  'min_group': MIN_GROUP, 'pre_competition_max_id': PRE_COMPETITION_MAX_ID,
                  'refresh_minutes': REFRESH_MINUTES, 'expire_minutes': EXPIRE_MINUTES},
        'theta': round(theta, 5),
        'counts': {
            'texts': sum(counts.values()), 'authors': len(texts), 'accounts': n,
            'background': int(len(bg_idx)), 'background_extra': len(extra), 'calibration': int(len(cal_idx)),
            'npc_labelled': len(labelled), 'calibrated_npcs': len(data['calibrated']),
            'holdout': len(holdout & present), 'features_nonzero': features_nonzero, **split,
            'cores': int(cores.sum()), 'edges': int(len(left)),
            'groups': len(members_of), 'grouped_accounts': grouped_accounts,
            'largest_group': max((len(idx) for idx in members_of.values()), default=0),
            'listed_groups': len(groups), 'listed_accounts': sum(group['listed'] for group in groups),
            'groups_with_ours': groups_with_ours, 'possible_teammates': len(possible),
            'excluded_in_groups': totals, 'npc_in_groups': npc_in_groups, 'holdout_in_groups': holdout_in_groups},
        'groups': groups,
        'possible_teammates': possible,
        'warnings': warnings,
        'run': {'load_seconds': data.get('load_seconds'), 'stages_wall_cpu_seconds': stages.times},
        'caveats': [
            '文风组：账号最近 40 条帖子的字符片段（3–5 个字符）减去 NPC 背景后高度相似，像是同一个生成器写的；'
            '不等于已确认的队伍，只凭文风不能举报，需要行为等其他证据',
            '阈值取校准 NPC 账号对里的最大相似度，每轮重新校准；连边要求互为前 20 近邻，组内每人至少 3 条边，组至少 3 人',
            '已认证账号、我方角色、team_accounts 和已标为 NPC 的账号（赛前注册或官方校准）留在计算里，但不列出',
            f'组里有我方角色或 team_accounts 的账号时，组里其他账号都标为「{TEAMMATE_NOTE}」，不能当举报目标',
            '只用于识别别队的协同账号（D47）'],
    }
    index = {'generated_at': data['generated_at'], 'accounts': index_accounts, 'groups': index_groups,
             'posts': posts, 'usernames': {author: usernames.get(author) for author in posts}}
    return result, index


def compute(conn, now=None, *, npc=None, holdout=()):
    """(view, account index) of the last WINDOW_HOURS before ``now`` on ``conn``. Read-only; the cache is untouched."""
    return analyse(load(conn, now, npc=npc), holdout=holdout)


def _memory_mb():
    """(working set, peak working set) of this process in MB on Windows, else (None, None)."""
    if os.name != 'nt':
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        query = kernel32.K32GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if not query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None, None
        return round(counters.WorkingSetSize / 2 ** 20, 1), round(counters.PeakWorkingSetSize / 2 ** 20, 1)
    except Exception:
        return None, None


def _store(result, index):
    with _LOCK:
        _STATE.update(result=result, index=index, last_error=None)


def _failed(exc):
    with _LOCK:
        _STATE['last_error'] = {'at': utc_now(), 'error': f'{type(exc).__name__}: {exc}'[:300]}


def refresh(db_path, now=None, *, npc=None, holdout=()):
    """One round in this thread: a fresh connection for the reads only, then compute; replaces the cache."""
    wall, cpu = time.monotonic(), time.thread_time()
    conn = connect(db_path)
    try:
        data = load(conn, now, npc=npc)
    finally:
        conn.close()
    result, index = analyse(data, holdout=holdout)
    result['run'].update(cpu_seconds=round(time.thread_time() - cpu, 2), wall_seconds=round(time.monotonic() - wall, 2),
                         finished_at=utc_now(), isolated=False)
    _store(result, index)
    return result


def _restore(index):
    """The account index after a JSON round trip: integer account ids again."""
    def ints(table):
        return {int(key): value for key, value in table.items()}
    return {'generated_at': index['generated_at'], 'groups': index['groups'],
            **{key: ints(index[key]) for key in ('accounts', 'posts', 'usernames')}}


def refresh_isolated(db_path, *, started=None, timeout=CHILD_TIMEOUT_SECONDS):
    """refresh() in a child Python process at below-normal priority with BLAS_THREADS threads; returns the view.

    The database URL goes in on stdin, not on the command line. ``started``
    receives the process (StyleGroupMonitor.stop kills it).
    """
    wall = time.monotonic()
    env = {**os.environ, **{name: str(BLAS_THREADS) for name in
                            ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')}}
    options = ({'creationflags': subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW}
               if os.name == 'nt' else {'preexec_fn': lambda: os.nice(10)})
    proc = subprocess.Popen([sys.executable, '-m', 'ctn_data.style_groups'], cwd=str(_ROOT), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **options)
    if started is not None:
        started(proc)
    try:
        out, err = proc.communicate((str(db_path) + '\n').encode('utf-8'), timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f'文风分组子进程超过 {timeout} 秒，已终止') from None
    if proc.returncode:
        detail = err.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(f'文风分组子进程失败（{proc.returncode}）：{detail[-1] if detail else ""}'[:300])
    payload = json.loads(out.decode('utf-8'))
    result, index = payload['result'], _restore(payload['index'])
    result['run'].update(isolated=True, wall_seconds=round(time.monotonic() - wall, 2), finished_at=utc_now())
    _store(result, index)
    return result


def _child():
    """python -m ctn_data.style_groups: database URL on stdin, {'result', 'index'} as UTF-8 JSON on stdout."""
    db_path = sys.stdin.readline().strip()
    cpu = time.process_time()
    conn = connect(db_path)
    try:
        if getattr(conn, 'dialect', None) == 'postgresql':
            # This process only reads; its pooled session dies with it.
            conn.raw.execute('SET default_transaction_read_only = on')
        data = load(conn)
    finally:
        conn.close()
    result, index = analyse(data)
    current, peak = _memory_mb()
    result['run'].update(cpu_seconds=round(time.process_time() - cpu, 2), memory_mb=current, peak_memory_mb=peak,
                         blas_threads=os.environ.get('OPENBLAS_NUM_THREADS'))
    sys.stdout.buffer.write(json.dumps({'result': result, 'index': index}, ensure_ascii=False,
                                       separators=(',', ':')).encode('utf-8'))
    sys.stdout.flush()


def _expired(generated_at, now):
    try:
        return _now(now) - _now(generated_at) > timedelta(minutes=EXPIRE_MINUTES)
    except (TypeError, ValueError):
        return True


def cached(now=None):
    """The last view without the account index ('pending' before the first), its last error and whether it expired."""
    with _LOCK:
        result, error = _STATE.get('result'), _STATE.get('last_error')
    if result is None:
        return {'status': 'pending', 'rules': {'refresh_minutes': REFRESH_MINUTES}, 'last_error': error, 'stale': False}
    return {**result, 'last_error': error, 'stale': _expired(result.get('generated_at'), now)}


def account_evidence(account_ids, *, now=None):
    """From the cache only: each account's style group, for the Blue account cards (copy_evidence).

    Returns {account_id: {...}} for the requested accounts that were analysed
    (MIN_TEXTS texts in the window) and may be listed; {} before the first round
    and once the result is older than EXPIRE_MINUTES. Our roles, team_accounts,
    verified and NPC-labelled accounts never appear. Per account:
      * style_group: group id ('S' + smallest listed account id), None when in no group;
      * group_size: accounts in the group, the unlisted ones included (0 without a group);
        group_listed: those listed; group_hidden_accounts: listed members with a
        confirmed hidden post (post_visibility status 'hidden'), this account included;
      * peers: up to PEERS listed members of its group, most similar first;
      * examples: up to EXAMPLES representative posts of those peers,
        [{'post_id', 'account_id', 'username', 'excerpt'}];
      * possible_teammate: the group also holds one of our roles or team accounts;
        teammate_note: TEAMMATE_NOTE then, else None. Such an account is no report target;
      * linked_to_ours: our roles and team accounts above the threshold with it (any group);
      * account_id, username, generated_at, source 'style_groups'.
    No template_share or phrases: those come from copy_patterns.account_evidence.
    Never touches the database.
    """
    with _LOCK:
        index = _STATE.get('index')
    if not index or _expired(index.get('generated_at'), now):
        return {}
    accounts, groups, posts, usernames = index['accounts'], index['groups'], index['posts'], index['usernames']
    found = {}
    for account_id in account_ids:
        entry = accounts.get(account_id) if type(account_id) is int else None
        if entry is None:
            continue
        group = groups.get(entry['group']) if entry['group'] else None
        flagged = bool(group and group['possible_teammate'])
        found[account_id] = {
            'account_id': account_id, 'username': usernames.get(account_id),
            'style_group': entry['group'], 'group_size': group['size'] if group else 0,
            'group_listed': group['listed'] if group else 0,
            'group_hidden_accounts': group['hidden_accounts'] if group else 0,
            'peers': list(entry['peers']),
            'examples': [{'post_id': posts[peer]['post_id'], 'account_id': peer, 'username': usernames.get(peer),
                          'excerpt': posts[peer]['excerpt']} for peer in entry['peers'] if peer in posts][:EXAMPLES],
            'possible_teammate': flagged, 'teammate_note': TEAMMATE_NOTE if flagged else None,
            'linked_to_ours': entry['linked_to_ours'],
            'generated_at': index['generated_at'], 'source': 'style_groups'}
    return found


def listed_accounts(*, now=None):
    """From the cache only: every account id account_evidence can answer for; [] before the first
    round and once the result expired. Read-only (blue_reports hands the evidence to its child process)."""
    with _LOCK:
        index = _STATE.get('index')
    if not index or _expired(index.get('generated_at'), now):
        return []
    return sorted(index['accounts'])


class StyleGroupMonitor:
    """One background thread: a round every REFRESH_MINUTES, in a child process unless ``isolated`` is False.

    In-thread rounds (isolated=False) open a fresh connection for each round's
    reads and close it before computing; nothing holds a connection between rounds.
    """

    def __init__(self, db_path, *, first_run_seconds=FIRST_RUN_SECONDS, interval_seconds=REFRESH_MINUTES * 60,
                 retry_seconds=RETRY_SECONDS, isolated=True):
        self.db_path, self.first_run_seconds, self.isolated = db_path, first_run_seconds, isolated
        self.interval_seconds, self.retry_seconds = interval_seconds, retry_seconds
        self._stop = threading.Event()
        self._thread = None
        self._proc = None

    def _started(self, proc):
        self._proc = proc

    def run_once(self):
        """One round; the view, or None after a failure (logged; the previous result stays)."""
        try:
            result = refresh_isolated(self.db_path, started=self._started) if self.isolated else refresh(self.db_path)
        except Exception as exc:
            if not self._stop.is_set():
                LOGGER.error('Style group refresh failed (%s: %s); keeping the previous result, retrying in %s s',
                             type(exc).__name__, str(exc)[:200], self.retry_seconds)
                _failed(exc)
            return None
        finally:
            self._proc = None
        LOGGER.info('Style groups: %s accounts, %s groups, theta %s, %s s CPU', result['counts']['accounts'],
                    result['counts']['groups'], result['theta'], result['run'].get('cpu_seconds'))
        return result

    def _run(self):
        wait = self.first_run_seconds
        while not self._stop.wait(wait):
            result = self.run_once()
            wait = self.interval_seconds if result is not None else self.retry_seconds

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-style-groups', daemon=True)
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
