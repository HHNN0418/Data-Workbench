"""Finish the storage regression in independent, isolated test processes."""
import argparse
import faulthandler
import unittest


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--part', type=int, required=True, choices=range(3))
    args = parser.parse_args()
    modules = {}
    for test in flatten(unittest.defaultTestLoader.discover('tests')):
        module = test.id().split('.')[0]
        if (module >= 'test_agent_quotas' or module.startswith(('test_agent_memory', 'test_agent_generation'))
                or module in ('test_ai_routes', 'test_agent_heartbeat_storage')):
            modules.setdefault(module, []).append(test)
    bins, counts = [[], [], []], [0, 0, 0]
    for module, tests in sorted(modules.items(), key=lambda row: (-len(row[1]), row[0])):
        part = min(range(3), key=lambda index: counts[index])
        bins[part].append((module, tests))
        counts[part] += len(tests)
    selected = [test for _, tests in sorted(bins[args.part]) for test in tests]
    print('partition', args.part, 'tests', len(selected), 'modules', [name for name, _ in sorted(bins[args.part])], flush=True)
    faulthandler.dump_traceback_later(120, repeat=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    raise SystemExit(not result.wasSuccessful())


if __name__ == '__main__':
    main()
