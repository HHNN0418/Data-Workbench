"""Read-only report over the llm_calls ledger: per-purpose volume, latency, waits, tokens, cache and cost.

The session starts with BEGIN READ ONLY and is rolled back; no model request
is made and nothing is written.

    .venv/Scripts/python.exe -X utf8 scripts/measure_llm.py --hours 2
    .venv/Scripts/python.exe -X utf8 scripts/measure_llm.py --hours 24 --json
"""
import sys

sys.dont_write_bytecode = True  # keep the run free of side files

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctn_data import db, postgres  # noqa: E402
from ctn_data.db import connect  # noqa: E402
from ctn_data.secret_files import read_secret  # noqa: E402


def _refuse_schema_changes(*args, **kwargs):
    raise RuntimeError('read-only script: schema initialization against the live database is not allowed')


# Board rule 6: a newer working-tree SCHEMA must never reach the live database.
db.initialize = postgres.initialize_postgres = _refuse_schema_changes


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


def total(rows, key):
    return sum(row[key] or 0 for row in rows)


def summarize(rows):
    ok = [row for row in rows if row['status'] == 'ok']
    prompt = total(rows, 'prompt_tokens')
    return {
        'calls': len(rows), 'ok': len(ok),
        'errors': dict(Counter(row['error_code'] or 'error' for row in rows if row['status'] == 'error').most_common()),
        'cancelled': sum(1 for row in rows if row['status'] == 'cancelled'),
        'duration_ms': {'median': pct([r['duration_ms'] for r in ok], .5), 'p90': pct([r['duration_ms'] for r in ok], .9)},
        'ttft_ms': {'median': pct([r['ttft_ms'] for r in ok], .5), 'p90': pct([r['ttft_ms'] for r in ok], .9)},
        'retried_calls': sum(1 for row in rows if (row['attempts'] or 1) > 1),
        'wait_ms_total': round(total(rows, 'wait_ms'), 1),
        'truncated': sum(1 for row in rows if row['error_code'] == 'output_truncated'),
        'prompt_tokens': prompt, 'cached_tokens': total(rows, 'cached_tokens'),
        'cached_share': round(total(rows, 'cached_tokens') / prompt, 3) if prompt else None,
        'completion_tokens': total(rows, 'completion_tokens'), 'reasoning_tokens': total(rows, 'reasoning_tokens'),
        'completion_p99': pct([r['completion_tokens'] for r in ok], .99),
        'cost': round(total(rows, 'cost'), 4),
        'calls_with_cost': sum(1 for row in rows if row['cost'] is not None),
        'upstreams': dict(Counter(row['upstream'] or 'unknown' for row in rows).most_common(8)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--hours', type=float, default=2.0, help='window length in hours (default 2)')
    parser.add_argument('--json', action='store_true', help='print the full JSON report')
    args = parser.parse_args()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    conn = connect(read_secret(ROOT / 'data/secrets/postgres_url').strip())
    try:
        conn.execute('BEGIN READ ONLY')
        rows = [dict(zip(row.keys(), tuple(row))) for row in conn.execute(
            'SELECT * FROM llm_calls WHERE ctn_timestamp(started_at)>=ctn_timestamp(?) ORDER BY ctn_timestamp(started_at)',
            (iso(start),)).fetchall()]
    finally:
        conn.rollback()
        conn.close()
    by_purpose = defaultdict(list)
    for row in rows:
        by_purpose[row['purpose']].append(row)
    report = {'window': {'start': iso(start), 'end': iso(end), 'hours': args.hours}, 'read_only': True,
              'all': summarize(rows), 'by_purpose': {key: summarize(value) for key, value in sorted(by_purpose.items())}}
    print('LLM calls %s .. %s (%sh): %d calls' % (report['window']['start'], report['window']['end'], args.hours, len(rows)))
    print('%-16s %6s %6s %-28s %9s %9s %8s %7s %8s %9s' % ('purpose', 'calls', 'ok', 'errors', 'med_ms', 'ttft_ms',
                                                           'retried', 'cached', 'trunc', 'cost$'))
    for name, item in [('ALL', report['all'])] + list(report['by_purpose'].items()):
        print('%-16s %6d %6d %-28s %9s %9s %8d %7s %8d %9s' % (
            name, item['calls'], item['ok'], json.dumps(item['errors'])[:28], item['duration_ms']['median'],
            item['ttft_ms']['median'], item['retried_calls'], item['cached_share'], item['truncated'], item['cost']))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
