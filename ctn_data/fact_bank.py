"""Citable facts for Agent writing: a reviewed core layer plus news-extracted facts.

Agents may state only active facts. News facts enter as pending or active
according to the operator's review mode, the same choice as draft review.
"""
import hashlib
import json
import re
import threading
import time
from pathlib import Path

from .db import begin_write, connect, utc_now

SETTINGS_KEY = 'fact_bank'
# pipeline: 'staged' = decide -> write -> check with facts; 'single' = the original one-call decision.
DEFAULT_SETTINGS = {'review_mode': 'manual', 'extract_enabled': False, 'provider_id': '', 'pipeline': 'single',
                    'revision': 1}
PIPELINES = ('single', 'staged')
REVIEW_MODES = ('manual', 'auto')
STATUSES = ('pending', 'active', 'rejected', 'superseded', 'retired')
LAYERS = ('core', 'news')
SEED_PATH = Path(__file__).with_name('fact_bank_seed.json')
MAX_CLAIM = 220
CACHE_SECONDS = 60.0
NEAR_DUPLICATE = 0.85
# Time-sensitive subjects: only the newest active fact per topic is offered.
TOPICS = ('poll:referendum', 'h2d2:cases_kingstonia', 'h2d2:regional_spread', 'h2d2:deaths_regional',
          'vaccine:delivery', 'vaccine:evidence', 'hospital:staffing', 'hospital:capacity',
          'rule:venue_list', 'rule:implementation', 'campaign:endorsements', 'referendum:timeline')

CONCERNS = frozenset((
    'access', 'bodily_autonomy', 'children', 'choice', 'cost', 'democracy', 'economy', 'freedom',
    'hospital', 'id_privacy', 'liability', 'lockdown_memory', 'mild_disease', 'momentum', 'new_vaccine',
    'not_here_yet', 'outbreak_risk', 'outer_islands', 'overreaction', 'power_grab', 'precedent',
    'protect_vulnerable', 'rushed', 'safety', 'school', 'surveillance', 'timing', 'tourism', 'two_tier',
    'undecided', 'venue_list', 'wait_and_see', 'work_business'))

# Rotation (deep analysis 2026-10-22: the top 5 facts made 52.5% of uses, 14 core facts never used).
REUSE_HOURS = 6
REUSE_LIMIT = 3
# Team-wide: a fact our roles used this many times in the last hour is not offered
# (D90). On 23 Oct 00-03 UTC 13 roles posted the same nursing-post figures 34 times,
# and all 6 posts reported in that window were among them (ban audit). Sorting it to
# the back was not enough: concern overlap ranks first, and phased_after_access
# (6 concerns) still went into 431 of 685 posts after W35, up to 64 an hour.
TEAM_HOURLY_LIMIT = 3
# A fact that reads as "mostly mild" on its own is only offered with the capacity arithmetic.
PAIRED = {'one_in_twenty': ('icu_capacity', 'surge_150')}
# Extracted claims that restate a No-side argument or a weakness; these always wait for review.
NO_LEANING = re.compile(
    r"warn(?:ed)? significant|questions .* remain|limited (?:real-world )?data|yet to determine|may only become apparent|"
    r"not be fully understood|no evidence of sustained|disproportionate|two-tier|divide society|opposition|opposed|"
    r"withheld|dropped from|\blead\b|wary|civil (?:rights|liberties)|biometric data|no immediate timeline|"
    r"data remains limited|steadfast|voluntary vaccination|mild or moderate|stable condition|"
    r"shops, schools|public transport", re.I)

_CACHE = {}
_CACHE_LOCK = threading.Lock()


class FactBankError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def configuration(data):
    value = data.get('settings', {}).get(SETTINGS_KEY, {})
    return {**DEFAULT_SETTINGS, **{key: value[key] for key in DEFAULT_SETTINGS if key in value}}


