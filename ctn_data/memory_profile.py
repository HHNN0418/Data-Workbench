"""Which objects fill the backend's memory (GET /api/diagnostics/memory).

On 24 Oct the backend's private memory grew about 215 MB an hour (1,686 MB at
09:00 UTC, 2,279 MB at 11:46) and no unbounded cache could be found in the code.
Two reads some hours apart, compared, show which object types and caches grow.

Read-only: it changes no application state. Counting walks every object the
garbage collector tracks (containers; not str, bytes or numbers), which can take
a second or two on a large heap. tracemalloc stays off unless asked: ``start``
traces allocations from then on (at a cost), ``top`` lists the sites holding the
most memory allocated since the start, ``stop`` ends tracing.
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import tracemalloc
from collections import Counter

TOP_TYPES = 30
TOP_ALLOCATIONS = 25
TRACE_ACTIONS = ('', 'start', 'top', 'stop')
TRACE_FRAMES = 1
# Process-wide caches of our own modules, by module and attribute; only modules already loaded are read.
KNOWN_CACHES = (
    ('account_status', '_cache'), ('agent_opportunities', '_cache'), ('agent_targeting', '_cache'),
    ('copy_patterns', '_CACHE'), ('fact_bank', '_CACHE'), ('local_rag', '_VECTOR_CACHE'),
    ('npc_calibration', '_cache'), ('proxy_status', '_cache'), ('stance_labels', '_mode_cache'),
    ('stance_labels', '_requested'), ('stance_labels', '_watched'), ('vote_tracker', '_labels_cache'),
)
MB = 1024 * 1024


def _mb(value):
    return round(value / MB, 1) if value is not None else None


def _process():
    """Working set and private bytes of this process (Windows), or None where unavailable."""
    if os.name != 'nt':
        return {'working_set_mb': None, 'private_mb': None}
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                    ('PrivateUsage', ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        return {'working_set_mb': None, 'private_mb': None}
    return {'working_set_mb': _mb(counters.WorkingSetSize), 'private_mb': _mb(counters.PrivateUsage),
            'peak_working_set_mb': _mb(counters.PeakWorkingSetSize)}


def _type_name(kind):
    module = getattr(kind, '__module__', 'builtins')
    return kind.__qualname__ if module == 'builtins' else f'{module}.{kind.__qualname__}'


def _types():
    counts, sizes = Counter(), Counter()
    objects = gc.get_objects()
    try:
        for value in objects:
            kind = type(value)
            counts[kind] += 1
            try:
                sizes[kind] += sys.getsizeof(value)
            except TypeError:
                pass
        total = len(objects)
    finally:
        del objects
    return total, [{'type': _type_name(kind), 'count': count, 'shallow_mb': _mb(sizes[kind])}
                   for kind, count in counts.most_common(TOP_TYPES)]


def _caches():
    found = {}
    for module_name, attribute in KNOWN_CACHES:
        module = sys.modules.get('ctn_data.' + module_name)
        value = getattr(module, attribute, None) if module is not None else None
        if value is not None and hasattr(value, '__len__'):
            found[f'{module_name}.{attribute}'] = len(value)
    return found


def _tracemalloc(action):
    if action == 'start' and not tracemalloc.is_tracing():
        tracemalloc.start(TRACE_FRAMES)
    elif action == 'stop' and tracemalloc.is_tracing():
        tracemalloc.stop()
    result = {'tracing': tracemalloc.is_tracing()}
    if result['tracing']:
        current, peak = tracemalloc.get_traced_memory()
        result.update(traced_mb=_mb(current), peak_mb=_mb(peak))
        if action == 'top':
            stats = tracemalloc.take_snapshot().statistics('lineno')[:TOP_ALLOCATIONS]
            result['top'] = [{'where': f'{os.path.basename(stat.traceback[0].filename)}:{stat.traceback[0].lineno}',
                              'size_mb': _mb(stat.size), 'count': stat.count} for stat in stats]
    return result


def snapshot(trace=''):
    """Process memory, the most numerous object types and our caches; ``trace`` drives tracemalloc."""
    if trace not in TRACE_ACTIONS:
        raise ValueError('trace 只能是 start、top 或 stop')
    tracked, types = _types()
    return {'pid': os.getpid(), 'threads': threading.active_count(), 'process': _process(),
            'gc': {'tracked_objects': tracked, 'generations': list(gc.get_count())},
            'types': types, 'caches': _caches(), 'tracemalloc': _tracemalloc(trace)}
