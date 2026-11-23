"""Short, read-only observation after a CTN performance reload."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
import psycopg
from ctn_data.secret_files import read_secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=6)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--report', type=Path,
                        default=ROOT / 'reports/generated/system-optimization-live-observation.json')
    args = parser.parse_args()
    if not 1 <= args.samples <= 30 or not 0 <= args.interval <= 30:
        parser.error('Use 1-30 samples and a 0-30 second interval')
    report = {'read_only': True, 'external_requests': 0, 'model_calls': 0,
              'connection_policy': 'Fresh TCP connection for each health probe', 'samples': []}
    # Probe the service independently of a pooled socket expiring at Uvicorn's
    # keepalive boundary between samples.
    with httpx.Client(trust_env=False, timeout=5, limits=httpx.Limits(max_keepalive_connections=0)) as client, psycopg.connect(
            read_secret(ROOT / 'data/secrets/postgres_url').strip(), autocommit=True,
            connect_timeout=5, options='-c default_transaction_read_only=on -c statement_timeout=5000') as conn:
        for index in range(args.samples):
            sample = {'at': datetime.now(timezone.utc).isoformat()}
            started = time.perf_counter()
            try:
                response = client.get('http://127.0.0.1:8765/api/health')
                health = response.json()
                sample.update(health_ok=response.status_code == 200 and health.get('ok') is True,
                              performance_revision=health.get('system_performance_policy'),
                              health_ms=round((time.perf_counter() - started) * 1000, 1))
            except Exception as exc:
                sample.update(health_ok=False, error_type=type(exc).__name__)
            try:
                sample['advisory_waiters'] = conn.execute('''SELECT count(*) FROM pg_stat_activity
                    WHERE datname=current_database() AND wait_event='advisory' ''').fetchone()[0]
            except Exception as exc:
                sample['database_error_type'] = type(exc).__name__
            report['samples'].append(sample)
            if index + 1 < args.samples:
                time.sleep(args.interval)
    report['all_health_checks_passed'] = all(row.get('health_ok') and row.get('performance_revision') == 2
                                            for row in report['samples'])
    report['scope'] = 'Short local observation; not a continuous availability guarantee'
    args.report.write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps(report, indent=2))
    return 0 if report['all_health_checks_passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Read-only observation failed: ' + type(exc).__name__)
        raise SystemExit(1) from None