def normalize(text):
    """Comparison form: case, quotes, dashes and whitespace do not matter."""
    text = str(text or '').lower()
    text = re.sub(r'[‘’‚‛`]', "'", text)
    text = re.sub(r'[“”„‟]', '"', text)
    text = re.sub(r'[‐-―]', '-', text)
    return re.sub(r'\s+', ' ', text).strip()


def claim_hash(claim):
    plain = re.sub(r'[^a-z0-9% ]+', '', normalize(claim))
    return hashlib.sha256(re.sub(r'\s+', ' ', plain).strip().encode()).hexdigest()


def _concerns(values):
    if not isinstance(values, list) or not values or len(values) > 6:
        raise FactBankError('事实的顾虑标签需为 1–6 个')
    unknown = sorted(set(values) - CONCERNS)
    if unknown:
        raise FactBankError('未知的顾虑标签：' + ', '.join(unknown))
    return sorted(set(values))


def _claim(value):
    if not isinstance(value, str) or not value.strip():
        raise FactBankError('事实内容不能为空')
    value = re.sub(r'\s+', ' ', value).strip()
    if len(value) > MAX_CLAIM:
        raise FactBankError(f'事实内容最多 {MAX_CLAIM} 字符')
    if re.search(r'[一-鿿]', value):
        raise FactBankError('事实内容须为英文，Agent 会直接引用')
    return value


def _row(row):
    item = dict(row)
    item['concerns'] = json.loads(item.pop('concerns_json'))
    return item


def invalidate():
    with _CACHE_LOCK:
        _CACHE.clear()


def initialize(conn, *, now=None):
    """Import seed rows that do not exist yet. Existing rows stay as edited."""
    if not SEED_PATH.exists():
        return 0
    seed = json.loads(SEED_PATH.read_text(encoding='utf-8'))
    now = now or utc_now()
    added = 0
    existing = {row['id'] for row in conn.execute("SELECT id FROM facts WHERE layer='core'")}
    for fact in seed.get('facts', []):
        if fact['id'] in existing:
            continue
        claim = _claim(fact['claim'])
        conn.execute('''INSERT INTO facts(id,layer,status,claim,claim_hash,concerns_json,source_label,
            decided_by,created_at,updated_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING''',
            (fact['id'], 'core', 'active', claim, claim_hash(claim),
             json.dumps(_concerns(fact['concerns'])), fact['source'], 'import', now, now, now))
        added += 1
    for rebuttal in seed.get('rebuttals', []):
        conn.execute('''INSERT INTO fact_rebuttals(concern,no_argument,line,fact_ids_json,status,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING''',
            (rebuttal['concern'], rebuttal['no_argument'], rebuttal['line'],
             json.dumps(rebuttal['fact_ids']), 'active', now))
    for index, rule in enumerate(seed.get('do_not_claim', []), 1):
        conn.execute('''INSERT INTO fact_guardrails(id,rule,status,updated_at) VALUES(?,?,?,?)
            ON CONFLICT DO NOTHING''', ('seed-' + str(index), rule, 'active', now))
    if added:
        invalidate()
    return added


def _snapshot(db_path):
    key = str(db_path)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached['until'] > now:
            return cached
    conn = connect(db_path)
    try:
        facts = [_row(row) for row in conn.execute(
            "SELECT * FROM facts WHERE status='active' ORDER BY layer,observed_at DESC,id")]
        rebuttals = [{**dict(row), 'fact_ids': json.loads(row['fact_ids_json'])} for row in conn.execute(
            "SELECT concern,no_argument,line,fact_ids_json FROM fact_rebuttals WHERE status='active' ORDER BY concern")]
        guardrails = [row['rule'] for row in conn.execute(
            "SELECT rule FROM fact_guardrails WHERE status='active' ORDER BY id")]
    finally:
        conn.close()
    for rebuttal in rebuttals:
        rebuttal.pop('fact_ids_json', None)
    value = {'until': now + CACHE_SECONDS, 'facts': facts, 'rebuttals': rebuttals, 'guardrails': guardrails}
    with _CACHE_LOCK:
        _CACHE[key] = value
    return value


