"""Shift schedule for automatic roles (D37).

Roles are split into groups that take turns, so the team is always online
while each account rests at least six hours in a row. Citizens are most active
07:00-22:00 Sydney; one whole group (two accounts) covers every hour of that,
and only one group, at a lower rate, works the night.

Default (Sydney time, D40):
  A (Maya, Leah)  06:00-13:00 and 16:00-22:00, at most 8 per hour; rests 22:00-06:00
  B (Ethan, Nora) 12:00-17:00, at most 8 per hour, and 21:00-06:00, 4 per hour; rests 06:00-12:00
Neighbouring shifts overlap by one hour. max_per_hour is a hard cap. Output,
and with the 2:1 mix the original posts, is weighted toward the citizen peaks
(06-08, 09-11, 12-14): a day shift runs at its cap during a peak and at
off_peak_per_hour otherwise. A shift without off_peak_per_hour (the night)
always runs at its cap. Any number of groups works; accounts in no group are
not limited. settings['shift_schedule'] overrides the default with the same shape.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SETTINGS_KEY = 'shift_schedule'
DEFAULT_SCHEDULE = {
    'timezone': 'Australia/Sydney',
    # A: Bev, Leah, Grace, Priya, Daniel; B: Ethan, Nora, Tomás, Hemi, Simon (new roles joined on 22 Oct);
    # C: Ruth, Sam, Lena, Marcus, Ella (D59, the user raised the role limit to 15). Bev (10783) took
    # Maya's (5554) place after Maya's account was suspended on 22 Oct (D64, D65).
    # D: Callum, Fiona, Rob, Aisha, Kerry (accounts connected 23 Oct, D73). Isaac (11563) took
    # Ethan's (5832) place in B after Ethan's account was suspended on 23 Oct (D81).
    # E: Mele, Owen, Gareth, Rosa, Tia (accounts connected 23 Oct, D85). Peg (11674, a retiree who is
    # up early) joined A and Sefa (11676, a night-shift taxi driver) joined B the same evening.
    # Colin (11746, an early-run ferry engineer) took Peg's place in A after Peg's account was
    # suspended later that night (D88).
    'groups': {'A': [10783, 6464, 10242, 10264, 10276, 11746], 'B': [11563, 6494, 10243, 10275, 6503, 11676],
               'C': [10436, 10437, 10438, 10439, 10440], 'D': [11252, 11255, 11256, 11259, 11260],
               'E': [11625, 11629, 11631, 11633, 11639]},
    'shifts': [
        {'group': 'A', 'start': '06:00', 'end': '13:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        {'group': 'B', 'start': '12:00', 'end': '17:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        {'group': 'A', 'start': '16:00', 'end': '22:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        {'group': 'B', 'start': '21:00', 'end': '06:00', 'max_per_hour': 4},
        # C covers the 09-11 and 12-14 peaks and the evening, 12 hours a day.
        {'group': 'C', 'start': '08:00', 'end': '14:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        {'group': 'C', 'start': '17:00', 'end': '23:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        # D covers the hours with most NPC writes per group on shift (20-23 Oct): 14-16 had B alone
        # at 224-254 writes an hour, 06-08 had A alone at up to 199. Rests 21:00-05:00.
        {'group': 'D', 'start': '05:00', 'end': '10:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        {'group': 'D', 'start': '14:00', 'end': '21:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        # E fills the hours with two groups or fewer on shift (10-12, 13-16) and the night
        # B covered alone (22-02). Rests 02:00-10:00 and 16:00-22:00.
        {'group': 'E', 'start': '10:00', 'end': '16:00', 'max_per_hour': 8, 'off_peak_per_hour': 6},
        {'group': 'E', 'start': '22:00', 'end': '02:00', 'max_per_hour': 4},
    ],
    # Best hours for originals (deep-analysis README): full rate then, less in between.
    'peaks': [{'start': '06:00', 'end': '08:00'}, {'start': '09:00', 'end': '11:00'},
              {'start': '12:00', 'end': '14:00'}],
}
# A reply generated just before a shift ends may still go out shortly after.
PUBLISH_GRACE_SECONDS = 900
_COUNTED_STATES = ('pending', 'sending', 'uncertain', 'published')


def configuration(data):
    """The stored schedule, or the default; an invalid stored value falls back to the default."""
    stored = ((data or {}).get('settings') or {}).get(SETTINGS_KEY)
    try:
        return validate(stored) if stored else DEFAULT_SCHEDULE
    except ValueError:
        return DEFAULT_SCHEDULE


def _minutes(value):
    hours, minutes = str(value).split(':')
    hours, minutes = int(hours), int(minutes)
    if not (0 <= hours <= 24 and 0 <= minutes < 60) or hours * 60 + minutes > 1440:
        raise ValueError('time must be HH:MM')
    return hours * 60 + minutes


def validate(schedule):
    """Check shape and values; return a normalised copy or raise ValueError."""
    if not isinstance(schedule, dict):
        raise ValueError('schedule must be an object')
    ZoneInfo(str(schedule.get('timezone', DEFAULT_SCHEDULE['timezone'])))
    groups = schedule.get('groups')
    if not isinstance(groups, dict) or not groups:
        raise ValueError('groups are required')
    members = [account for accounts in groups.values() for account in accounts]
    if any(type(account) is not int for account in members) or len(members) != len(set(members)):
        raise ValueError('each account id appears in at most one group')
    shifts = schedule.get('shifts')
    if not isinstance(shifts, list) or not shifts:
        raise ValueError('shifts are required')
    for shift in shifts:
        if shift.get('group') not in groups:
            raise ValueError('shift group is unknown')
        _minutes(shift.get('start')), _minutes(shift.get('end'))
        if type(shift.get('max_per_hour')) is not int or shift['max_per_hour'] < 1:
            raise ValueError('max_per_hour must be a positive integer')
        off_peak = shift.get('off_peak_per_hour')
        if off_peak is not None and (type(off_peak) is not int or not 1 <= off_peak <= shift['max_per_hour']):
            raise ValueError('off_peak_per_hour must be an integer from 1 to max_per_hour')
    peaks = schedule.get('peaks', [])
    if not isinstance(peaks, list):
        raise ValueError('peaks must be a list')
    for peak in peaks:
        _minutes(peak.get('start')), _minutes(peak.get('end'))
    keys = ('group', 'start', 'end', 'max_per_hour', 'off_peak_per_hour')
    return {'timezone': str(schedule.get('timezone', DEFAULT_SCHEDULE['timezone'])),
            'groups': {name: list(accounts) for name, accounts in groups.items()},
            'shifts': [{key: shift[key] for key in keys if shift.get(key) is not None} for shift in shifts],
            'peaks': [{'start': peak['start'], 'end': peak['end']} for peak in peaks]}


def _group(schedule, account_id):
    return next((name for name, accounts in schedule['groups'].items() if account_id in accounts), None)


def _covers(shift, minute):
    start, end = _minutes(shift['start']), _minutes(shift['end'])
    return start <= minute < end if start < end else (minute >= start or minute < end)


def current_shift(schedule, account_id, now):
    """The account's active shift at ``now`` (epoch seconds), or None when off shift.

    Raises LookupError for an account in no group, which the schedule does not limit.
    """
    group = _group(schedule, account_id)
    if group is None:
        raise LookupError(account_id)
    local = datetime.fromtimestamp(now, ZoneInfo(schedule['timezone']))
    minute = local.hour * 60 + local.minute
    active = [shift for shift in schedule['shifts'] if shift['group'] == group and _covers(shift, minute)]
    # Overlapping shifts of one group: the more generous rate applies.
    return max(active, key=lambda shift: shift['max_per_hour']) if active else None


def next_start(schedule, account_id, now):
    """Epoch seconds when the account's next shift starts (searching one week ahead)."""
    group = _group(schedule, account_id)
    zone = ZoneInfo(schedule['timezone'])
    local = datetime.fromtimestamp(now, zone)
    best = None
    for days in range(8):
        day = (local + timedelta(days=days)).date()
        for shift in schedule['shifts']:
            if shift['group'] != group:
                continue
            minutes = _minutes(shift['start'])
            start = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=minutes)
            if start.timestamp() > now and (best is None or start.timestamp() < best):
                best = start.timestamp()
        if best is not None:
            break
    return best if best is not None else now + 3600


