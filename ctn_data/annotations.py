"""Stored post and account annotations: stance, concerns, thread depth, bot flags.

The rules live in agent_targeting (keyword stance/concerns and behaviour-based
bot detection). This module only runs them incrementally and stores the
results, so pages, analysis and targeting read one table instead of
re-classifying every post on every request.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from . import agent_targeting
from .db import connect, utc_now

LOGGER = logging.getLogger(__name__)
# Classification costs ~1 ms of CPU per post and shares the API process, so a
# pass stays near 5 s; the first backfill of ~150k posts takes about 30 passes.
BATCH_SIZE = 1000
MAX_POSTS_PER_PASS = 5000


def classifier_version():
    """Changes whenever agent_targeting's rules change, so old labels are redone."""
    source = json.dumps([agent_targeting._YES.pattern, agent_targeting._NO.pattern,
                         agent_targeting._UNDECIDED.pattern, agent_targeting._TOPIC.pattern,
                         agent_targeting.CONCERN_PATTERNS], sort_keys=True)
    return hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]


def refresh_posts(db_path, *, limit=MAX_POSTS_PER_PASS):
    """Annotate new, edited or outdated posts, newest first. Returns the count.

    With the team's stance source on 'model' (D84) a post with a stored model label
    (stance_labels) takes that label and classifier stance_labels.CLASSIFIER, so every page
    and query that reads post_annotations follows the switch; on 'keyword' those rows are
    redone with the rules, and switching back re-applies the stored labels.
    """
    from . import stance_labels
    version, done = classifier_version(), 0
    model = stance_labels.mode(db_path) == 'model'
    while done < limit:
        conn = connect(db_path)
        try:
            if model:
                rows = [dict(row) for row in conn.execute(
                    '''SELECT p.post_id,p.author_id,p.body,p.created_at,p.edited_at,l.stance AS model_stance,
                              l.concerns_json AS model_concerns FROM posts p
                       LEFT JOIN post_annotations a ON a.post_id=p.post_id
                       LEFT JOIN post_stance_labels l ON l.post_id=p.post_id
                       WHERE a.post_id IS NULL OR (l.post_id IS NOT NULL AND a.classifier<>?)
                          OR (l.post_id IS NULL AND a.classifier<>?)
                          OR COALESCE(p.edited_at,'')<>COALESCE(a.source_edited_at,'')
                       ORDER BY p.post_id DESC LIMIT ?''',
                    (stance_labels.CLASSIFIER, version, min(BATCH_SIZE, limit - done)))]
            else:
                rows = [dict(row) for row in conn.execute(
                    '''SELECT p.post_id,p.author_id,p.body,p.created_at,p.edited_at FROM posts p
                       LEFT JOIN post_annotations a ON a.post_id=p.post_id
                       WHERE a.post_id IS NULL OR a.classifier<>?
                          OR COALESCE(p.edited_at,'')<>COALESCE(a.source_edited_at,'')
                       ORDER BY p.post_id DESC LIMIT ?''', (version, min(BATCH_SIZE, limit - done)))]
        finally:
            conn.close()
        if not rows:
            break
        depths = agent_targeting.thread_depths(db_path, [row['post_id'] for row in rows])
        now = utc_now()
        values = []
        for row in rows:
            if row.get('model_stance'):
                stance, concerns, tag = row['model_stance'], row['model_concerns'], stance_labels.CLASSIFIER
            else:
                result = agent_targeting.classify(row['body'])
                stance, concerns, tag = result['stance'], json.dumps(result['concerns']), version
            values.append((row['post_id'], row['author_id'], row['created_at'], stance, concerns,
                           depths.get(row['post_id']), row['edited_at'], tag, now))
        conn = connect(db_path)
        try:
            with conn:
                conn.executemany(
                    '''INSERT INTO post_annotations(post_id,author_id,created_at,stance,concerns_json,thread_depth,
                           source_edited_at,classifier,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(post_id) DO UPDATE SET author_id=excluded.author_id,created_at=excluded.created_at,
                           stance=excluded.stance,concerns_json=excluded.concerns_json,thread_depth=excluded.thread_depth,
                           source_edited_at=excluded.source_edited_at,classifier=excluded.classifier,
                           updated_at=excluded.updated_at''', values)
        finally:
            conn.close()
        done += len(rows)
        if len(rows) < BATCH_SIZE:
            break
    return done