def _current(facts, now_iso):
    return [f for f in facts if not f.get('valid_until') or f['valid_until'] > now_iso]


NEWS_URL = 'https://sites.legitreal.com/'


def public_fact(fact):
    return {'id': fact['id'], 'claim': fact['claim'], 'source': fact['source_label'],
            'concerns': fact['concerns'], **({'observed_at': fact['observed_at']} if fact.get('observed_at') else {}),
            **({'url': NEWS_URL + fact['article_key']} if fact.get('article_key') else {})}


def usage(db_path, agent_id=None, *, hours=REUSE_HOURS, now_iso=None):
    """Fact uses as (this account's counts, the team's counts) over the last hours,
    then the team's counts over the last hour, then the attributed posts and drafts
    as ('post', post_id) and ('draft', draft_id) keys."""
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    now = datetime.fromisoformat((now_iso or utc_now()).replace('Z', '+00:00'))
    stamp = lambda value: value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    since, hour = stamp(now - timedelta(hours=hours)), stamp(now - timedelta(hours=1))
    mine, team, recent, attributed = Counter(), Counter(), Counter(), set()
    conn = connect(db_path)
    try:
        rows = conn.execute('SELECT post_id,draft_id,agent_id,fact_ids_json,published_at FROM post_attribution '
                            'WHERE published_at>=?', (since,)).fetchall()
    finally:
        conn.close()
    for row in rows:
        ids = json.loads(row['fact_ids_json'] or '[]')
        team.update(ids)
        if (row['published_at'] or '') >= hour:
            recent.update(ids)
        if agent_id is not None and row['agent_id'] == agent_id:
            mine.update(ids)
        attributed.add(('post', row['post_id']))
        if row['draft_id']:
            attributed.add(('draft', row['draft_id']))
    return mine, team, recent, attributed


def facts_for(db_path, concerns, *, limit=5, now_iso=None, agent_id=None, used=None):
    """Most relevant active facts: concern overlap, core before news, least used first.

    used is (account counts, team counts, team counts this hour) from usage(); it is read
    here when agent_id is given. A fact this account used REUSE_LIMIT times in REUSE_HOURS
    goes to the back. A fact the team used TEAM_HOURLY_LIMIT times this hour is left out,
    unless a wanted concern has no other fact: then that concern's least used one comes last.
    """
    wanted = set(concerns or ()) & CONCERNS
    if not wanted:
        return []
    current = _current(_snapshot(db_path)['facts'], now_iso or utc_now())
    if used is None and agent_id is not None:
        used = usage(db_path, agent_id, now_iso=now_iso)
    mine, team, recent = (tuple(used or ()) + ({}, {}, {}))[:3]
    capped = {f['id'] for f in current if recent.get(f['id'], 0) >= TEAM_HOURLY_LIMIT}
    matching = [f for f in current if wanted & set(f['concerns'])]
    facts = [f for f in matching if f['id'] not in capped]
    covered = {concern for f in facts for concern in f['concerns']}
    for concern in sorted(wanted - covered):
        spare = [f for f in matching if concern in f['concerns']]
        if spare:
            fallback = min(spare, key=lambda f: (recent.get(f['id'], 0), team.get(f['id'], 0), f['id']))
            if fallback not in facts:
                facts.append(fallback)
    # Stable sorts, least important key first.
    facts.sort(key=lambda f: (f.get('observed_at') or '', f['id']), reverse=True)
    facts.sort(key=lambda f: (f['id'] in capped, -len(wanted & set(f['concerns'])),
                              mine.get(f['id'], 0) >= REUSE_LIMIT, f['layer'] != 'core', team.get(f['id'], 0)))
    limit, by_id, chosen = max(0, int(limit)), {f['id']: f for f in current}, []
    for fact in facts:
        if len(chosen) >= limit:
            break
        if fact in chosen:
            continue
        # A capped partner stays out too; without one the paired fact is skipped.
        partners = [by_id[p] for p in PAIRED.get(fact['id'], ()) if p in by_id and p not in capped]
        if fact['id'] in PAIRED and not any(p in chosen for p in partners):
            # Skip it when there is no partner or no room for one.
            if not partners or len(chosen) + 2 > limit:
                continue
            chosen.extend([fact, partners[0]])
            continue
        chosen.append(fact)
    return [public_fact(f) for f in chosen]


