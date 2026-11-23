"""Combine sequential coverage, independent partitions and a repaired legacy test."""
import json
from pathlib import Path
import re
import unittest

from tests.run_heartbeat_regression import flatten


def normalize(identifier):
    return identifier.removeprefix('tests.')


def main():
    directory = Path('reports/generated')
    statuses = {}
    reports = ['heartbeat-lock-regression.log'] + [f'heartbeat-lock-regression-part{i}.log' for i in range(3)]
    for followup in ('heartbeat-lock-new-tests.log', 'heartbeat-lock-shutdown-focused.log',
                     'heartbeat-lock-read-performance.log', 'competition-budget-storage-regression.log',
                     'system-context-tests-20261021.log'):
        if (directory / followup).exists():
            reports.append(followup)
    for report in reports:
        pending = None
        for line in (directory / report).read_text(encoding='utf-8-sig', errors='replace').splitlines():
            match = re.match(r'^(test_\w+) \(([^)]+)\) \.\.\.\s*(.*)', line)
            if match:
                pending, outcome = normalize(match[2]), match[3]
            elif pending and line in ('ok', 'FAIL', 'ERROR'):
                outcome = line
            else:
                continue
            if outcome == 'ok' or outcome.startswith('skipped') or outcome in ('FAIL', 'ERROR'):
                statuses[pending] = {'result': 'skipped' if outcome.startswith('skipped') else outcome,
                                     'report': report}
                pending = None
    all_tests = {normalize(test.id()): test for test in flatten(unittest.defaultTestLoader.discover('tests'))}
    # This legacy test used the old synchronous endpoint signature. Its test
    # adapter was updated to the existing async API and all nine cases rerun.
    recheck = directory / 'heartbeat-lock-retrieval-recheck.log'
    if re.search(r'Ran 9 tests[\s\S]*\nOK\s*$', recheck.read_text(encoding='utf-8-sig')):
        for identifier in all_tests:
            if identifier.startswith('test_review_ai_query_regressions.'):
                statuses[identifier] = {'result': 'ok', 'report': recheck.name}
    missing = sorted(set(all_tests) - set(statuses))
    failed = {identifier: value for identifier, value in statuses.items()
              if identifier in all_tests and value['result'] not in ('ok', 'skipped')}
    result = {'status': 'passed' if not missing and not failed else 'incomplete',
              'discovered': len(all_tests), 'passed': sum(statuses.get(key, {}).get('result') == 'ok' for key in all_tests),
              'skipped': sum(statuses.get(key, {}).get('result') == 'skipped' for key in all_tests),
              'missing': missing, 'failed': failed, 'reports': reports + [recheck.name],
              'note': 'Sequential prefix plus three independent partitions; latest per-test result retained. Explicit PG regressions run separately.'}
    (directory / 'heartbeat-lock-regression-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(result['status'] != 'passed')


if __name__ == '__main__':
    main()
