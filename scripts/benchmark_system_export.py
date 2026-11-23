"""Compare CSV export ASGI streaming on an isolated synthetic database."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ctn_data.db import connect
from ctn_data.server import create_app


async def measure(endpoint):
    digest = hashlib.sha256()
    chunks = total = 0
    started = time.perf_counter()
    async for chunk in endpoint('posts').body_iterator:
        digest.update(chunk)
        chunks += 1
        total += len(chunk)
    return {'seconds': time.perf_counter() - started, 'chunks': chunks,
            'bytes': total, 'sha256': digest.hexdigest()}


async def run(args):
    spec = importlib.util.spec_from_file_location('ctn_data._baseline_export_server', args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    with tempfile.TemporaryDirectory(dir=ROOT / '.tmp') as temp:
        db = Path(temp) / 'synthetic.db'
        before_app, after_app = baseline.create_app(db), create_app(db)
        connection = connect(db)
        try:
            with connection:
                connection.executemany('''INSERT INTO posts
                    (post_id,kind,body,created_at,first_seen,last_seen) VALUES(?,?,?,?,?,?)''',
                    ((index, 'post', '=CSV 合成数据,"带引号"\n第二行 ' + 'x' * 100,
                      '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z')
                     for index in range(1, args.posts + 1)))
        finally:
            connection.close()

        def endpoint(app):
            return next(route.endpoint for route in app.routes if getattr(route, 'path', '') == '/api/export')

        before, after = [], []
        for _ in range(3):
            before.append(await measure(endpoint(before_app)))
            after.append(await measure(endpoint(after_app)))
        assert len({sample['sha256'] for sample in before + after}) == 1
        before_time, after_time = (statistics.median(sample['seconds'] for sample in group)
                                   for group in (before, after))
        return {'fixture': 'synthetic local CSV export; no production data or external requests',
                'posts': args.posts, 'runs': 3, 'identical_csv': True,
                'before': {'median_seconds': round(before_time, 4), 'chunks': before[0]['chunks']},
                'after': {'median_seconds': round(after_time, 4), 'chunks': after[0]['chunks']},
                'speedup': round(before_time / after_time, 2),
                'scope': 'ASGI streaming only; one local workload, not a production throughput guarantee'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--posts', type=int, default=4000)
    parser.add_argument('--baseline', type=Path,
                        default=ROOT / 'backups/system-optimization-20261021/server/ctn_data/server.py')
    parser.add_argument('--report', type=Path,
                        default=ROOT / 'reports/generated/system-optimization-export-benchmark.json')
    args = parser.parse_args()
    report = asyncio.run(run(args))
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