def by_ids(db_path, ids, *, now_iso=None):
    wanted = list(dict.fromkeys(ids or ()))
    facts = {f['id']: f for f in _current(_snapshot(db_path)['facts'], now_iso or utc_now())}
    return [public_fact(facts[i]) for i in wanted if i in facts]


def rebuttals_for(db_path, concerns):
    wanted = set(concerns or ())
    return [dict(row) for row in _snapshot(db_path)['rebuttals'] if row['concern'] in wanted]


def guardrails(db_path):
    return list(_snapshot(db_path)['guardrails'])


def catalog(db_path, *, now_iso=None):
    """Compact list of every usable fact for the decision step."""
    return [{'id': f['id'], 'claim': f['claim'], 'concerns': f['concerns']}
            for f in _current(_snapshot(db_path)['facts'], now_iso or utc_now())]


def _activate(conn, fact, now):
    """Activate a fact; within one topic only the newest report's facts stay active.

    Facts observed at the same time (one article, or two papers on the same
    story) are different findings of that report and stay active together.
    """
    status, supersedes = 'active', None
    if fact.get('topic_key'):
        others = conn.execute('''SELECT id,observed_at FROM facts WHERE topic_key=? AND status='active' AND id<>?''',
                              (fact['topic_key'], fact['id'])).fetchall()
        mine = fact.get('observed_at') or ''
        newer = [row for row in others if (row['observed_at'] or '') > mine]
        if newer:
            status = 'superseded'
        else:
            older = sorted((row for row in others if (row['observed_at'] or '') < mine),
                           key=lambda row: row['observed_at'] or '')
            for row in older:
                conn.execute('''UPDATE facts SET status='superseded',updated_at=?,revision=revision+1 WHERE id=?''',
                             (now, row['id']))
            supersedes = older[-1]['id'] if older else None
    conn.execute('''UPDATE facts SET status=?,supersedes_id=COALESCE(?,supersedes_id),updated_at=?,
        revision=revision+1 WHERE id=?''', (status, supersedes, now, fact['id']))
    return status


