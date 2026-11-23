"""Demand-driven counter turns, stored inside the existing coordinator lock.

Only callers waiting for permission compete. A route doing HTTP or Feed work
holds no share; other routes can use that capacity immediately. A short,
adaptive handoff prevents fast local polling from overtaking remote workers.
This module never grants or reserves a future endpoint budget slot.
"""
import math

MIN_HANDOFF = .15
MAX_HANDOFF = .6
DEMAND_TTL = 2.0
PROFILE_TTL = 60.0
MAX_ACTORS = 64
MAX_BYPASSES = 2


def _number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _state(state, now):
    allocation = state.get('allocation')
    if not isinstance(allocation, dict) or allocation.get('version') != 1:
        allocation = {'version': 1, 'mode': 'ready_queue'}
        state['allocation'] = allocation
    profiles = allocation.get('actors', {})
    profiles = profiles if isinstance(profiles, dict) else {}
    profiles = {actor: profile for actor, profile in profiles.items()
                if isinstance(profile, dict) and _number(profile.get('seen_at'))
                and 0 <= now-profile['seen_at'] <= PROFILE_TTL}
    profiles = dict(sorted(profiles.items(), key=lambda item: item[1]['seen_at'],
                           reverse=True)[:MAX_ACTORS])
    for profile in profiles.values():
        grace = profile.get('grace')
        profile['grace'] = min(MAX_HANDOFF, max(MIN_HANDOFF, grace)) if _number(grace) else MIN_HANDOFF
    allocation['actors'] = profiles
    waiting, seen = [], set()
    candidates = allocation.get('waiting', [])
    for row in candidates if isinstance(candidates, list) else []:
        if (isinstance(row, dict) and isinstance(row.get('actor'), str)
                and isinstance(row.get('path'), str) and _number(row.get('seen_at'))
                and 0 <= now-row['seen_at'] <= DEMAND_TTL and row['actor'] not in seen):
            waiting.append(row)
            bypassed = row.get('bypassed', 0)
            row['bypassed'] = min(MAX_BYPASSES, max(0, bypassed)) if isinstance(bypassed, int) else 0
            seen.add(row['actor'])
        if len(waiting) >= MAX_ACTORS:
            break
    allocation['waiting'] = waiting
    turn = allocation.get('turn')
    if (not isinstance(turn, dict) or not _number(turn.get('until')) or not waiting
            or turn.get('actor') != waiting[0]['actor']):
        allocation.pop('turn', None)
    return allocation


def discard(state, actor, path=None):
    """Withdraw one abandoned/ineligible request without cancelling another."""
    allocation = state.get('allocation')
    if not isinstance(allocation, dict):
        return False
    waiting = allocation.get('waiting', [])
    if not isinstance(waiting, list):
        return False
    kept = [row for row in waiting if isinstance(row, dict) and not (row.get('actor') == actor
            and (path is None or row.get('path') == path))]
    if kept == waiting:
        return False
    allocation['waiting'] = kept
    profiles = allocation.get('actors', {})
    profile = profiles.get(actor) if isinstance(profiles, dict) else None
    if isinstance(profile, dict):
        profile.pop('retry_at', None)
    turn = allocation.get('turn')
    if not kept or not isinstance(turn, dict) or turn.get('actor') != kept[0].get('actor'):
        allocation.pop('turn', None)
    return True


def reset(state):
    """A server cooldown invalidates readiness; no queued catch-up burst."""
    allocation = state.get('allocation')
    if isinstance(allocation, dict):
        allocation['waiting'] = []
        allocation.pop('turn', None)
        profiles = allocation.get('actors', {})
        for profile in profiles.values() if isinstance(profiles, dict) else []:
            if isinstance(profile, dict):
                profile.pop('retry_at', None)


def request_turn(state, actor, path, now, not_before):
    """Return a retry response, or None when this ready caller has its turn.

    The caller must still enforce endpoint pacing and create the object lease
    atomically. Failed polls only record demand, never advance ``next_at``.
    """
    allocation = _state(state, now)
    profiles, waiting = allocation['actors'], allocation['waiting']
    profile = profiles.setdefault(actor, {'seen_at': now, 'grace': MIN_HANDOFF})
    grace = profile.get('grace', MIN_HANDOFF)
    grace = min(MAX_HANDOFF, max(MIN_HANDOFF, grace)) if _number(grace) else MIN_HANDOFF
    retry_at = profile.pop('retry_at', None)
    poll_lag = profile.get('poll_lag', 0)
    poll_lag = min(MAX_HANDOFF, max(0, poll_lag)) if _number(poll_lag) else 0
    if _number(retry_at) and retry_at <= now < retry_at+DEMAND_TTL:
        # Learn control/poll lateness, not remote HTTP latency. Retain profiles
        # after a missed turn so a consistently late worker can recover.
        poll_lag = min(MAX_HANDOFF, .5*poll_lag+.5*(now-retry_at))
        grace = min(MAX_HANDOFF, max(MIN_HANDOFF, poll_lag+.05))
    profile.update(seen_at=now, grace=grace, poll_lag=poll_lag)
    row = next((row for row in waiting if row['actor'] == actor), None)
    if row is None:
        row = {'actor': actor, 'path': path, 'seen_at': now, 'bypassed': 0}
        waiting.append(row)
    else:
        row.update(path=path, seen_at=now)

    def retry(reason, seconds):
        delay = max(50, int(seconds*1000)+1)
        profile['retry_at'] = now+delay/1000
        return {'allowed': False, 'retry_after_ms': delay,
                'bucket': 'counters', 'reason': reason}

    while waiting:
        head = waiting[0]['actor']
        turn = allocation.get('turn')
        if not turn or turn['actor'] != head:
            head_grace = profiles.get(head, {}).get('grace', MIN_HANDOFF)
            allocation['turn'] = turn = {'actor': head,
                'until': max(now, not_before)+head_grace}
        if now < not_before:
            # Wake a remote caller before the slot opens to account for its
            # measured control round trip. Admission still checks next_at.
            return retry('pacing', max(.05, not_before-now-poll_lag))
        if head == actor:
            waiting.pop(0)
            allocation.pop('turn', None)
            return None
        ahead = waiting[:waiting.index(row)]
        if all(item.get('bypassed', 0) < MAX_BYPASSES for item in ahead):
            # Use otherwise idle capacity, but never let fast local polling
            # overtake a waiting route indefinitely. Slow routes do not impose
            # equal request counts on routes that can finish more work.
            for item in ahead:
                item['bypassed'] = item.get('bypassed', 0)+1
            waiting.remove(row)
            allocation.pop('turn', None)
            return None
        if now < turn['until']:
            return retry('allocation', min(.05, turn['until']-now))
        # Missing workers cannot hold a slot indefinitely. Their later poll
        # rejoins the queue and updates the learned handoff tolerance.
        waiting.pop(0)
        allocation.pop('turn', None)

