"""CTN 2026 rules, hypothetical calculators, and official-score reconciliation.

Scenario results are never written to the official ledger. All points use Decimal.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import connect, initialize, utc_now
from .imports import decimal_text, normalize_timestamp

SOURCE = "https://capturethenarrative.com/info/competitions/2026/scoring/"
RULES_SOURCE = "https://capturethenarrative.com/info/competitions/2026/rules/"
START = date(2026, 10, 19)
ENGAGEMENT = {"views": 1, "likes": 5, "reposts": 10, "replies": 15, "follows": 20, "unfollows": -20}
CHANGES = {"towards": (160, 80), "away": (-80, -40), "neutral": (20, 10), "other": (20, 10)}
# Moderator delay and polling awards as revised on the official scoring page, 2026-10-20 PM.
RULING_BASE_HOURS = 3
POLL_THRESHOLDS = {1: 1, 2: 7000, 3: 7000}
POLL_AWARD = 6000
CLOSE_CALL_AWARD = 3000
REFERENDUM_BASE = 20000
REFERENDUM_THRESHOLD = 7500
PERSUASION_POOL = 100000


def _datetime(value=None):
    return datetime.fromisoformat(normalize_timestamp(value or utc_now()).replace("Z", "+00:00"))


def sydney_time(value=None):
    utc = _datetime(value)
    try:
        return utc.astimezone(ZoneInfo("Australia/Sydney"))
    except ZoneInfoNotFoundError:
        # Windows can lack IANA tzdata. Campaign is in 2026; current NSW rules
        # start/end at 16:00 UTC on the preceding Saturday of October/April.
        def first_sunday(month):
            initial = date(utc.year, month, 1)
            return initial + timedelta(days=(6 - initial.weekday()) % 7)
        def transition(month):
            local_day = first_sunday(month)
            return datetime(local_day.year, local_day.month, local_day.day, tzinfo=timezone.utc) - timedelta(hours=8)
        daylight = utc < transition(4) or utc >= transition(10)
        return utc.astimezone(timezone(timedelta(hours=11 if daylight else 10), "AEDT" if daylight else "AEST"))


def rules_context(at=None) -> dict:
    local = sydney_time(at)
    day = (local.date() - START).days
    return {
        "source": SOURCE, "rules_source": RULES_SOURCE, "checked_on": "2026-10-21",
        "timezone": "Australia/Sydney", "at": normalize_timestamp(at or utc_now()),
        "campaign_start_date": START.isoformat(), "campaign_day": day,
        "red_multiplier": None if day < 0 else (1 if day <= 6 else 2 if day <= 12 else 4),
        "multiplier_schedule": [{"from_day": 0, "through_day": 6, "red": 1, "blue": 1, "polling": 1},
                                {"from_day": 7, "through_day": 12, "red": 2, "blue": 1, "polling": 1},
                                {"from_day": 13, "through_day": None, "red": 4, "blue": 1, "polling": 1}],
        "red": {"engagement": ENGAGEMENT, "views_daily_cap": 500,
                "changes": {key: {"decisive": rates[0], "contribution_pool": rates[1]} for key, rates in CHANGES.items()},
                "notes": ["只计 NPC；每帖子/公民/公民会话内去重，浏览上限按全队计",
                          "浏览上限与日计数在悉尼午夜重置；按入账时倍率计算，正负分同乘",
                          "贡献分池按全部贡献帖子等分，再按每帖所属阵营判方向",
                          "可只有贡献而没有决定性帖子；转发再转发归原帖团队；取消点赞无扣分",
                          "每帖最多提及4个账号，回复最多3个；超过8层对话时公民继续回复的概率降低"]},
        "blue": {"first_post_report": 25, "later_post_report": 5, "paid_report_team_limit": 5,
                 "daily_same_target_tiers": [{"from": 1, "through": 5, "rate": "1"},
                                             {"from": 6, "through": 10, "rate": "0.5"},
                                             {"from": 11, "through": None, "rate": "0.1"}],
                 "ruling_hours": [decimal_text(Decimal(RULING_BASE_HOURS) / 2 ** teams) for teams in range(5)],
                 "genuine_suspension_hours": 6,
                 "account_award": 15, "account_min_actions": 100, "account_min_hidden_posts": 1,
                 "automatic_account_suspension_hidden_posts": 3,
                 "notes": ["依裁决而非真实身份计分；同队多账号仅计一队举报",
                           "五队临时隐藏不等于永久隐藏；仅前五队有帖子举报积分",
                           "自己的帖子/账号不给分；同目标队伍折扣以裁决当天悉尼日期计",
                           "账号审核未达条件直接驳回，无分无暂停，后来达标不追溯",
                           "非 bot/spam 账号举报不计分；约十分之一裁决可能有误，认证账号例外"]},
        "polling": {"polls": [{"number": number, "day": day, "date": stamp, "threshold": str(POLL_THRESHOLDS[number])}
                              for number, day, stamp in ((1, 2, "2026-10-21"), (2, 6, "2026-10-25"), (3, 10, "2026-10-29"))],
                    "poll_win_award": str(POLL_AWARD), "close_call_award": str(CLOSE_CALL_AWARD), "close_call_max_lead_percent": "52",
                    "referendum_date": "2026-11-02", "referendum_day": 14,
                    "referendum_base": str(REFERENDUM_BASE), "referendum_threshold": str(REFERENDUM_THRESHOLD),
                    "persuasion_pool": str(PERSUASION_POOL),
                    "change_weight": "1", "contribution_pool_weight": "0.5",
                    "notes": ["资格按计票时总分（含此前民调），不足阈值不补发",
                              f"民调仅 Yes/No 计比例；52–48 或更接近为 close-call，双方合资格队各{CLOSE_CALL_AWARD}",
                              f"公投平票无分；仅赢家阵营，{REFERENDUM_THRESHOLD}分或至少一次合资格说服可获基础{REFERENDUM_BASE}",
                              "分池仅按赢家阵营的正向疫苗观点改变及贡献权重分配，不能用贡献总次数替代分母"]},
        "notes": ["规则优先于计分指南，组织方可能调整；此为2026-10-21核验版本（含10-20下午民调与裁决时间修订）",
                  "公开计数不等于NPC互动，不等于实得积分；未公开身份/分母不得推断",
                  "NPC会话结束后通常数分钟批次入账；积分与计数采集时间不必同步",
                  "失败的提示注入筛查可能无通知，不得用无互动反推被筛除",
                  "无IANA时区库时采用NSW现行DST规则计算2026边界，未来规则变更需复核"]}


def _count(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须为非负整数")
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} 必须为非负整数") from None
    if number < 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValueError(f"{name} 必须为非负整数")
    return number


def _amount(value):
    return Decimal(decimal_text(value))


def _boolean(value, name):
    if not isinstance(value, bool):
        raise ValueError(f"{name} 必须为布尔值")
    return value


def calculate_scenario(payload: dict) -> dict:
    """Compute a supplied hypothetical; omissions needed for a formula stay unknown.

    red: recorded_at, engagement counts (+views_already_paid_today when views>0),
      changes[{view:vaccination|other,direction:towards|away|neutral,
      decisive_count,contributing_posts,total_contributing_posts}].
    blue post: ruling, report_rank, target_is_own, upheld_against_target_today
      (inclusive ordinal), optional distinct_teams/first_report_at.
    blue account: reason:bot_spam, target_is_own, permanently_hidden_posts,
      actions, report_open_at_ruling; type:account; optional ruling timestamp.
    polling poll: poll_number, team_score, team_side:yes|no, yes_votes,no_votes.
    polling referendum: team_score, team_side, yes_votes,no_votes,
      team_persuasion_weight or changes/contributions[{count,total_posts}],
      winning_side_total_weight (includes this team's weight).
    """
    if not isinstance(payload, dict):
        raise ValueError("情景输入必须为对象")
    result = {"mode": "scenario", "is_official": False, "status": "unknown", "points_decimal": None,
              "components": [], "missing": [], "notes": ["仅为输入假设下的计算，不是官方实得；不写入积分流水"],
              "source": SOURCE}
    if "public_counts" in payload or "counter_snapshots" in payload:
        result["missing"] = ["不能把公开互动总计数作为官方NPC积分输入"]
        return result
    track = str(payload.get("track", "")).lower()
    result["track"] = track
    total = Decimal(0)
    def need(*names, data=None):
        source = payload if data is None else data
        missing = [name for name in names if source.get(name) is None]
        result["missing"].extend(missing)
        return not missing
    def component(event, points, **extra):
        nonlocal total
        total += _amount(points)
        result["components"].append({"event": event, "points_decimal": decimal_text(points), **extra})
    if track == "red":
        if not need("recorded_at"):
            return result
        context = rules_context(payload["recorded_at"])
        multiplier = context["red_multiplier"]
        result.update(campaign_day=context["campaign_day"], campaign_date=sydney_time(payload["recorded_at"]).date().isoformat(), multiplier=multiplier)
        if multiplier is None:
            result["missing"].append("比赛开始前没有适用Red倍率")
            return result
        engagement = payload.get("engagement", {})
        changes = payload.get("changes", [])
        if not isinstance(engagement, dict) or not isinstance(changes, list):
            raise ValueError("engagement 必须为对象，changes 必须为数组")
        unknown_keys = set(engagement) - set(ENGAGEMENT) - {"views_already_paid_today"}
        if unknown_keys:
            raise ValueError("未知互动字段：" + ",".join(sorted(unknown_keys)))
        if not engagement and not changes:
            result["missing"].append("engagement 或 changes")
        for event, rate in ENGAGEMENT.items():
            if event not in engagement:
                continue
            count = _count(engagement[event], event)
            paid = count
            if event == "views" and count:
                if not need("views_already_paid_today", data=engagement):
                    continue
                paid = min(count, max(0, 500 - _count(engagement["views_already_paid_today"], "views_already_paid_today")))
            component(event, Decimal(paid * rate * multiplier), count=count, paid_count=paid, base_rate=rate)
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                raise ValueError("changes 的每项必须为对象")
            if not need("view", "decisive_count", "contributing_posts", data=change):
                continue
            view = change["view"]
            direction = "other" if view == "other" else change.get("direction")
            if view not in {"vaccination", "other"}:
                raise ValueError("view 只支持 vaccination 或 other")
            if direction not in ({"towards", "away", "neutral"} if view == "vaccination" else {"other"}):
                result["missing"].append(f"changes[{index}].direction")
                continue
            decisive, pool = CHANGES[direction]
            count = _count(change["decisive_count"], "decisive_count")
            posts = _count(change["contributing_posts"], "contributing_posts")
            component(f"change_{index}_decisive", Decimal(count * decisive * multiplier), direction=direction, count=count)
            if posts:
                if not need("total_contributing_posts", data=change):
                    continue
                denominator = _count(change["total_contributing_posts"], "total_contributing_posts")
                if denominator == 0 or posts > denominator:
                    raise ValueError("全部贡献帖子数必须大于0且不小于本队贡献帖子数")
                component(f"change_{index}_contributions", Decimal(pool) * posts / denominator * multiplier,
                          direction=direction, posts=posts, total_posts=denominator)
        result["notes"].append("仅计算提交的事件类型；计数假定已按NPC/帖子/会话去重，贡献分母含所有团队")
    elif track == "blue":
        subtype = payload.get("type", "post")
        result["multiplier"] = 1
        if "distinct_teams" in payload:
            teams = _count(payload["distinct_teams"], "distinct_teams")
            if teams < 1:
                raise ValueError("distinct_teams 至少为1")
            if teams > 100:
                raise ValueError("distinct_teams 超出情景计算范围")
            if subtype == "post" and teams > 5:
                raise ValueError("帖子五队举报后已临时隐藏，不再接受第六队举报")
            hours = Decimal(RULING_BASE_HOURS) / (2 ** (teams - 1))
            result["ruling_after_hours"] = decimal_text(hours)
            if payload.get("first_report_at"):
                result["scheduled_ruling_at"] = normalize_timestamp(_datetime(payload["first_report_at"]) + timedelta(seconds=float(hours * 3600)))
        if subtype == "post":
            if not need("ruling"):
                return result
            ruling = payload["ruling"]
            if ruling == "pending":
                result["missing"].append("尚未裁决，不是已得积分")
                return result
            if ruling == "genuine":
                component("genuine_ruling", 0)
                result["report_suspension_hours"] = 6
            elif ruling == "inauthentic":
                if not need("report_rank", "target_is_own"):
                    return result
                rank = _count(payload["report_rank"], "report_rank")
                if rank < 1:
                    raise ValueError("report_rank 从1开始")
                own = _boolean(payload["target_is_own"], "target_is_own")
                if own or rank > 5:
                    component("ineligible_post_report", 0)
                elif need("upheld_against_target_today"):
                    ordinal = _count(payload["upheld_against_target_today"], "upheld_against_target_today")
                    if ordinal < 1:
                        raise ValueError("upheld_against_target_today 为包含本次的序号，从1开始")
                    rate = Decimal(1) if ordinal <= 5 else Decimal("0.5") if ordinal <= 10 else Decimal("0.1")
                    component("upheld_post_report", (25 if rank == 1 else 5) * rate,
                              diminishing_rate=decimal_text(rate), inclusive_daily_ordinal=ordinal)
            else:
                raise ValueError("ruling 只支持 pending/genuine/inauthentic")
        elif subtype == "account":
            if not need("reason", "target_is_own"):
                return result
            own = _boolean(payload["target_is_own"], "target_is_own")
            if own or payload["reason"] != "bot_spam":
                component("ineligible_account_report", 0)
            elif need("permanently_hidden_posts", "actions", "report_open_at_ruling"):
                hidden = _count(payload["permanently_hidden_posts"], "permanently_hidden_posts")
                actions = _count(payload["actions"], "actions")
                open_report = _boolean(payload["report_open_at_ruling"], "report_open_at_ruling")
                qualifies = hidden >= 1 and actions >= 100 and open_report
                component("account_review", 15 if qualifies else 0)
                result["account_suspended_by_review"] = qualifies
                result["report_suspension_hours"] = 0
                if not qualifies:
                    result["notes"].append("驳回/已关闭报告不追溯计分；临时隐藏不计永久隐藏阈值")
        else:
            raise ValueError("Blue type 只支持 post/account")
    elif track == "polling":
        subtype = payload.get("type", "poll")
        result["multiplier"] = 1
        if subtype == "baseline":
            component("baseline_poll", 0)
        else:
            if not need("team_side", "yes_votes", "no_votes"):
                return result
            side = str(payload["team_side"]).lower()
            if side not in {"yes", "no"}:
                raise ValueError("team_side 只支持 yes/no")
            yes, no = _count(payload["yes_votes"], "yes_votes"), _count(payload["no_votes"], "no_votes")
            votes = yes + no
            if not votes:
                result["missing"].append("Yes/No有效票为0，无法确定结果")
                return result
            leader = "yes" if yes > no else "no" if no > yes else None
            result["leading_side"] = leader
            result["yes_percent"] = decimal_text(Decimal(yes) / votes * 100)
            if subtype == "poll":
                if not need("poll_number", "team_score"):
                    return result
                number = _count(payload["poll_number"], "poll_number")
                if number not in (1, 2, 3):
                    raise ValueError("poll_number 只支持1、2、3")
                threshold = Decimal(POLL_THRESHOLDS[number])
                eligible = _amount(payload["team_score"]) >= threshold
                close = Decimal(max(yes, no)) / votes <= Decimal("0.52")
                result.update(eligible=eligible, close_call=close, score_threshold=decimal_text(threshold))
                component("official_poll", CLOSE_CALL_AWARD if eligible and close
                          else POLL_AWARD if eligible and side == leader else 0)
            elif subtype == "referendum":
                if leader is None or leader != side:
                    component("referendum_non_winner", 0)
                else:
                    if payload.get("team_persuasion_weight") is not None:
                        weight = _amount(payload["team_persuasion_weight"])
                    elif payload.get("qualifying_changes") is not None and payload.get("contributions") is not None:
                        weight = Decimal(_count(payload["qualifying_changes"], "qualifying_changes"))
                        contributions = payload["contributions"]
                        if not isinstance(contributions, list):
                            raise ValueError("contributions 必须为数组")
                        for contribution in contributions:
                            if not isinstance(contribution, dict):
                                raise ValueError("contributions 的每项必须为对象")
                            if not need("count", "total_posts", data=contribution):
                                continue
                            count = _count(contribution["count"], "contribution.count")
                            denominator = _count(contribution["total_posts"], "contribution.total_posts")
                            if denominator == 0:
                                raise ValueError("contribution.total_posts 必须大于0")
                            weight += Decimal("0.5") * count / denominator
                    else:
                        result["missing"].append("team_persuasion_weight 或 qualifying_changes+contributions")
                        return result
                    if weight < 0:
                        raise ValueError("说服权重不可为负")
                    result["team_persuasion_weight"] = decimal_text(weight)
                    if weight > 0:
                        base_eligible = True
                    elif need("team_score"):
                        base_eligible = _amount(payload["team_score"]) >= REFERENDUM_THRESHOLD
                    else:
                        base_eligible = False
                    component("referendum_base", REFERENDUM_BASE if base_eligible else 0)
                    if weight == 0:
                        component("referendum_persuasion_share", 0)
                    elif need("winning_side_total_weight"):
                        denominator = _amount(payload["winning_side_total_weight"])
                        if denominator < weight or denominator <= 0:
                            raise ValueError("赢家阵营总权重必须大于0且不小于本队权重")
                        component("referendum_persuasion_share", Decimal(PERSUASION_POOL) * weight / denominator)
            else:
                raise ValueError("Polling type 只支持 baseline/poll/referendum")
    else:
        raise ValueError("track 只支持 red/blue/polling")
    result["partial_points_decimal"] = decimal_text(total)
    if not result["missing"]:
        result["status"] = "calculated"
        result["points_decimal"] = decimal_text(total)
    result["notes"].append("采用Decimal计算，未假设官方舍入规则；循环小数按28位有效数字显示")
    return result


def score_overview(db_path) -> dict:
    initialize(db_path)
    conn = connect(db_path)
    try:
        activities = [dict(row) for row in conn.execute("SELECT * FROM official_activity ORDER BY occurred_at,id")]
        # Newest rows from the live activity table until the CSV export has them.
        from .official_feed import pending_rows
        live = pending_rows(conn)
        activities += live
        snapshots = [dict(row) for row in conn.execute("SELECT * FROM score_snapshots ORDER BY ctn_timestamp(observed_at) ASC NULLS FIRST,id")]
    finally:
        conn.close()
    parsed = [row for row in activities if row["parse_status"] == "parsed" and row["points_decimal"] is not None]
    statuses = Counter(row["parse_status"] for row in activities)
    groups = defaultdict(lambda: {"points": Decimal(0), "events": 0})
    total = Decimal(0)
    for row in parsed:
        amount = _amount(row["points_decimal"])
        total += amount
        group = groups[row["track"] or "unknown"]
        group["points"] += amount
        group["events"] += 1
    # Legacy imports marked all malformed timestamps invalid_time, including
    # membership events. Classify those again without rewriting their evidence.
    from .imports import parse_activity
    def non_scoring(row):
        return (row["parse_status"] == "non_scoring"
                or (row["parse_status"] == "invalid_time"
                    and parse_activity(row["event_type"], row["message"])["parse_status"] == "non_scoring"))
    unknown = [row for row in activities if row["parse_status"] != "parsed" and not non_scoring(row)]
    invalid_scoring = sum(row["parse_status"] == "invalid_time" and not non_scoring(row)
                          for row in activities)
    valid_times = [row["occurred_at"] for row in activities if row["occurred_at"]]
    latest = snapshots[-1] if snapshots else None
    reconciliation = {"status": "no_snapshot", "snapshot_total": None, "parsed_through_snapshot": None,
                      "unexplained_decimal": None, "absolute_balance_minus_parsed_decimal": None,
                      "unexplained_basis": "尚无两次不同时间的总分快照，无法区间对账",
                      "unparsed_events": len(unknown),
                      "coverage_start": min(valid_times) if valid_times else None,
                      "coverage_end": max(valid_times) if valid_times else None, "intervals": [],
                      "note": "只有两次官方总分快照之间的差额可进行区间对账；流水覆盖完整性仍需人工确认"}
    if latest:
        cutoff = _datetime(latest["observed_at"])
        through = sum((_amount(row["points_decimal"]) for row in parsed if _datetime(row["occurred_at"]) <= cutoff), Decimal(0))
        reconciliation.update(status="single_snapshot" if len(snapshots) == 1 else "awaiting_interval", snapshot_total=latest["total_decimal"],
                              parsed_through_snapshot=decimal_text(through),
                              absolute_balance_minus_parsed_decimal=decimal_text(_amount(latest["total_decimal"]) - through))
        reconciliation["note"] = "仅有一个观测时点，等待下一次总分快照形成区间；余额减已解析金额仅作参考，不能认定为漏分或未解释项目"
    for before, after in zip(snapshots, snapshots[1:]):
        start, end = _datetime(before["observed_at"]), _datetime(after["observed_at"])
        if end <= start:
            continue
        relevant = [row for row in activities if row["occurred_at"] and start < _datetime(row["occurred_at"]) <= end]
        booked = sum((_amount(row["points_decimal"]) for row in relevant if row["parse_status"] == "parsed" and row["points_decimal"] is not None), Decimal(0))
        official = _amount(after["total_decimal"]) - _amount(before["total_decimal"])
        unparsed = sum(row["parse_status"] != "parsed" and not non_scoring(row) for row in relevant)
        invalid = invalid_scoring
        status = "matched_observed_scope" if official == booked and not unparsed and not invalid else "unresolved"
        reconciliation["intervals"].append({"from": before["observed_at"], "to": after["observed_at"],
                                            "official_delta": decimal_text(official), "parsed_delta": decimal_text(booked),
                                            "difference": decimal_text(official - booked), "unparsed_events": unparsed,
                                            "invalid_time_unallocated": invalid, "status": status})
    if reconciliation["intervals"]:
        interval = reconciliation["intervals"][-1]
        reconciliation.update(status=interval["status"], unexplained_decimal=interval["difference"],
                              unexplained_basis="最近两次不同时间快照区间的官方增量减已解析流水增量",
                              note="区间以(前次时间,后次时间]计算；零差额仅说明已观测范围相符，流水覆盖完整性仍需确认")
    return {"official_total": latest["total_decimal"] if latest else None, "latest_snapshot": latest,
            "parsed_total": decimal_text(total),
            "breakdown": [{"track": track, "points_decimal": decimal_text(values["points"]), "events": values["events"]}
                          for track, values in sorted(groups.items())],
            "activity": {"total": len(activities), "parsed": len(parsed), "unparsed": len(unknown), "live": len(live),
                         "non_scoring": statuses.get("non_scoring", 0), "invalid_time": statuses.get("invalid_time", 0)},
            "reconciliation": reconciliation, "snapshots": snapshots, "source": SOURCE,
            "caveats": ["最新几条来自队伍页面的实时活动表（CSV 导出约晚 2 小时）；导出赶上后改用 CSV 那条，不会重复计算",
                        "明确积分金额才计入parsed_total；纯事件计数、贡献次数和倍率不作为实际入账金额",
                        "总分快照为本地导入/手工抄录，source与note记录来源；系统没有读取私有后台的权限",
                        "零差额仅说明已观测范围相符，不能证明没有漏导或相互抵销的未知事件",
                        "绝对总分减已解析流水不等于已确认漏分：初始余额、导入范围和未知事件均可能解释差额",
                        "没有逐帖官方明细时，不能把团队积分精确归因到帖子或公开互动"]}


# Official polls give only the in-world date. Poll 1 was booked 2026-10-21
# 07:11 Sydney, so later polls are assumed to be counted at the same local time.
POLL_COUNT_LOCAL_TIME = (7, 11)
POLL_DATES = {2: date(2026, 10, 25), 3: date(2026, 10, 29)}
PACE_WINDOWS_HOURS = (6, 12, 24)
# Red is booked once an hour, so shorter windows swing with single bookings.
PACE_BASIS_HOURS = 12
# The browser records the portal total every ~2 minutes; older means collection stopped.
PACE_STALE_MINUTES = 30


def _sydney_utc(day, hour=0, minute=0):
    """UTC instant of a Sydney wall-clock time (no DST change during the campaign)."""
    naive = datetime(day.year, day.month, day.day, hour, minute)
    offset = sydney_time((naive - timedelta(hours=11)).replace(tzinfo=timezone.utc).isoformat()).utcoffset()
    return (naive - offset).replace(tzinfo=timezone.utc)


def _red_multiplier(at):
    day = (sydney_time(at.isoformat()).date() - START).days
    return 1 if day <= 6 else 2 if day <= 12 else 4


def _multiplier_segments(start, end):
    """[(hours, red multiplier)] between two UTC instants, split at Day 7 and Day 13."""
    cuts = [start] + [point for point in (_sydney_utc(START + timedelta(days=7)),
                                          _sydney_utc(START + timedelta(days=13))) if start < point < end] + [end]
    return [((b - a).total_seconds() / 3600, _red_multiplier(a)) for a, b in zip(cuts, cuts[1:]) if b > a]


def score_pace(db_path, now=None) -> dict:
    """Distance to the 7,000-point poll threshold at the current earning speed.

    Speed comes from consecutive official total snapshots, minus polling awards
    booked in the window, and is normalised to Red ×1 so later multipliers can
    be applied. Blue points are not multiplied officially; they are small and
    treated the same here.
    """
    now = _datetime(now)
    widest = now - timedelta(hours=max(PACE_WINDOWS_HOURS))
    initialize(db_path)
    conn = connect(db_path)
    try:
        # Only the rate windows plus the latest earlier total are needed.
        rows = conn.execute("""SELECT observed_at,total_decimal FROM score_snapshots
            WHERE total_decimal IS NOT NULL AND ctn_timestamp(observed_at)>=? AND ctn_timestamp(observed_at)<=?""",
            (normalize_timestamp(widest), normalize_timestamp(now))).fetchall() or conn.execute("""SELECT observed_at,total_decimal
            FROM score_snapshots WHERE total_decimal IS NOT NULL AND ctn_timestamp(observed_at)<=?
            ORDER BY ctn_timestamp(observed_at) DESC,id DESC LIMIT 1""", (normalize_timestamp(now),)).fetchall()
        snapshots = [(_datetime(row["observed_at"]), _amount(row["total_decimal"])) for row in rows]
        polling = [(at, _amount(row["points_decimal"])) for row in conn.execute(
            "SELECT occurred_at,points_decimal FROM official_activity WHERE track='polling' AND parse_status='parsed' AND points_decimal IS NOT NULL")
            if row["occurred_at"] and (at := _datetime(row["occurred_at"])) >= widest]
    finally:
        conn.close()
    snapshots.sort()
    latest = snapshots[-1] if snapshots else None
    rates = {}
    for hours in PACE_WINDOWS_HOURS:
        start = now - timedelta(hours=hours)
        window = [item for item in snapshots if item[0] >= start]
        if latest is None or len(window) < 2 or (window[-1][0] - window[0][0]) < timedelta(hours=hours / 2):
            rates[f"{hours}h"] = None
            continue
        first, last = window[0], window[-1]
        awards = sum((points for at, points in polling if first[0] < at <= last[0]), Decimal(0))
        elapsed = (last[0] - first[0]).total_seconds() / 3600
        observed = float(last[1] - first[1] - awards) / elapsed
        rates[f"{hours}h"] = {"points_per_hour": round(observed, 1),
                              "base_points_per_hour": round(observed / _red_multiplier(last[0]), 1),
                              "from": normalize_timestamp(first[0]), "to": normalize_timestamp(last[0]),
                              "excluded_polling_points": decimal_text(awards)}
    basis_key = next((key for key in [f"{PACE_BASIS_HOURS}h"] + [f"{hours}h" for hours in sorted(PACE_WINDOWS_HOURS, reverse=True)]
                      if rates.get(key)), None)
    basis = rates.get(basis_key) if basis_key else None
    base_rate = basis["base_points_per_hour"] if basis else None
    total = latest[1] if latest else None
    polls = []
    for number, day in sorted(POLL_DATES.items()):
        count_at = _sydney_utc(day, *POLL_COUNT_LOCAL_TIME)
        if count_at <= now:
            continue
        threshold = POLL_THRESHOLDS[number]
        segments = _multiplier_segments(now, count_at)
        weighted_hours = sum(hours * multiplier for hours, multiplier in segments)
        gap = max(Decimal(0), Decimal(threshold) - total) if total is not None else None
        entry = {"poll": number, "threshold": threshold, "count_date": day.isoformat(),
                 "estimated_count_at": normalize_timestamp(count_at), "count_time_estimated": True,
                 "hours_left": round((count_at - now).total_seconds() / 3600, 1),
                 "gap": decimal_text(gap) if gap is not None else None,
                 "required_base_points_per_hour": round(float(gap) / weighted_hours, 1) if gap is not None and weighted_hours else None,
                 "required_points_per_hour_now": None, "projected_total": None, "eta": None, "on_track": None}
        if entry["required_base_points_per_hour"] is not None:
            entry["required_points_per_hour_now"] = round(entry["required_base_points_per_hour"] * _red_multiplier(now), 1)
        if total is not None and base_rate is not None:
            entry["projected_total"] = round(float(total) + base_rate * weighted_hours)
            if gap == 0:
                entry["eta"], entry["on_track"] = normalize_timestamp(latest[0]), True
            elif base_rate > 0:
                remaining, cursor = float(gap), now
                for hours, multiplier in segments:
                    if base_rate * multiplier * hours >= remaining:
                        entry["eta"] = normalize_timestamp(cursor + timedelta(hours=remaining / (base_rate * multiplier)))
                        break
                    remaining -= base_rate * multiplier * hours
                    cursor += timedelta(hours=hours)
                entry["on_track"] = entry["eta"] is not None
            else:
                entry["on_track"] = False
        polls.append(entry)
    age = (now - latest[0]).total_seconds() / 60 if latest else None
    return {"generated_at": normalize_timestamp(now), "total": decimal_text(total) if total is not None else None,
            "observed_at": normalize_timestamp(latest[0]) if latest else None,
            "snapshot_age_minutes": round(age, 1) if age is not None else None,
            "stale": age is None or age > PACE_STALE_MINUTES,
            "red_multiplier_now": _red_multiplier(now), "rates": rates, "rate_basis": basis_key,
            "polls": polls,
            "caveats": ["计票时刻按 Poll 1 入账时间（悉尼 07:11）推算，官方只公布日期",
                        "速度来自官方总分快照的差值，已扣除窗口内的民调奖励；按 Red ×1 归一后再套用 Day 7 ×2、Day 13 ×4",
                        "Blue 分官方不乘倍率，这里一并按 Red 处理；按当前速度外推，不代表 Agent 策略变化后的结果"]}
