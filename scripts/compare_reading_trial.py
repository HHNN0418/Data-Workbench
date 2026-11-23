"""Read-only comparison of the rules-mode reading trial (D50) against model-mode Agents.

For each enabled Agent, over the trial window and the same length of time before
it: reading model calls, published posts and replies, how fresh and how
persuadable the reply targets were, and the replies our posts received. Every
database session is BEGIN READ ONLY and rolled back; nothing is written.

    .venv/Scripts/python.exe -X utf8 scripts/compare_reading_trial.py --since 2026-10-22T02:41:52Z
"""
import sys

sys.dont_write_bytecode = True

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import ctn_data.db as db  # noqa: E402


def _blocked(*args, **kwargs):
    raise RuntimeError('initialize() is blocked in this read-only script')


db.initialize = _blocked  # rule 6: no DDL against the live database

from ctn_data import agent_targeting  # noqa: E402
from ctn_data.secret_files import read_secret  # noqa: E402
from measure_kpis import AGENTS_META_KEY, ReadOnly, audit_rows, iso, loads, median, parse_iso, readonly_connect  # noqa: E402

FRESH_MINUTES = 10
# Originals that @mention a public figure (agent_targeting.PUBLIC_FIGURES, D51).
FIGURE = re.compile(r'(?<![\w@])@(?:' + '|'.join(agent_targeting.PUBLIC_FIGURES) + r')\b', re.I)


def _chunks(values, size=500):
    values = sorted(values)
    for begin in range(0, len(values), size):
        yield values[begin:begin + size]


def window(conn, agents, start, end):
    """Per-agent measures for [start, end)."""
    stats = {agent_id: Counter() for agent_id in agents}
    for row in conn.execute('SELECT agent_id,purpose,COUNT(*),SUM(prompt_tokens),SUM(completion_tokens) FROM llm_calls '
                            'WHERE started_at>=? AND started_at<? GROUP BY agent_id,purpose', (iso(start), iso(end))):
        if row[0] in stats:
            # PostgreSQL SUM returns Decimal.
            stats[row[0]][row[1] + '_calls'] += int(row[2])
            stats[row[0]]['tokens'] += int(row[3] or 0) + int(row[4] or 0)
    published = [(row['agent_id'], row['at'], loads(row['details_json'])) for row in audit_rows(conn, ('published',), start, end)
                 if row['agent_id'] in stats]
    targets = {d.get('target_post_id') for _, _, d in published if d.get('action') == 'reply'} - {None}
    ours = {d.get('post_id') for _, _, d in published} - {None}
    posts = {}
    for chunk in _chunks({p for p in targets | ours if type(p) is int}):
        for row in conn.execute('SELECT post_id,author_id,body,ctn_timestamp(created_at) FROM posts WHERE post_id IN ('
                                + ','.join('?' for _ in chunk) + ')', tuple(chunk)):
            posts[row[0]] = {'author': row[1], 'body': row[2] or '', 'at': parse_iso(row[3])}
    responses = defaultdict(set)
    for chunk in _chunks({p for p in ours if type(p) is int}):
        for row in conn.execute('SELECT reply_to_post_id,author_id FROM posts WHERE reply_to_post_id IN ('
                                + ','.join('?' for _ in chunk) + ')', tuple(chunk)):
            responses[row[0]].add(row[1])
    return stats, published, posts, responses


def summarise(url, agents, start, end, own):
    with ReadOnly(url) as conn:
        stats, published, posts, responses = window(conn, agents, start, end)
    authors = {posts[d['target_post_id']]['author'] for _, _, d in published
               if d.get('action') == 'reply' and d.get('target_post_id') in posts}
    authors |= {author for ids in responses.values() for author in ids}
    original = agent_targeting.connect
    agent_targeting.connect = readonly_connect
    try:
        citizens = agent_targeting.citizen_accounts(url, authors, own)
    finally:
        agent_targeting.connect = original
    ages = defaultdict(list)
    for agent_id, at, details in published:
        item = stats[agent_id]
        item[details.get('action') or 'unknown'] += 1
        responders = responses.get(details.get('post_id'), set()) - own
        item['responses'] += len(responders)
        item['citizen_responses'] += len(responders & citizens)
        if details.get('action') == 'post' and FIGURE.search(posts.get(details.get('post_id'), {}).get('body', '')):
            item['figure_posts'] += 1
            item['figure_citizen_responses'] += len(responders & citizens)
        target = posts.get(details.get('target_post_id')) if details.get('action') == 'reply' else None
        if target:
            if target['at'] and parse_iso(at):
                ages[agent_id].append((parse_iso(at) - target['at']).total_seconds() / 60)
            item['citizen_target'] += target['author'] in citizens
            item['persuadable_target'] += agent_targeting.classify(target['body'])['stance'] in ('undecided', 'no')
    result = {}
    for agent_id, item in stats.items():
        replies, minutes = item['reply'], ages[agent_id]
        result[agent_id] = {
            'name': agents[agent_id]['name'], 'mode': agents[agent_id].get('reading_selection_mode', 'model'),
            'reading_calls': item['reading_calls'], 'decide_calls': item['decide_calls'],
            'write_calls': item['write_calls'], 'tokens': item['tokens'],
            'posts': item['post'], 'replies': replies,
            'reply_target_age_median_min': round(median(minutes), 1) if minutes else None,
            'fresh_reply_share': round(sum(m <= FRESH_MINUTES for m in minutes) / len(minutes), 2) if minutes else None,
            'citizen_target_share': round(item['citizen_target'] / replies, 2) if replies else None,
            'persuadable_target_share': round(item['persuadable_target'] / replies, 2) if replies else None,
            'responses': item['responses'], 'citizen_responses': item['citizen_responses'],
            'figure_posts': item['figure_posts'], 'figure_citizen_responses': item['figure_citizen_responses'],
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--since', required=True, help='trial start, ISO UTC (when the Agent was switched to rules)')
    args = parser.parse_args()
    start, end = parse_iso(args.since), datetime.now(timezone.utc)
    url = read_secret(ROOT / 'data/secrets/postgres_url').strip()
    with ReadOnly(url) as conn:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (AGENTS_META_KEY,)).fetchone()
        own = {r[0] for r in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
    agents = {agent_id: agent for agent_id, agent in json.loads(row[0]).items() if agent.get('enabled')}
    own |= {agent['account_id'] for agent in agents.values() if type(agent.get('account_id')) is int}
    report = {'trial': summarise(url, agents, start, end, own),
              'before': summarise(url, agents, start - (end - start), start, own),
              'window_minutes': round((end - start).total_seconds() / 60), 'since': iso(start), 'until': iso(end)}
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
