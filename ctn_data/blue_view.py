"""Read-only view of Blue report candidates (GET /api/blue/candidates).

Wraps blue_reports.candidates/daily_plan with public GET availability checks,
post links and a suggested English report note, and groups the candidates by
account ('accounts'): each author's posts, its account-level evidence, its
hidden posts from risk_assessment and whether an account report is ready.
Nothing here submits a report. Computing candidates takes
~10 s on live data, so results are kept for five minutes and refreshed in the
background while the previous result is served.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import blue_availability, blue_reports
from .db import utc_now

LOGGER = logging.getLogger(__name__)

CACHE_SECONDS = 300
POST_URL = 'https://social.legitreal.com/twoot/{}'
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# The platform's report form: this reason earns Blue points, and its detail
# field suggests this opening. Notes stay short enough to paste.
SUGGESTED_REASON = 'Inauthentic (bot) / Spam'
NOTE_OPENING = 'I think that this content is inauthentic (bot) activity because '
ACCOUNT_NOTE_OPENING = 'I think that this account is inauthentic (bot) activity because '
MAX_NOTE_CHARS = 300
# Evidence about one post rather than its author; an account's card and its report leave these out.
POST_ONLY_RULES = frozenset({'shared_text_post', 'cotimed_post', 'chain_text', 'deep_post', 'answer_latency'})
# The account-report rule: suspended at the ruling if a moderator already hid one of its posts
# and it has 100 platform actions; otherwise dismissed (no points, no suspension) and it may be
# reported again. The ruling comes at most 3 h after the first report, so a repeat waits 6 h.
ACCOUNT_REPORT_MIN_HIDDEN = 1
ACCOUNT_REPORT_MIN_ACTIONS = 100
ACCOUNT_REREPORT_HOURS = 6
# Three hidden posts suspend an account on their own, so an account report pays nothing. The
# platform does not show suspension: a suspended account's profile still answers 200 (Maya,
# Ethan and Peg, 24 Oct), so the confirmed hidden count is the only sign.
SUSPENSION_HIDDEN = 3
CAVEATS = ['p 值是按规则估计的，尚未用官方裁定校准',
           '误报（被裁定为真人）会让全队暂停举报 6 小时',
           '同一队伍的举报每天前 5 条全额，之后减半，再之后为 10%；计划已按簇控制在全额以内',
           '立场按关键词从簇内帖子估计：Yes 方是我方，举报 Yes 方的机器人会削弱我方，所以今日建议先排 No 方',
           '恶意或轻率举报违反平台规则；每一条都由你在本页确认后，才用 Simon 的账号提交，每条帖子只报一次',
           '账号举报：裁定时该账号已有 1 条帖子被版主隐藏、且平台动作满 100 次才会封号，每个举报的队各得 15 分；'
           '不满足就驳回，不得分也不罚停。隐藏帖来自风险评估（第一次 404 后隔一段时间再查仍是 404 才确认），分不清版主隐藏和作者自删；'
           '动作数是采集到的发帖、回复和转发，是下限。账号举报和帖子举报一起计入同一个簇的当天全额名额，先报帖子']

# Plain-language reasons per evidence rule; weak or informational rules are left out.
NOTES = {
    'behaviour_bot_rule': lambda e: (f"the account answered {round(e['answer_rate'] * e['received'])} of the {e['received']} "
                                     f"replies it received within {blue_reports.BEHAVIOR_WINDOW_HOURS} h"),
    'deep_threads': lambda e: f"{e['deep_ratio']:.0%} of the account's posts sit 10 or more replies deep in threads",
    'fast_answers': lambda e: f"its median reply time is {e['median_latency_seconds']:.0f} seconds",
    'quick_answers': lambda e: f"its median reply time is {e['median_latency_seconds']:.0f} seconds",
    'shared_text_post': lambda e: (f"this exact text was posted by {e['accounts']} accounts within "
                                   f"{blue_reports.CANDIDATE_WINDOW_HOURS} h"),
    'shared_text_author': lambda e: (f"the account posted {e['texts']} other text(s) copied word for word by "
                                     f"{blue_reports.SHARED_MIN_ACCOUNTS}+ accounts"),
    'cotimed_post': lambda e: f"other accounts posted the same text within {blue_reports.COTIME_SECONDS // 60} minutes",
    'cotimed_author': lambda e: (f"the account repeatedly posts the same text within {blue_reports.COTIME_SECONDS // 60} "
                                 f"minutes of other accounts"),
    'self_repeat_strong': lambda e: (f"the account posted one identical text {e['repeats']} times in "
                                     f"{blue_reports.CANDIDATE_WINDOW_HOURS} h"),
    'self_repeat': lambda e: f"the account posted one identical text {e['repeats']} times in {blue_reports.CANDIDATE_WINDOW_HOURS} h",
    'style_group': lambda e: f"it writes in the same style as a group of {e['group_size']} accounts",
    'style_group_hidden': lambda e: (f"it writes in the same style as a group of {e['group_size']} accounts, "
                                     f"{e['others_hidden']} of which already had posts removed"),
}
TEAMMATE_NOTE = '与我方文风同组，可能是未登记的队友'


def report_note(evidence, opening=NOTE_OPENING):
    """An English report detail (<= 300 chars), strongest evidence first."""
    reasons = []
    for item in sorted(evidence, key=lambda item: -item.get('log_odds', 0)):
        note = NOTES.get(item.get('rule'))
        if note:
            try:
                reasons.append(note(item))
            except (KeyError, TypeError, ValueError):
                continue
    note = None
    for reason in reasons:
        candidate = (note[:-1] + '; ' if note else opening) + reason + '.'
        if len(candidate) > MAX_NOTE_CHARS:
            break
        note = candidate
    return note


def build(db_path, *, limit=DEFAULT_LIMIT, isolated=False):
    """The view; with ``isolated`` the CPU-heavy candidates() runs in a child process (blue_reports.candidates_isolated).

    The availability GETs and the account merge stay here. 'timing' records each
    part's seconds, and candidates' own stages, for GET /api/blue/candidates.
    """
    started = time.monotonic()
    if isolated:
        items, stages = blue_reports.candidates_isolated(db_path, limit=limit)
    else:
        stages = {'isolated': False}
        items = blue_reports.candidates(db_path, limit=limit, timing=stages)
    timing = {'candidates': stages, 'isolated': bool(isolated), 'candidates_seconds': round(time.monotonic() - started, 3)}
    from .blue_submit import submitted, submitted_today
    reported = {entry.get('post_id') for entry in submitted(db_path)}
    items = [item for item in items if item['post_id'] not in reported]
    clock = time.monotonic()
    availability = blue_availability.check(db_path, [item['post_id'] for item in items])
    timing['availability_seconds'] = round(time.monotonic() - clock, 3)
    for item in items:
        verification = availability.get(item['post_id']) or {
            'status': 'unverified', 'http_status': None, 'error': '尚未确认平台可访问性', 'checked_at': utc_now()}
        item.update(platform_url=POST_URL.format(item['post_id']), local_url=f"/posts?post={item['post_id']}",
                    suggested_reason=SUGGESTED_REASON, suggested_note=report_note(item['evidence']),
                    availability=verification, availability_checked_at=verification['checked_at'])
    # A successful report may arrive while the public GETs are in flight.
    clock = time.monotonic()
    reports = submitted(db_path)
    _attach_accounts(db_path, items, reports)
    reported = {entry.get('post_id') for entry in reports}
    view = _view([item for item in items if item['post_id'] not in reported], submitted_today(db_path), timing)
    timing.update(accounts_seconds=round(time.monotonic() - clock, 3), total_seconds=round(time.monotonic() - started, 3),
                  finished_at=utc_now())
    LOGGER.info('Blue candidates rebuilt in %.1f s (candidates %.1f s%s, availability %.1f s, accounts %.1f s)',
                timing['total_seconds'], timing['candidates_seconds'], ', child process' if isolated else '',
                timing['availability_seconds'], timing['accounts_seconds'])
    return view


def _attach_accounts(db_path, items, reports):
    """Give each candidate its author's hidden posts (risk_assessment, confirmed by a second 404), last account report
    and copy evidence: copy_patterns' shared wording with style_groups' group under 'style' (D99/D100).

    Both modules only read their in-memory results; either may be empty before its first round.
    Our roles, team_accounts and verified accounts never get copy evidence, whatever the modules return.
    """
    from . import copy_patterns, style_groups
    from .db import connect
    from .risk_assessment import account_risk
    authors, risk, own, copy, style = {item.get('author_id') for item in items} - {None}, {}, set(), {}, {}
    if authors:
        conn = connect(db_path)
        try:
            try:
                risk = account_risk(conn, authors)
            except Exception as exc:
                # The Blue page still works; accounts then show no hidden posts and no ready account report.
                LOGGER.warning('Blue accounts: hidden posts unavailable (%s)', type(exc).__name__)
            own = blue_reports._own_accounts(conn) | {row[0] for row in conn.execute('SELECT account_id FROM team_accounts')}
        finally:
            conn.close()
        try:
            copy, style = copy_patterns.account_evidence(db_path, authors), style_groups.account_evidence(authors)
        except Exception as exc:
            LOGGER.warning('Blue accounts: copy or style evidence unavailable (%s)', type(exc).__name__)
    last = {}
    for entry in reports:
        author = entry.get('author_id')
        if entry.get('target') == 'account' and author in authors and \
                str(entry.get('at') or '') > str((last.get(author) or {}).get('at') or ''):
            last[author] = {key: entry.get(key) for key in ('at', 'day', 'status')}
    for item in items:
        author = item.get('author_id')
        item['author_risk'] = risk.get(author, {'hidden': 0, 'suspect': 0, 'last_confirmed_at': None})
        item['author_account_report'] = last.get(author)
        evidence = None
        if author not in own and item.get('verified') is not True and (author in copy or author in style):
            evidence = {**copy.get(author, {}), 'style': style.get(author)}
        item['author_copy'] = evidence


def _recent(at, now, hours):
    try:
        return now - datetime.fromisoformat(str(at).replace('Z', '+00:00')) < timedelta(hours=hours)
    except ValueError:
        return False


def _accounts(candidates, issues, plan, reports_today, now=None):
    """Candidates grouped by author, each with its posts and the account report it is ready for (or what it lacks).

    Account and post reports share the full-rate places for the day of a text
    cluster and, separately, of a style group, and planned post reports (25
    points to the first team) keep theirs first.
    """
    now = now or datetime.now(timezone.utc)
    planned = {item['post_id']: item for item in plan.get('items', [])}
    skipped = {item['post_id']: item.get('reason') for item in plan.get('skipped', [])}
    used = defaultdict(int)
    for entry in [*reports_today, *planned.values()]:
        for group in (entry.get('cluster_id'), entry.get('style_group')):
            if group is not None:
                used[str(group)] += 1
    rows = {}
    for item, issue in [*((item, False) for item in candidates), *((item, True) for item in issues)]:
        author = item.get('author_id')
        if author is None:
            continue
        row = rows.get(author)
        if row is None:
            row = rows[author] = {'account_id': author, 'p': item['p'], 'evidence': {}, 'posts': [],
                                  'risk': item.get('author_risk') or {'hidden': 0, 'suspect': 0, 'last_confirmed_at': None},
                                  'last_account_report': item.get('author_account_report'),
                                  'copy_evidence': item.get('author_copy'),
                                  **{key: item.get(key) for key in ('username', 'cluster_id', 'cluster_size', 'side',
                                                                    'side_counts', 'verified', 'observed_actions',
                                                                    'style_group', 'possible_teammate', 'linked_to_ours')}}
        row['p'] = max(row['p'], item['p'])
        post = {**planned.get(item['post_id'], item), 'availability_issue': issue, 'skip_reason': skipped.get(item['post_id'])}
        for key in ('author_risk', 'author_account_report', 'author_copy'):
            post.pop(key, None)
        row['posts'].append(post)
        for evidence in item.get('evidence') or []:
            kept = row['evidence'].get(evidence.get('rule'))
            if evidence.get('rule') not in POST_ONLY_RULES and (
                    kept is None or evidence.get('log_odds', 0) > kept.get('log_odds', 0)):
                row['evidence'][evidence['rule']] = evidence
    for row in rows.values():
        row['evidence'] = sorted(row['evidence'].values(), key=lambda item: -item.get('log_odds', 0))
        row['posts'].sort(key=lambda post: (post.get('plan_rank') is None, post.get('plan_rank') or 0,
                                            -post['p'], post['post_id']))
        row['planned'] = sum(1 for post in row['posts'] if post.get('plan_rank'))
        row['last_post_at'] = max((str(post.get('created_at') or '') for post in row['posts']), default=None) or None
        hidden, actions = int(row['risk'].get('hidden') or 0), int(row.get('observed_actions') or 0)
        note, last = report_note(row['evidence'], ACCOUNT_NOTE_OPENING), row['last_account_report']
        missing = ['possible_teammate'] if row.get('possible_teammate') else []
        if row['p'] < blue_reports.DEFAULT_THRESHOLD:
            missing.append('below_threshold')
        if row.get('verified') is not False:
            missing.append('verified_or_unknown')
        if hidden >= SUSPENSION_HIDDEN:
            missing.append('suspension_line')
        elif hidden < ACCOUNT_REPORT_MIN_HIDDEN:
            missing.append('hidden')
        if actions < ACCOUNT_REPORT_MIN_ACTIONS:
            missing.append('actions')
        if last and last.get('status') == 'submitted' and _recent(last.get('at'), now, ACCOUNT_REREPORT_HOURS):
            missing.append('recently_reported')
        if used[str(row['cluster_id'])] >= blue_reports.FULL_RATE_PER_CLUSTER:
            missing.append('cluster_daily_cap')
        if row.get('style_group') and used[str(row['style_group'])] >= blue_reports.FULL_RATE_PER_CLUSTER:
            missing.append('style_group_daily_cap')
        if not note:
            missing.append('no_note')
        row['account_report'] = {'eligible': not missing, 'missing': missing, 'suggested_reason': SUGGESTED_REASON,
                                 'suggested_note': note, 'hidden': hidden, 'suspect': int(row['risk'].get('suspect') or 0),
                                 'actions': actions}
        style = (row['copy_evidence'] or {}).get('style') or {}
        row['group_hidden_accounts'] = int(style.get('group_hidden_accounts') or 0)
        row['teammate_note'] = (style.get('teammate_note') or TEAMMATE_NOTE) if row.get('possible_teammate') else None
    # Yes-side accounts last (D49), then the accounts with something to report; then most hidden posts of
    # their own, most members of their style group with hidden posts, most actions (style research §6.2).
    return sorted(rows.values(), key=lambda row: (
        row.get('side') == 'yes', not (row['planned'] or row['account_report']['eligible']),
        -row['account_report']['hidden'], -row['group_hidden_accounts'], -row['account_report']['actions'],
        min((post['plan_rank'] for post in row['posts'] if post.get('plan_rank')), default=10**6),
        -row['p'], row['account_id']))


def _view(items, reports_today, timing=None):
    """Group checked candidates; only accessible posts can enter today's plan. ``timing`` is passed through.

    Probable unregistered teammates (style_groups possible_teammate) leave the
    candidates, the plan and every report button; their accounts are listed
    apart under 'excluded_teammates' with the reason.
    """
    teammates = [item for item in items if item.get('possible_teammate')]
    items = [item for item in items if not item.get('possible_teammate')]
    candidates = [item for item in items if item['availability']['status'] == 'available']
    issues = [item for item in items if item['availability']['status'] != 'available']
    summary = {status: sum(item['availability']['status'] == status for item in items)
               for status in ('available', 'unavailable', 'unverified')}
    checked_at = max((item['availability_checked_at'] for item in items), default=utc_now())
    plan = blue_reports.daily_plan(candidates, reports_today)
    accounts = _accounts(candidates, issues, plan, reports_today)
    excluded = _accounts(teammates, [], {'items': [], 'skipped': []}, reports_today)
    caveats = [*CAVEATS, f"候选已通过官方公开接口 GET 核验；404/410 无法访问 {summary['unavailable']} 条，"
               f"权限、限流或网络等原因未能确认 {summary['unverified']} 条，保留在下方待核对建议；"
               "可再次确认，只有恢复可访问后才进入今日建议，提交前还会再次核验"]
    return {'generated_at': utc_now(), 'day': blue_reports.sydney_day(), 'threshold': blue_reports.DEFAULT_THRESHOLD,
            'plan': plan, 'candidates': candidates, 'availability_issues': issues, 'caveats': caveats, 'submits_reports': False,
            'availability_checked_at': checked_at, 'availability_summary': summary, 'accounts': accounts,
            'excluded_teammates': excluded, 'teammate_items': teammates, 'timing': timing,
            'account_summary': {'accounts': len(accounts), 'with_plan': sum(1 for row in accounts if row['planned']),
                                'account_report_ready': sum(1 for row in accounts if row['account_report']['eligible']),
                                'excluded_teammates': len(excluded)}}


def _all_items(view):
    return [*view.get('candidates', []), *view.get('availability_issues', []), *view.get('teammate_items', [])]


class BlueCandidates:
    """One cached result per requested limit, refreshed in the background when stale.

    ``isolated`` (the backend) computes candidates in a child process. A failed
    background refresh keeps the previous result; its error is in 'refresh_error'.
    """
    def __init__(self, db_path, *, isolated=False):
        self.db_path, self.isolated = db_path, isolated
        self._lock = threading.Lock()
        self._recheck_lock = threading.Lock()
        self._refreshing = set()
        self._cached = {}
        self._revisions = {}
        self._errors = {}

    def _refresh(self, limit, revision):
        try:
            result = build(self.db_path, limit=limit, isolated=self.isolated)
            with self._lock:
                self._errors.pop(limit, None)
                if self._revisions.get(limit, 0) == revision:
                    self._cached[limit] = (time.monotonic(), result)
        except Exception as exc:
            LOGGER.warning('Blue candidates refresh failed (%s: %s); keeping the previous result',
                           type(exc).__name__, str(exc)[:200])
            with self._lock:
                self._errors[limit] = {'at': utc_now(), 'error': f'{type(exc).__name__}: {exc}'[:300]}
        finally:
            with self._lock:
                self._refreshing.discard(limit)

    def invalidate(self):
        """Mark every cached result stale so the next read refreshes it in the background."""
        with self._lock:
            self._cached = {limit: (float('-inf'), result) for limit, (_, result) in self._cached.items()}
            for limit in self._cached:
                self._revisions[limit] = self._revisions.get(limit, 0) + 1

    def peek(self, limit=DEFAULT_LIMIT):
        """The cached result for ``limit`` or None; never computes candidates."""
        cached = self._cached.get(limit)
        return cached[1] if cached else None

    def get(self, limit=DEFAULT_LIMIT):
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f'limit 必须是 1–{MAX_LIMIT} 的整数')
        cached = self._cached.get(limit)
        if cached is None:
            with self._lock:
                if limit not in self._cached:
                    self._cached[limit] = (time.monotonic(), build(self.db_path, limit=limit, isolated=self.isolated))
                return {**self._cached[limit][1], 'stale': False}
        stale = time.monotonic() - cached[0] >= CACHE_SECONDS
        if stale:
            with self._lock:
                if limit not in self._refreshing:
                    self._refreshing.add(limit)
                    revision = self._revisions.get(limit, 0)
                    threading.Thread(target=self._refresh, args=(limit, revision), name='ctn-blue-candidates', daemon=True).start()
        return {**cached[1], 'stale': stale, 'refresh_error': self._errors.get(limit)}

    def _remove_reported(self, reported, reports_today):
        """Caller holds the cache lock; submitted posts belong only in history."""
        for limit, (stamp, result) in list(self._cached.items()):
            items = _all_items(result)
            remaining = [item for item in items if item['post_id'] not in reported]
            if len(remaining) != len(items):
                self._cached[limit] = (stamp, _view(remaining, reports_today, result.get('timing')))
                self._revisions[limit] = self._revisions.get(limit, 0) + 1

    def recheck(self, post_id, limit=DEFAULT_LIMIT):
        """Force one known candidate's GET check, without fetching other posts."""
        if type(post_id) is not int or post_id <= 0:
            raise ValueError('帖子 ID 无效')
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f'limit 必须是 1–{MAX_LIMIT} 的整数')
        from .blue_submit import submitted, submitted_today
        # Manual rechecks serialize independently of cached reads and refreshes.
        with self._recheck_lock:
            with self._lock:
                cached = self._cached.get(limit)
                if cached is None or not any(item['post_id'] == post_id for item in _all_items(cached[1])):
                    raise ValueError('这条帖子不在当前建议中，请先刷新列表')
            reported = {entry.get('post_id') for entry in submitted(self.db_path)}
            reports_today = submitted_today(self.db_path)
            if post_id in reported:
                with self._lock:
                    self._remove_reported(reported, reports_today)
                raise ValueError('这条帖子已经举报过，请查看历史举报记录')
            verification = blue_availability.check(self.db_path, [post_id], deadline_seconds=10)[post_id]
            reported = {entry.get('post_id') for entry in submitted(self.db_path)}
            reports_today = submitted_today(self.db_path)
            with self._lock:
                self._remove_reported(reported, reports_today)
                if post_id in reported:
                    raise ValueError('这条帖子已经举报过，请查看历史举报记录')
                cached = self._cached.get(limit)
                if cached is None or not any(item['post_id'] == post_id for item in _all_items(cached[1])):
                    raise ValueError('这条帖子已不在当前建议中，请刷新列表')
                for cache_limit, (stamp, result) in list(self._cached.items()):
                    items = _all_items(result)
                    if not any(item['post_id'] == post_id for item in items):
                        continue
                    items = [{**item, 'availability': verification, 'availability_checked_at': verification['checked_at']}
                             if item['post_id'] == post_id else item for item in items]
                    self._cached[cache_limit] = (stamp, _view(items, reports_today, result.get('timing')))
                    self._revisions[cache_limit] = self._revisions.get(cache_limit, 0) + 1
                stamp, result = self._cached[limit]
                return {'post_id': post_id, 'availability': verification,
                        'view': {**result, 'stale': time.monotonic() - stamp >= CACHE_SECONDS}}
