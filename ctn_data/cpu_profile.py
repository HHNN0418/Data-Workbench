"""Which threads use the backend's CPU, and doing what (GET /api/diagnostics/cpu).

On 23 Oct the backend process ran at 101% of one core with 33 threads: work
that takes 0.2 s inside the agent store lock took seconds waiting for the
interpreter (D90). This samples every thread's stack while measuring each
thread's CPU time, so the heavy work can be found on the live process without
restarting it. Read-only; it changes nothing.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter, defaultdict

MAX_SECONDS = 120
INTERVAL = 0.02
# Innermost Python functions that mean the thread is waiting, not computing.
WAITING = {'wait', 'acquire', 'sleep', 'select', 'recv', 'recv_into', 'read', 'readinto', 'accept', 'poll',
           '_wait_for_tstate_lock', 'get', 'join', 'getaddrinfo', 'create_connection', 'connect', 'readline',
           '_recv_bytes', 'do_handshake', 'execute', 'fetchone', 'fetchall', 'fetchmany', 'commit'}
STACK_DEPTH = 6


def _thread_cpu(native_id):
    """CPU seconds a thread has used (Windows), or None when unavailable."""
    if os.name != 'nt' or not native_id:
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenThread(0x0800, False, native_id)  # THREAD_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetThreadTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        kernel, user = times[2], times[3]
        to_seconds = lambda t: ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7
        return to_seconds(kernel) + to_seconds(user)
    finally:
        kernel32.CloseHandle(handle)


def _stack(frame):
    names = []
    while frame is not None and len(names) < 40:
        code = frame.f_code
        names.append((os.path.basename(code.co_filename), code.co_name, frame.f_lineno))
        frame = frame.f_back
    return names  # innermost first


def profile(seconds=30, interval=INTERVAL):
    seconds = max(1.0, min(float(seconds), MAX_SECONDS))
    me = threading.get_ident()
    threads = {t.ident: t for t in threading.enumerate()}
    start = {ident: _thread_cpu(getattr(t, 'native_id', None)) for ident, t in threads.items()}
    wall = time.monotonic()
    samples, busy, stacks = Counter(), Counter(), defaultdict(Counter)
    while time.monotonic() - wall < seconds:
        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            names = _stack(frame)
            samples[ident] += 1
            if names and names[0][1] in WAITING:
                continue
            busy[ident] += 1
            # Innermost frames of our own code identify the work best.
            ours = [n for n in names if not n[0].startswith(('threading', 'queue', 'selectors'))][:STACK_DEPTH]
            stacks[ident][' < '.join(f'{f}:{fn}:{line}' for f, fn, line in ours)] += 1
        time.sleep(interval)
    elapsed = time.monotonic() - wall
    rows = []
    for ident, thread in threads.items():
        if ident == me:
            continue
        before, after = start.get(ident), _thread_cpu(getattr(thread, 'native_id', None))
        cpu = (after - before) if before is not None and after is not None else None
        rows.append({'thread': thread.name, 'cpu_seconds': round(cpu, 2) if cpu is not None else None,
                     'cpu_share': round(cpu / elapsed, 3) if cpu is not None else None,
                     'busy_samples': busy[ident], 'samples': samples[ident],
                     'top_stacks': [{'stack': stack, 'samples': n} for stack, n in stacks[ident].most_common(3)]})
    rows.sort(key=lambda row: (row['cpu_seconds'] is None, -(row['cpu_seconds'] or 0), -row['busy_samples']))
    total = sum(row['cpu_seconds'] or 0 for row in rows)
    return {'seconds': round(elapsed, 1), 'threads': len(rows), 'cpu_seconds_total': round(total, 2),
            'cores_used': round(total / elapsed, 2), 'by_thread': rows}