FEATURE_REASONS = ('always_on', 'high_volume', 'fast_reply', 'regular_rhythm')
# Agreed with agent_targeting (D39): behaviour alone, or two always-on features.
# Duplicate text or a single feature is recorded but does not make a bot.
FEATURES_FOR_BOT = 2


def is_bot(reasons):
    return 'behavior' in reasons or sum(reason in FEATURE_REASONS for reason in reasons) >= FEATURES_FOR_BOT


def refresh_accounts(db_path):
    """Store the current bot verdicts and reasons; accounts that no longer qualify are cleared.

    A failing feature computation raises before anything is written, so the
    previous verdicts stay until the next pass. Our own accounts keep their
    reasons for display but are never marked as bots.
    """
    reasons, metrics = {}, {}
    for author in agent_targeting.behavior_bots(db_path):
        reasons.setdefault(author, []).append('behavior')
    for author in agent_targeting.duplicate_authors(db_path):
        reasons.setdefault(author, []).append('duplicate_text')
    for author, flags in agent_targeting.bot_feature_flags(db_path).items():
        reasons.setdefault(author, []).extend(reason for reason in flags['reasons'] if reason in FEATURE_REASONS)
        metrics[author] = flags.get('metrics') or {}
    now = utc_now()
    conn = connect(db_path)
    try:
        own = {row['account_id'] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
        with conn:
            current = {row['account_id']: (row['is_bot'], row['bot_reasons_json'], row['bot_metrics_json']) for row in conn.execute(
                "SELECT account_id,is_bot,bot_reasons_json,bot_metrics_json FROM account_annotations "
                "WHERE is_bot=1 OR bot_reasons_json<>'[]'")}
            for account_id in set(current) - set(reasons):
                conn.execute("UPDATE account_annotations SET is_bot=0,bot_reasons_json='[]',bot_metrics_json='{}',updated_at=? "
                             'WHERE account_id=?', (now, account_id))
            for account_id, why in reasons.items():
                flagged = int(is_bot(why) and account_id not in own)
                state = (flagged, json.dumps(sorted(set(why))), json.dumps(metrics.get(account_id, {}), sort_keys=True))
                if current.get(account_id) == state:
                    continue
                conn.execute('''INSERT INTO account_annotations(account_id,is_bot,bot_reasons_json,bot_metrics_json,flagged_at,updated_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET is_bot=excluded.is_bot,
                    bot_reasons_json=excluded.bot_reasons_json,bot_metrics_json=excluded.bot_metrics_json,
                    flagged_at=CASE WHEN excluded.is_bot=0 THEN NULL
                                    WHEN account_annotations.is_bot=1 THEN account_annotations.flagged_at
                                    ELSE excluded.flagged_at END,
                    updated_at=excluded.updated_at''', (account_id, *state, now if state[0] else None, now))
    finally:
        conn.close()
    return sum(is_bot(why) and account_id not in own for account_id, why in reasons.items())


class AnnotationWorker:
    """Lifecycle companion; one bounded pass every minute."""
    def __init__(self, db_path, *, interval_seconds=60):
        self.db_path = db_path
        self.interval_seconds = max(10, interval_seconds)
        self._stop = threading.Event()
        self._thread = None
        self.last_result = None

    def run_once(self):
        self.last_result = {'posts': refresh_posts(self.db_path), 'bots': refresh_accounts(self.db_path),
                            'finished_at': utc_now()}
        return self.last_result

    def _run(self):
        # Let startup and the collectors establish their state first.
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception as exc:
                LOGGER.error('Annotation refresh failed (%s); retrying on the next pass', type(exc).__name__)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='ctn-annotations', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