def _stamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0


def gate(data, agent, now):
    """None when the role may decide now, else (reason, retry_at).

    Off shift: wait for the next shift. On shift: at most max_per_hour posts
    and replies in the last hour, counting drafts not yet sent.
    """
    schedule = configuration(data)
    try:
        shift = current_shift(schedule, agent.get('account_id'), now)
    except LookupError:
        return None
    if shift is None:
        return 'off_shift', next_start(schedule, agent.get('account_id'), now)
    limit = hourly_limit(schedule, shift, now)
    recent = sorted(_stamp(d.get('created_at')) for d in (data or {}).get('drafts', {}).values()
                    if d.get('agent_id') == agent.get('id') and d.get('action') in ('post', 'reply')
                    and d.get('state') in _COUNTED_STATES and now - _stamp(d.get('created_at')) < 3600)
    if len(recent) >= limit:
        return 'hourly_limit', recent[len(recent) - limit] + 3600
    return None


def hourly_limit(schedule, shift, now):
    """The shift's cap during a peak, else its off-peak rate; never above max_per_hour."""
    off_peak = shift.get('off_peak_per_hour')
    if off_peak is None:
        return shift['max_per_hour']
    local = datetime.fromtimestamp(now, ZoneInfo(schedule['timezone']))
    minute = local.hour * 60 + local.minute
    peak = any(_covers(window, minute) for window in schedule.get('peaks', []))
    return shift['max_per_hour'] if peak else min(off_peak, shift['max_per_hour'])


def is_peak(schedule, now):
    """True inside one of the schedule's citizen peaks (06-08, 09-11, 12-14 Sydney by default)."""
    local = datetime.fromtimestamp(now, ZoneInfo(schedule['timezone']))
    minute = local.hour * 60 + local.minute
    return any(_covers(window, minute) for window in schedule.get('peaks', []))


def pacing(data, agent, now):
    """A read-only view of the role's shift for the decide step; it changes no limit."""
    schedule = configuration(data)
    result = {'peak_hour': is_peak(schedule, now)}
    try:
        shift = current_shift(schedule, agent.get('account_id'), now)
    except LookupError:
        return result
    if shift is None:
        return {**result, 'on_shift': False}
    used = sum(1 for d in (data or {}).get('drafts', {}).values()
               if d.get('agent_id') == agent.get('id') and d.get('action') in ('post', 'reply')
               and d.get('state') in _COUNTED_STATES and now - _stamp(d.get('created_at')) < 3600)
    return {**result, 'on_shift': True, 'hour_limit': hourly_limit(schedule, shift, now), 'used_last_hour': used}


def may_publish(data, agent, now):
    """True when the role is on shift now or was within the publish grace period."""
    schedule = configuration(data)
    try:
        return (current_shift(schedule, agent.get('account_id'), now) is not None
                or current_shift(schedule, agent.get('account_id'), now - PUBLISH_GRACE_SECONDS) is not None)
    except LookupError:
        return True