def insert_extracted(conn, rows, *, review_mode, article_key, source_label, observed_at, now=None):
    """Store verified news facts; returns (accepted, duplicates). Caller owns the transaction."""
    from difflib import SequenceMatcher
    now = now or utc_now()
    accepted = duplicates = 0
    # Both papers usually report the same story; near-identical claims are one fact.
    # Same wording with different numbers (a new poll, a new case count) is new.
    def numbers(text):
        return sorted(re.findall(r'\d+(?:\.\d+)?', text.replace(',', '')))
    known = [(normalize(row['claim']), numbers(row['claim'])) for row in conn.execute(
        "SELECT claim FROM facts WHERE status IN ('pending','active')")]
    for row in rows:
        claim = _claim(row['claim'])
        digest = claim_hash(claim)
        plain, figures = normalize(claim), numbers(claim)
        if (conn.execute('SELECT 1 FROM facts WHERE claim_hash=?', (digest,)).fetchone()
                or any(figures == other_figures and SequenceMatcher(None, plain, other).ratio() >= NEAR_DUPLICATE
                       for other, other_figures in known)):
            duplicates += 1
            continue
        known.append((plain, figures))
        topic = row.get('topic_key') if row.get('topic_key') in TOPICS else None
        fact = {'id': 'n:' + digest[:16], 'topic_key': topic, 'observed_at': observed_at}
        auto = review_mode == 'auto' and not NO_LEANING.search(claim)
        conn.execute('''INSERT INTO facts(id,layer,status,claim,claim_hash,concerns_json,source_label,article_key,
            evidence_quote,observed_at,topic_key,decided_by,decision_note,created_at,updated_at,decided_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (fact['id'], 'news', 'pending', claim, digest, json.dumps(_concerns(row['concerns'])), source_label,
             article_key, row.get('evidence_quote'), observed_at, fact['topic_key'],
             'auto' if auto else None, None if auto or review_mode != 'auto' else '可能偏向 No 方，需人工确认',
             now, now, now if auto else None))
        if auto:
            _activate(conn, fact, now)
        accepted += 1
    if accepted:
        invalidate()
    return accepted, duplicates


def _load(conn, fact_id):
    row = conn.execute('SELECT * FROM facts WHERE id=?', (fact_id,)).fetchone()
    if row is None:
        raise FactBankError('事实不存在', 404)
    return _row(row)


def _write(db_path, callback):
    conn = connect(db_path)
    try:
        with conn:
            begin_write(conn, 'fact_bank')
            result = callback(conn)
    finally:
        conn.close()
    invalidate()
    return result


def list_facts(db_path, *, status=None, layer=None, q='', limit=100, offset=0):
    clauses, args = [], []
    if status:
        if status not in STATUSES:
            raise FactBankError('状态筛选无效')
        clauses.append('status=?')
        args.append(status)
    if layer:
        if layer not in LAYERS:
            raise FactBankError('来源层筛选无效')
        clauses.append('layer=?')
        args.append(layer)
    if q:
        clauses.append('(lower(claim) LIKE ? OR lower(source_label) LIKE ?)')
        args += ['%' + q.lower() + '%'] * 2
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    conn = connect(db_path)
    try:
        total = conn.execute('SELECT COUNT(*) FROM facts' + where, tuple(args)).fetchone()[0]
        rows = conn.execute('SELECT * FROM facts' + where +
                            " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,"
                            ' COALESCE(observed_at,created_at) DESC,id LIMIT ? OFFSET ?',
                            (*args, int(limit), int(offset))).fetchall()
        counts = {row['status']: row['n'] for row in conn.execute(
            'SELECT status,COUNT(*) AS n FROM facts GROUP BY status')}
    finally:
        conn.close()
    return {'items': [_row(row) for row in rows], 'total': total, 'counts': counts}


def decide(db_path, fact_id, action, *, expected_revision, note='', edits=None):
    """approve | reject | retire | restore, optionally editing before approval."""
    if action not in ('approve', 'reject', 'retire', 'restore', 'edit'):
        raise FactBankError('操作无效')

    def apply(conn):
        fact = _load(conn, fact_id)
        if fact['revision'] != expected_revision:
            raise FactBankError('该事实已被其他成员修改，请刷新后再操作', 409)
        now = utc_now()
        if edits:
            claim = _claim(edits.get('claim', fact['claim']))
            concerns = _concerns(edits.get('concerns', fact['concerns']))
            topic = edits.get('topic_key', fact['topic_key']) or None
            valid_until = edits.get('valid_until', fact['valid_until']) or None
            digest = claim_hash(claim)
            clash = conn.execute('SELECT id FROM facts WHERE claim_hash=? AND id<>?', (digest, fact_id)).fetchone()
            if clash:
                raise FactBankError('已有相同内容的事实：' + clash['id'], 409)
            conn.execute('''UPDATE facts SET claim=?,claim_hash=?,concerns_json=?,topic_key=?,valid_until=?,
                updated_at=?,revision=revision+1 WHERE id=?''',
                (claim, digest, json.dumps(concerns), topic, valid_until, now, fact_id))
            fact.update(claim=claim, topic_key=topic)
        if action == 'edit':
            return _load(conn, fact_id)
        allowed = {'approve': ('pending', 'rejected'), 'reject': ('pending',),
                   'retire': ('active',), 'restore': ('retired', 'superseded')}[action]
        if fact['status'] not in allowed:
            raise FactBankError('当前状态不能执行该操作', 409)
        conn.execute('''UPDATE facts SET decided_by='operator',decided_at=?,decision_note=?,updated_at=?
            WHERE id=?''', (now, str(note or '')[:500], now, fact_id))
        if action in ('approve', 'restore'):
            _activate(conn, fact, now)
        else:
            conn.execute('UPDATE facts SET status=?,updated_at=?,revision=revision+1 WHERE id=?',
                         ('rejected' if action == 'reject' else 'retired', now, fact_id))
        return _load(conn, fact_id)

    return _write(db_path, apply)


def approve_pending(db_path):
    def apply(conn):
        rows = conn.execute("SELECT * FROM facts WHERE status='pending' ORDER BY observed_at,id").fetchall()
        now = utc_now()
        for row in rows:
            conn.execute('''UPDATE facts SET decided_by='operator',decided_at=?,updated_at=? WHERE id=?''',
                         (now, now, row['id']))
            _activate(conn, _row(row), now)
        return {'approved': len(rows)}
    return _write(db_path, apply)


def create_fact(db_path, payload):
    """Operator-added core fact; it is active immediately."""
    claim = _claim(payload.get('claim'))
    concerns = _concerns(payload.get('concerns'))
    source = str(payload.get('source_label') or '').strip()
    if not source or len(source) > 200:
        raise FactBankError('请填写事实来源（最多 200 字符）')

    def apply(conn):
        digest = claim_hash(claim)
        if conn.execute('SELECT 1 FROM facts WHERE claim_hash=?', (digest,)).fetchone():
            raise FactBankError('已有相同内容的事实', 409)
        now = utc_now()
        fact = {'id': 'c:' + digest[:16], 'topic_key': payload.get('topic_key') or None,
                'observed_at': payload.get('observed_at') or now}
        conn.execute('''INSERT INTO facts(id,layer,status,claim,claim_hash,concerns_json,source_label,observed_at,
            topic_key,valid_until,decided_by,created_at,updated_at,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (fact['id'], 'core', 'pending', claim, digest, json.dumps(concerns), source, fact['observed_at'],
             fact['topic_key'], payload.get('valid_until') or None, 'operator', now, now, now))
        _activate(conn, fact, now)
        return _load(conn, fact['id'])
    return _write(db_path, apply)



def list_rules(db_path):
    conn = connect(db_path)
    try:
        rebuttals = [{**dict(row), 'fact_ids': json.loads(row['fact_ids_json'])}
                     for row in conn.execute('SELECT * FROM fact_rebuttals ORDER BY concern')]
        rules = [dict(row) for row in conn.execute('SELECT * FROM fact_guardrails ORDER BY id')]
    finally:
        conn.close()
    for row in rebuttals:
        row.pop('fact_ids_json', None)
    return {'rebuttals': rebuttals, 'guardrails': rules, 'concerns': sorted(CONCERNS)}


def save_rebuttal(db_path, concern, payload):
    if concern not in CONCERNS:
        raise FactBankError('未知的顾虑标签')
    for key in ('no_argument', 'line'):
        if not isinstance(payload.get(key), str) or not payload[key].strip() or len(payload[key]) > 400:
            raise FactBankError('反驳内容需填写且最多 400 字符')
    fact_ids = payload.get('fact_ids') or []
    if not isinstance(fact_ids, list) or len(fact_ids) > 8:
        raise FactBankError('关联事实最多 8 条')
    status = payload.get('status', 'active')
    if status not in ('active', 'retired'):
        raise FactBankError('状态无效')

    def apply(conn):
        missing = [i for i in fact_ids if not conn.execute('SELECT 1 FROM facts WHERE id=?', (i,)).fetchone()]
        if missing:
            raise FactBankError('关联事实不存在：' + ', '.join(missing))
        row = conn.execute('SELECT revision FROM fact_rebuttals WHERE concern=?', (concern,)).fetchone()
        now = utc_now()
        if row is None:
            conn.execute('''INSERT INTO fact_rebuttals(concern,no_argument,line,fact_ids_json,status,updated_at)
                VALUES(?,?,?,?,?,?)''', (concern, payload['no_argument'].strip(), payload['line'].strip(),
                                         json.dumps(fact_ids), status, now))
        else:
            if payload.get('expected_revision') != row['revision']:
                raise FactBankError('该反驳已被其他成员修改，请刷新后再保存', 409)
            conn.execute('''UPDATE fact_rebuttals SET no_argument=?,line=?,fact_ids_json=?,status=?,updated_at=?,
                revision=revision+1 WHERE concern=?''', (payload['no_argument'].strip(), payload['line'].strip(),
                                                         json.dumps(fact_ids), status, now, concern))
        return {'concern': concern}
    return _write(db_path, apply)


def save_guardrail(db_path, rule_id, payload):
    rule = payload.get('rule')
    if not isinstance(rule, str) or not rule.strip() or len(rule) > 400:
        raise FactBankError('禁止说法需填写且最多 400 字符')
    status = payload.get('status', 'active')
    if status not in ('active', 'retired'):
        raise FactBankError('状态无效')

    def apply(conn):
        now = utc_now()
        if rule_id is None:
            new_id = 'g:' + hashlib.sha256(rule.encode()).hexdigest()[:12]
            conn.execute('INSERT INTO fact_guardrails(id,rule,status,updated_at) VALUES(?,?,?,?)',
                         (new_id, rule.strip(), status, now))
            return {'id': new_id}
        row = conn.execute('SELECT revision FROM fact_guardrails WHERE id=?', (rule_id,)).fetchone()
        if row is None:
            raise FactBankError('禁止说法不存在', 404)
        if payload.get('expected_revision') != row['revision']:
            raise FactBankError('该条已被其他成员修改，请刷新后再保存', 409)
        conn.execute('''UPDATE fact_guardrails SET rule=?,status=?,updated_at=?,revision=revision+1 WHERE id=?''',
                     (rule.strip(), status, now, rule_id))
        return {'id': rule_id}
    return _write(db_path, apply)


def settings(controller, payload=None):
    """Get/save the fact review policy, stored with the other Agent settings."""
    if payload is not None:
        if not isinstance(payload, dict) or set(payload) - {
                'review_mode', 'extract_enabled', 'provider_id', 'pipeline', 'expected_revision'}:
            raise FactBankError('事实库设置字段无效')
        current = configuration(controller.store.read())
        proposed = {**current, **{k: payload[k] for k in ('review_mode', 'extract_enabled', 'provider_id', 'pipeline')
                                  if k in payload}}
        if (proposed['review_mode'] not in REVIEW_MODES or proposed['pipeline'] not in PIPELINES
                or type(proposed['extract_enabled']) is not bool
                or not isinstance(proposed['provider_id'], str) or len(proposed['provider_id']) > 128):
            raise FactBankError('事实库设置格式无效')
        if proposed['extract_enabled']:
            try:
                provider = controller.providers.get(proposed['provider_id'])
            except Exception:
                provider = None
            if not provider or provider.get('protocol') != 'openai_chat':
                raise FactBankError('开启新闻事实提取前，请选择可用的文字 AI 连接')

        def save(data, conn):
            old = configuration(data)
            if payload.get('expected_revision') != old['revision']:
                raise FactBankError('事实库设置已被其他成员修改，请载入最新配置后再保存', 409)
            data['settings'][SETTINGS_KEY] = {**proposed, 'revision': old['revision'] + 1}
            controller._event(data, 'fact_bank_settings', '已更新事实库设置',
                              details={'review_mode': proposed['review_mode'], 'pipeline': proposed['pipeline'],
                                       'extract_enabled': proposed['extract_enabled']})
        controller.store.change(save)
    return configuration(controller.store.read())
