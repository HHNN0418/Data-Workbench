"""Read-only post-deployment observation; never starts an Agent or model call."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from ctn_data.secret_files import read_secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--secret-file', default='data/secrets/postgres_url')
    parser.add_argument('--seconds', type=float, default=100)
    parser.add_argument('--report', default='reports/generated/heartbeat-lock-live-verification.json')
    args = parser.parse_args()
    started = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    report = {'started_at': started, 'read_only': True, 'model_requests_started_by_probe': 0,
              'samples': [], 'heartbeat_updates': {}, 'observed_expired_heartbeats': [], 'errors': []}
    until = time.monotonic() + args.seconds
    with psycopg.connect(read_secret(args.secret_file).strip(), autocommit=True,
            options='-c default_transaction_read_only=on -c statement_timeout=10000', connect_timeout=6) as conn:
        while True:
            try:
                health_started = time.monotonic()
                with urlopen('http://127.0.0.1:8765/api/health', timeout=5) as response:
                    health = json.load(response)
                health_seconds = round(time.monotonic() - health_started, 4)
                agents = json.loads(conn.execute(
                    "SELECT value FROM meta WHERE key='agent_control_v1:agents'").fetchone()[0])
                sample = {'at': datetime.now(timezone.utc).isoformat(),
                          'health_ok': health.get('ok') is True,
                          'health_seconds': health_seconds,
                          'heartbeat_policy': health.get('agent_heartbeat_policy'),
                          'connection_setup_policy': health.get('ai_connection_setup_policy'), 'agents': []}
                for agent in agents.values():
                    job = agent.get('generation') or {}
                    row = {'name': agent['name'], 'job_id': job.get('id'), 'status': job.get('status'),
                           'stage': job.get('stage'), 'updated_at': job.get('updated_at'),
                           'elapsed_seconds': job.get('elapsed_seconds')}
                    for key in ('dns_seconds', 'connect_seconds', 'tls_seconds', 'send_seconds', 'local_progress_seconds'):
                        if key in job:
                            row[key] = job[key]
                    sample['agents'].append(row)
                    if job.get('status') in ('queued', 'running', 'cancelling'):
                        seen = report['heartbeat_updates'].setdefault(job['id'], [])
                        if job.get('updated_at') not in seen:
                            seen.append(job.get('updated_at'))
                        if job.get('heartbeat_until', 0) <= time.time():
                            report['observed_expired_heartbeats'].append(row)
                sample['advisory_waiters'] = conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'").fetchone()[0]
                report['samples'].append(sample)
            except Exception as exc:
                report['errors'].append({'type': type(exc).__name__})
            if time.monotonic() >= until:
                break
            time.sleep(min(5, max(0, until - time.monotonic())))
        rows = conn.execute("SELECT at,agent_id,kind,message,details_json FROM agent_audit_events "
                            "WHERE at >= %s AND kind IN ('generation_failed','generation_interrupted','reading_selection','error') "
                            "ORDER BY at", (started,)).fetchall()
        report['new_events'] = [{'at': at, 'agent_id': aid, 'kind': kind, 'message': message,
                                 'details': json.loads(details)} for at, aid, kind, message, details in rows]
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    try:
        dist = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'
        with urlopen('http://127.0.0.1:8765/', timeout=5) as response:
            served = response.read()
        report['frontend_index_matches_build'] = served == (dist / 'index.html').read_bytes()
        report['frontend_assets'] = []
        for asset in sorted(set(re.findall(r'(?:src|href)="(/assets/[A-Za-z0-9_.-]+)"', served.decode('utf-8')))):
            with urlopen('http://127.0.0.1:8765' + asset, timeout=5) as response:
                body = response.read()
            report['frontend_assets'].append({'asset': asset, 'matches_build':
                hashlib.sha256(body).digest() == hashlib.sha256((dist / asset.lstrip('/')).read_bytes()).digest()})
        report['frontend_assets_verified'] = (report['frontend_index_matches_build']
            and bool(report['frontend_assets']) and all(row['matches_build'] for row in report['frontend_assets']))
    except Exception as exc:
        report['errors'].append({'frontend_verification_error_type': type(exc).__name__})
        report['frontend_assets_verified'] = False
    report['all_health_checks_passed'] = bool(report['samples']) and all(
        sample['health_ok'] and sample['heartbeat_policy'] == 1
        and sample['connection_setup_policy'] == 1 for sample in report['samples'])
    report['active_jobs_with_multiple_updates'] = sum(len(updates) > 1 for updates in report['heartbeat_updates'].values())
    report['status'] = 'passed' if (report['all_health_checks_passed'] and not report['errors']
        and report['frontend_assets_verified']
        and not report['observed_expired_heartbeats']
        and not any(event['kind'] in ('generation_failed', 'generation_interrupted') for event in report['new_events'])) else 'attention'
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'started_at', 'finished_at',
        'all_health_checks_passed', 'active_jobs_with_multiple_updates')}, ensure_ascii=False))
    print('report=' + args.report)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Read-only verification failed: ' + type(exc).__name__)
        raise SystemExit(1) from None
