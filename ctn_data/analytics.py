"""Descriptive statistics of observed public data; snapshots stream one post at a time."""
from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from itertools import groupby
from functools import lru_cache
import math
import re
import statistics
import threading

from .db import connect, initialize, iter_rows, utc_now
from .imports import normalize_timestamp
from .scoring import sydney_time
from .analysis_explore import SAMPLE_STAGES, SAMPLE_STATUSES, SamplingAnalysis, reply_analysis, trending_analysis

METRICS = ("like_count", "reply_count", "repost_count", "view_count")
STOPWORDS = set("a an and are as at be been but by can could do for from had has have he her him his i if in into is it its may me more most my no nor not of on or our ours she so than that the their them there these they this those to too us was we were what when where which who will with would you your".split())


def _stamp(value):
    if not value:
        return None
    try:
        # The database already stores ISO timestamps. Avoid formatting and parsing
        # the same instant again; retain the import parser for legacy formats.
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.fromisoformat(normalize_timestamp(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _bucket_start(value, unit, bucket_timezone):
    """Return UTC bucket start, respecting Sydney's 23/25-hour DST days.

    Resolve truncated wall time by round-trip, because the Windows fallback
    returns a fixed offset for the observed instant, not a dynamic tzinfo.
    """
    local = sydney_time(value) if bucket_timezone == "Australia/Sydney" else value.astimezone(timezone.utc)
    truncated = local.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        truncated = truncated.replace(hour=0)
    if bucket_timezone == "UTC":
        return normalize_timestamp(truncated)
    wall = truncated.replace(tzinfo=None)
    for offset in (local.utcoffset(), timedelta(hours=10), timedelta(hours=11)):
        candidate = (wall - offset).replace(tzinfo=timezone.utc)
        round_trip = sydney_time(candidate)
        if round_trip.replace(tzinfo=None) == wall and (unit == "day" or round_trip.utcoffset() == local.utcoffset()):
            return normalize_timestamp(candidate)
    raise ValueError("无法解析悉尼时间桶边界")


class _Correlation:
    """Online Welford covariance: all eligible pairs, bounded memory."""
    def __init__(self, x_name, y_name):
        self.x_name, self.y_name = x_name, y_name
        self.n = 0
        self.x_mean = self.y_mean = self.x_var = self.y_var = self.covariance = 0.0

    def add(self, x, y):
        self.n += 1
        dx, dy = x - self.x_mean, y - self.y_mean
        self.x_mean += dx / self.n
        self.y_mean += dy / self.n
        self.x_var += dx * (x - self.x_mean)
        self.y_var += dy * (y - self.y_mean)
        self.covariance += dx * (y - self.y_mean)

    def result(self):
        result = {"x": self.x_name, "y": self.y_name, "n": self.n, "pearson_r": None,
                  "reason": None, "interpretation": "描述性相关，不能证明因果；全部合资格窗口样本均参与计算"}
        if self.n < 3:
            result["reason"] = "至少需要3个完整配对样本"
        elif self.x_var <= 0 or self.y_var <= 0:
            result["reason"] = "至少一个变量没有变化，相关系数未定义"
        else:
            result["pearson_r"] = round(max(-1.0, min(1.0, self.covariance / math.sqrt(self.x_var * self.y_var))), 6)
        return result


def _growth(post, samples, window, tolerance, baseline_delay, *, stamp_fn=_stamp):
    created = stamp_fn(post["created_at"])
    if created is None:
        return None, "missing_created_at"
    baseline = next(((stamp, snap) for stamp, snap in samples
                     if created <= stamp <= created + timedelta(minutes=baseline_delay)), None)
    if baseline is None:
        return None, "no_early_baseline"
    start, first = baseline
    target = start + timedelta(minutes=window)
    endpoints = ((stamp, snap) for stamp, snap in samples if stamp > start and abs((stamp - target).total_seconds()) <= tolerance * 60)
    endpoint = min(endpoints, key=lambda item: (abs((item[0] - target).total_seconds()), item[0]), default=None)
    if endpoint is None:
        return None, "no_endpoint_in_tolerance"
    end, last = endpoint
    deltas = {metric: last[metric] - first[metric] if _number(last[metric]) and _number(first[metric]) else None for metric in METRICS}
    interactions = sum(deltas[metric] for metric in METRICS[:3]) if all(deltas[metric] is not None for metric in METRICS[:3]) else None
    return {"post_id": post["post_id"], "author_id": post["author_id"], "username": post["username"],
            "created_at": post["created_at"], "baseline_at": normalize_timestamp(start), "endpoint_at": normalize_timestamp(end),
            "baseline_delay_minutes": round((start - created).total_seconds() / 60, 3),
            "elapsed_minutes": round((end - start).total_seconds() / 60, 3),
            "body_length": len(post["body"] or ""), "kind": post["kind"], "deltas": deltas,
            "interaction_delta": interactions, "has_negative_delta": any(value is not None and value < 0 for value in deltas.values())}, None


class _PostFacts:
    """One post's snapshot-derived contributions, applied in stream order.

    Integer deltas are summed per post; their float totals are exact, so the
    aggregate equals adding each snapshot pair individually.
    """
    __slots__ = ('snapshot_count', 'missing', 'invalid', 'invalid_times', 'sample_count', 'bins', 'deltas',
                 'negative_count', 'negatives', 'latest_time', 'latest', 'growth', 'reason', 'sampling')

    def __init__(self, post, records, *, stamp_for, bin_for, growth_for, sampler, growth_args):
        samples, missing, invalid = [], [0] * len(METRICS), [0] * len(METRICS)
        self.snapshot_count = self.invalid_times = 0
        # Historical snapshots are materialized only for this one post.
        for record in records:
            snap = dict(record)
            self.snapshot_count += 1
            for index, metric in enumerate(METRICS):
                missing[index] += snap[metric] is None
                invalid[index] += snap[metric] is not None and not _number(snap[metric])
            stamp = stamp_for(snap["observed_at"])
            if stamp is None:
                self.invalid_times += 1
            else:
                samples.append((stamp, snap))
        samples.sort(key=lambda item: (item[0], item[1]["id"]))
        self.missing, self.invalid, self.sample_count = tuple(missing), tuple(invalid), len(samples)
        self.bins = self.deltas = self.negatives = ()
        self.negative_count = 0
        self.latest_time = self.latest = self.growth = self.reason = self.sampling = None
        if not samples:
            return
        bins, deltas, negatives = {}, {}, []
        for stamp, _ in samples:
            key = bin_for(stamp)
            bins[key] = bins.get(key, 0) + 1
        post_id = str(post["post_id"])
        for (_, before), (after_time, after) in zip(samples, samples[1:]):
            key = bin_for(after_time)
            for metric in METRICS:
                if _number(before[metric]) and _number(after[metric]):
                    delta = after[metric] - before[metric]
                    total = deltas.setdefault((key, metric), [0, 0])
                    total[0] += delta
                    total[1] += 1
                    if delta < 0:
                        self.negative_count += 1
                        if len(negatives) < 100:
                            negatives.append({"post_id": post_id, "metric": metric, "before": before[metric],
                                              "after": after[metric], "observed_at": normalize_timestamp(after_time)})
        self.bins = tuple(bins.items())
        self.deltas = tuple((key, metric, total, pairs) for (key, metric), (total, pairs) in deltas.items())
        self.negatives = tuple(negatives)
        self.latest_time, latest = samples[-1]
        self.latest = tuple(latest[metric] for metric in METRICS)
        self.growth, self.reason = growth_for(post, samples, *growth_args)
        self.sampling = sampler.facts(post, samples) if sampler is not None else None


def _post_key(post):
    return hash((post["created_at"], post["author_id"], post["username"], post["body"], post["kind"]))


SNAPSHOT_BATCH_POSTS = 500


def _scope_facts(conn, scope_sql, arguments, post_map, compute, cached=None):
    """Yield (post_id, facts) for scoped posts with snapshots, by post_id.

    Without a cache every post is computed from one ordered stream. With one,
    a post is recomputed only when its snapshot count/last id or its own
    fields changed; all reads share the caller's snapshot transaction.
    """
    scope = "SELECT p.post_id" + scope_sql
    stream = ("SELECT c.* FROM counter_snapshots c WHERE c.post_id IN ({}) "
              "ORDER BY c.post_id,c.observed_at,c.id")

    def batches(post_ids):
        # Each batch is read and its cursor closed before any computation, so
        # no read transaction stays open while Python works (it held VACUUM back
        # for minutes on live data). Posts come from post_map, so a post added
        # after it was read is simply not part of this analysis.
        for offset in range(0, len(post_ids), SNAPSHOT_BATCH_POSTS):
            batch = post_ids[offset:offset + SNAPSHOT_BATCH_POSTS]
            with iter_rows(conn, stream.format(",".join("?" for _ in batch)), [int(post_id) for post_id in batch]) as cursor:
                rows = list(cursor)
            yield from groupby(rows, key=lambda row: str(row["post_id"]))

    if cached is None:
        for post_id, records in batches(sorted(post_map, key=int)):
            yield post_id, compute(post_map[post_id], records)
        return
    signatures = {str(row["post_id"]): (row["snapshots"], row["last_id"]) for row in conn.execute(
        "SELECT c.post_id,COUNT(*) AS snapshots,MAX(c.id) AS last_id FROM counter_snapshots c "
        "WHERE c.post_id IN (" + scope + ") GROUP BY c.post_id", arguments) if str(row["post_id"]) in post_map}
    keys = {post_id: (signature, _post_key(post_map[post_id])) for post_id, signature in signatures.items()}
    stale = {post_id for post_id, key in keys.items() if post_id not in cached or cached[post_id][0] != key}
    for post_id, records in batches(sorted(stale, key=int)):
        cached[post_id] = (keys[post_id], compute(post_map[post_id], records))
    for post_id in sorted(signatures, key=int):
        if post_id in cached:
            yield post_id, cached[post_id][1]


class PostFactsCache:
    """Per-post analysis facts for the most recently used parameter sets.

    Entries are only valid for the snapshot signature and post fields they
    were computed from; analyses sharing parameters run one at a time.
    """
    def __init__(self, max_parameter_sets=2):
        self.max_parameter_sets = max_parameter_sets
        self._lock = threading.Lock()
        self._entries = OrderedDict()

    @contextmanager
    def scope(self, key):
        with self._lock:
            entry = self._entries.pop(key, None) or (threading.Lock(), {})
            self._entries[key] = entry
            while len(self._entries) > self.max_parameter_sets:
                self._entries.popitem(last=False)
        with entry[0]:
            yield entry[1]


def analysis_parameters(params: dict | None = None) -> dict:
    """Validate and canonicalize request options without reading collected data."""
    params = params or {}
    available_sections = {"core", "growth", "network", "trending", "replies", "sampling"}
    sections_value = params.get("sections", "all")
    if not isinstance(sections_value, str):
        raise ValueError("sections 必须为逗号分隔的分析区段")
    sections = {section.strip() for section in sections_value.split(",")}
    if sections == {"all"}:
        sections = available_sections.copy()
    if not sections or not sections <= available_sections:
        raise ValueError("sections 包含未知分析区段")
    sections.add("core")
    export_value = params.get("include_exportable", True)
    if isinstance(export_value, bool):
        include_exportable = export_value
    elif isinstance(export_value, str) and export_value.lower() in {"true", "false", "1", "0"}:
        include_exportable = export_value.lower() in {"true", "1"}
    else:
        raise ValueError("include_exportable 必须为布尔值")
    def number(name, default, *, allow_zero=False, maximum=20160):
        value = params.get(name, default)
        if isinstance(value, bool):
            raise ValueError(f"{name} 必须为有效数值")
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须为有效数值") from None
        if not math.isfinite(result) or result < 0 or (not allow_zero and result == 0) or result > maximum:
            raise ValueError(f"{name} 超出有效范围")
        return result
    window = number("window_minutes", 60)
    tolerance = number("tolerance_minutes", min(max(5, window * .2), window * .5), allow_zero=True)
    baseline_delay = number("max_baseline_delay_minutes", 10, allow_zero=True)
    detail_value = number("detail_limit", 1000, maximum=10000)
    if not detail_value.is_integer():
        raise ValueError("detail_limit 必须为整数")
    detail_limit = int(detail_value)
    # Live data has thousands of authors; the page shows the most active ones.
    cohort_value = number("cohort_limit", 50, maximum=10000)
    if not cohort_value.is_integer():
        raise ValueError("cohort_limit 必须为整数")
    cohort_limit = int(cohort_value)
    focus_post_id = params.get("focus_post_id")
    if focus_post_id is not None:
        if (isinstance(focus_post_id, bool)
                or not isinstance(focus_post_id, (int, str))
                or not re.fullmatch(r"[1-9][0-9]*", str(focus_post_id))
                or int(focus_post_id) > 9223372036854775807):
            raise ValueError("focus_post_id 必须为正整数帖子ID")
        focus_post_id = int(focus_post_id)
    sample_status = params.get("sample_status")
    sample_stage = params.get("sample_stage")
    if sample_status is not None and sample_status != "" and (not isinstance(sample_status, str) or sample_status not in SAMPLE_STATUSES):
        raise ValueError("sample_status 不是有效采样状态")
    if sample_stage is not None and sample_stage != "" and (not isinstance(sample_stage, str) or sample_stage not in SAMPLE_STAGES):
        raise ValueError("sample_stage 不是有效采样阶段")
    sample_status, sample_stage = sample_status or None, sample_stage or None
    if sample_status is not None and sample_stage is not None:
        raise ValueError("sample_status 与 sample_stage 不可同时指定")
    if tolerance >= window:
        raise ValueError("tolerance_minutes 必须小于 window_minutes")
    bucket = params.get("bucket", "hour")
    if bucket not in {"hour", "day"}:
        raise ValueError("bucket 只支持 hour/day")
    bucket_timezone = params.get("bucket_timezone", "Australia/Sydney")
    if bucket_timezone not in {"Australia/Sydney", "UTC"}:
        raise ValueError("bucket_timezone 只支持 Australia/Sydney 或 UTC")
    own = params.get("own", "all")
    if own not in {"all", "own", "unknown"}:
        raise ValueError("own 只支持 all/own/unknown")
    since, until = _stamp(params.get("since")), _stamp(params.get("until"))
    if (params.get("since") and since is None) or (params.get("until") and until is None):
        raise ValueError("since/until 必须为 ISO8601 时间")
    if since and until and since > until:
        raise ValueError("since 不可晚于 until")
    if params.get("as_of") and _stamp(params["as_of"]) is None:
        raise ValueError("as_of 必须为 ISO8601 时间")
    sample_batch = params.get('sample_batch')
    if sample_batch and _stamp(sample_batch) is None:
        raise ValueError('sample_batch 必须为 ISO8601 时间')
    sample_batch = _bucket_start(_stamp(sample_batch), bucket, bucket_timezone) if sample_batch else None
    result = {"window_minutes": window, "tolerance_minutes": tolerance,
              "max_baseline_delay_minutes": baseline_delay, "detail_limit": detail_limit,
              "cohort_limit": cohort_limit, "bucket": bucket, "bucket_timezone": bucket_timezone, "own": own,
              "sections": ",".join(sorted(sections)), "include_exportable": include_exportable}
    for name, value in (("since", since), ("until", until), ("as_of", _stamp(params.get("as_of")))):
        if value is not None:
            result[name] = normalize_timestamp(value)
    if params.get("author_id") is not None:
        result["author_id"] = str(params["author_id"])
    if focus_post_id is not None:
        result["focus_post_id"] = focus_post_id
    for name, value in (("sample_status", sample_status), ("sample_stage", sample_stage), ("sample_batch", sample_batch)):
        if value is not None:
            result[name] = value
    return result


def analyze(db_path, params: dict | None = None, *, facts_cache: PostFactsCache | None = None) -> dict:
    """Analyze all selected records; detail_limit caps returned rows, never aggregates.

    params: window_minutes=60, tolerance_minutes=20% (min 5, max half-window),
    max_baseline_delay_minutes=10, bucket=hour|day, bucket_timezone=Australia/Sydney|UTC,
    author_id, own=all|own|unknown,
    since/until (publication range), as_of (quality reference), detail_limit=1000,
    focus_post_id (positive integer), sample_status or sample_stage (sampling details only).
    sections=all (default), or comma-separated core,growth,network,trending,replies,sampling.
    include_exportable=true (default); false removes the redundant legacy export copy.
    Each post's snapshots are loaded and released before the next post is read.
    facts_cache (PostFactsCache) reuses per-post results of unchanged posts.
    """
    params = params or {}
    options = analysis_parameters(params)
    sections = set(options["sections"].split(","))
    include_exportable = options["include_exportable"]
    window, tolerance = options["window_minutes"], options["tolerance_minutes"]
    baseline_delay, detail_limit = options["max_baseline_delay_minutes"], options["detail_limit"]
    focus_post_id, sample_status, sample_stage = (options.get(name) for name in ("focus_post_id", "sample_status", "sample_stage"))
    bucket, bucket_timezone, own = options["bucket"], options["bucket_timezone"], options["own"]
    since, until = _stamp(options.get("since")), _stamp(options.get("until"))
    now = _stamp(options.get("as_of")) or _stamp(utc_now())
    sample_batch = options.get("sample_batch")
    # These bounded caches belong to this invocation, so concurrent analyses
    # cannot share mutable state or keep a database result alive after a request.
    # Each post's created_at is parsed in several passes; hold every post.
    stamp_for = lru_cache(maxsize=262144)(_stamp)
    def growth_for(post, samples, window, tolerance, baseline_delay):
        return _growth(post, samples, window, tolerance, baseline_delay, stamp_fn=stamp_for)
    @lru_cache(maxsize=4096)
    def bucket_for_hour(hour):
        return _bucket_start(hour, bucket, bucket_timezone)
    def bin_for(stamp):
        # UTC hour boundaries cover both supported zones, including Sydney's
        # DST repeated/missing hour. Cache by hour, not by every microsecond.
        return bucket_for_hour(stamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0))
    sampling = (SamplingAnalysis(window, tolerance, baseline_delay, now, detail_limit, sample_status, focus_post_id, sample_stage,
                                growth_fn=growth_for, bucket_fn=bin_for, sample_batch=sample_batch, stamp_fn=stamp_for)
                if "sampling" in sections else None)
    clauses, arguments = [], []
    if params.get("author_id") is not None:
        clauses.append("p.author_id=?")
        arguments.append(str(params["author_id"]))
    if own == "own":
        clauses.append("t.is_own=1")
    elif own == "unknown":
        clauses.append("t.team_label IS NULL")
    if since:
        # Match the expression index and preserve microsecond boundaries;
        # julianday rounds SQLite timestamps to milliseconds and scans all posts.
        clauses.append("ctn_timestamp(p.created_at)>=?")
        arguments.append(normalize_timestamp(since))
    if until:
        clauses.append("ctn_timestamp(p.created_at)<=?")
        arguments.append(normalize_timestamp(until))
    predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
    joins = " FROM posts p LEFT JOIN accounts a ON a.account_id=p.author_id LEFT JOIN team_accounts t ON t.account_id=p.author_id"
    initialize(db_path)
    conn = connect(db_path)
    try:
        # No transaction spans the analysis: on live data it would hold a
        # snapshot for minutes and block VACUUM. Reads autocommit; snapshot
        # batches read only posts loaded here, so late inserts are ignored.
        posts = [dict(row) for row in conn.execute("SELECT p.*,a.username,a.display_name,t.team_label,t.is_own,t.evidence" + joins + predicate + " ORDER BY ctn_timestamp(p.created_at),p.post_id", arguments)]
        post_map = {str(post["post_id"]): post for post in posts}
        accounts_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        bins = defaultdict(lambda: {"posts_published": 0, "snapshots": 0, "observed_posts": 0,
                                    "delta": defaultdict(float), "pairs": Counter()})
        cohorts_map = {}
        terms, documents = Counter(), Counter()
        for post in posts:
            created = stamp_for(post["created_at"])
            if created:
                bins[bin_for(created)]["posts_published"] += 1
            author = str(post["author_id"])
            if author not in cohorts_map:
                cohorts_map[author] = {"author_id": post["author_id"], "username": post["username"], "team_label": post["team_label"],
                                       "is_own": post["is_own"], "evidence": post["evidence"], "posts": 0, "posts_with_counters": 0,
                                       "window_sample_count": 0, "latest_observed_totals": {metric: None for metric in METRICS},
                                       "metric_coverage_posts": {metric: 0 for metric in METRICS}, "_interactions": []}
            cohorts_map[author]["posts"] += 1
            body = re.sub(r"https?://\S+", " ", post["body"] or "", flags=re.I)
            tokens = [term.casefold() for term in re.findall(r"[^\W\d_]+", body, flags=re.UNICODE)]
            tokens = [term for term in tokens if len(term) >= 2 and term not in STOPWORDS]
            terms.update(tokens)
            documents.update(set(tokens))
        growth, exclusions, missing_metrics, invalid_metrics = [], Counter(), Counter(), Counter()
        correlations = [_Correlation("body_length", "same_window_interactions"), _Correlation("same_window_views", "same_window_interactions")]
        negative_examples = []
        snapshot_count = invalid_times = negative_count = stale_posts = growth_count = 0
        seen_posts, last_counter = set(), None
        max_post_snapshots = 0
        # Snapshot-derived results of each post do not depend on the clock,
        # filters or detail limits; facts_cache reuses them for unchanged posts.
        # Cached facts must serve later requests for any section.
        fact_sampler = (SamplingAnalysis(window, tolerance, baseline_delay, now, detail_limit,
                                         growth_fn=growth_for, stamp_fn=stamp_for)
                        if sampling is not None or facts_cache is not None else None)
        def compute(post, records):
            return _PostFacts(post, records, stamp_for=stamp_for, bin_for=bin_for, growth_for=growth_for,
                              sampler=fact_sampler, growth_args=(window, tolerance, baseline_delay))
        facts_key = (window, tolerance, baseline_delay, bucket, bucket_timezone)
        with (facts_cache.scope(facts_key) if facts_cache is not None else nullcontext()) as cached:
            for post_id, facts in _scope_facts(conn, joins + predicate, arguments, post_map, compute, cached):
                post = post_map[post_id]
                snapshot_count += facts.snapshot_count
                for metric, missing, invalid in zip(METRICS, facts.missing, facts.invalid):
                    missing_metrics[metric] += missing
                    invalid_metrics[metric] += invalid
                invalid_times += facts.invalid_times
                max_post_snapshots = max(max_post_snapshots, facts.sample_count)
                if not facts.sample_count:
                    continue
                seen_posts.add(post_id)
                for key, count in facts.bins:
                    bins[key]["snapshots"] += count
                    bins[key]["observed_posts"] += 1
                for key, metric, total, pairs in facts.deltas:
                    bins[key]["delta"][metric] += total
                    bins[key]["pairs"][metric] += pairs
                negative_count += facts.negative_count
                negative_examples.extend(facts.negatives[:100 - len(negative_examples)])
                latest_time = facts.latest_time
                last_counter = max(last_counter, latest_time) if last_counter else latest_time
                stale_posts += now - latest_time > timedelta(hours=24)
                cohort = cohorts_map[str(post["author_id"])]
                cohort["posts_with_counters"] += 1
                for metric, value in zip(METRICS, facts.latest):
                    if _number(value):
                        cohort["metric_coverage_posts"][metric] += 1
                        cohort["latest_observed_totals"][metric] = (cohort["latest_observed_totals"][metric] or 0) + value
                row, reason = facts.growth, facts.reason
                if sampling is not None:
                    sampling.apply(post, facts.sampling, row)
                if reason:
                    exclusions[reason] += 1
                else:
                    growth_count += 1
                    cohort["window_sample_count"] += 1
                    if ("growth" in sections or include_exportable) and len(growth) < detail_limit:
                        growth.append(row)
                    if row["interaction_delta"] is not None:
                        cohort["_interactions"].append(row["interaction_delta"])
                        if not row["has_negative_delta"]:
                            correlations[0].add(row["body_length"], row["interaction_delta"])
                            if row["deltas"]["view_count"] is not None:
                                correlations[1].add(row["deltas"]["view_count"], row["interaction_delta"])
        for post_id, post in post_map.items():
            if post_id not in seen_posts:
                exclusions["missing_created_at" if stamp_for(post["created_at"]) is None else "no_early_baseline"] += 1
                if sampling is not None:
                    sampling.add(post, [], None, "no_counters")
        timeseries = [{"bucket_at": stamp, "timezone": bucket_timezone, "posts_published": entry["posts_published"],
                       "snapshots": entry["snapshots"], "observed_posts": entry["observed_posts"],
                       "counter_deltas": {metric: entry["delta"][metric] if entry["pairs"][metric] else None for metric in METRICS},
                       "paired_samples": dict(entry["pairs"])} for stamp, entry in sorted(bins.items())]
        cohorts = []
        for cohort in cohorts_map.values():
            interactions = cohort.pop("_interactions")
            cohort.update(interaction_window_sample_count=len(interactions),
                          median_window_interactions=statistics.median(interactions) if interactions else None,
                          mean_window_interactions=round(statistics.mean(interactions), 4) if interactions else None)
            cohorts.append(cohort)
        cohorts.sort(key=lambda row: (-row["posts"], str(row["author_id"])))
        keywords = [{"term": term, "count": count, "post_count": documents[term]} for term, count in terms.most_common(100)]
        edges, account_edges = [], Counter()
        edge_count = missing_targets = 0
        relation_sql = "SELECT p.post_id,p.author_id,p.reply_to_post_id,p.repost_of_post_id,r.author_id AS reply_author,r.post_id AS reply_observed,s.author_id AS repost_author,s.post_id AS repost_observed" + joins + " LEFT JOIN posts r ON r.post_id=p.reply_to_post_id LEFT JOIN posts s ON s.post_id=p.repost_of_post_id" + predicate
        for relation_post in conn.execute(relation_sql, arguments):
            for field, relation, prefix in (("reply_to_post_id", "reply", "reply"), ("repost_of_post_id", "repost", "repost")):
                parent_id = relation_post[field]
                if parent_id is None:
                    continue
                edge_count += 1
                observed = relation_post[f"{prefix}_observed"] is not None
                target_author = relation_post[f"{prefix}_author"]
                missing_targets += not observed
                if len(edges) < detail_limit:
                    edges.append({"source_post_id": relation_post["post_id"], "target_post_id": parent_id, "relation": relation,
                                  "source_author_id": relation_post["author_id"], "target_author_id": target_author, "target_observed": observed})
                if observed and relation_post["author_id"] is not None and target_author is not None:
                    account_edges[(str(relation_post["author_id"]), str(target_author), relation)] += 1
        trending = trending_analysis(conn, posts, detail_limit, focus_post_id, stamp_fn=stamp_for) if "trending" in sections else None
        replies = reply_analysis(conn, posts, detail_limit, focus_post_id, stamp_fn=stamp_for) if "replies" in sections else None
    finally:
        conn.close()
    quality = {"sampled_posts": len(posts), "posts_without_counters": len(posts) - len(seen_posts),
               "posts_missing_created_at": sum(stamp_for(post["created_at"]) is None for post in posts),
               "posts_missing_author": sum(post["author_id"] is None for post in posts),
               "unknown_team_posts": sum(post["team_label"] is None for post in posts),
               "missing_metric_values": {metric: missing_metrics[metric] for metric in METRICS},
               "invalid_metric_values": {metric: invalid_metrics[metric] for metric in METRICS},
               "invalid_snapshot_timestamps": invalid_times, "stale_counter_posts_24h": stale_posts,
               "relations_outside_sample": missing_targets, "negative_counter_transitions": negative_count,
               "negative_transition_examples": negative_examples, "negative_examples_truncated": negative_count > len(negative_examples),
               "growth_eligible_posts": growth_count, "growth_exclusions": dict(exclusions),
               "last_counter_observed_at": normalize_timestamp(last_counter) if last_counter else None,
               "freshness_reference": normalize_timestamp(now), "snapshot_processing": "stream_by_post",
               "max_post_snapshots_buffered": max_post_snapshots}
    caveats = ["仅描述采集范围，不保证全平台覆盖；Latest/Trending和采集限额会造成选择偏差",
               "账号阵营仅采用显式证据，认证标记不代表NPC；公开互动不能换算NPC行为或官方积分",
               f"时间序列增量来自同帖连续快照，归入后一次观测所在{bucket_timezone}时间桶；bucket_at以UTC存储，跨桶间隔不代表真实事件发生时间",
               "相同窗口采用发布后早期实际快照及近目标时间的实际端点，不插值；容差和基线延迟会影响可比性",
               "缺失值保留为空；计数下降可能来自取消互动、删除或平台修正，未自动视为负面效果",
               "账号累计值来自各帖不同采集时刻的最新值；需结合覆盖帖子数，不是同一时点全量统计",
               "相关性采用全部合资格样本，排除计数下降窗口；未控制账号、曝光和采集偏差，不能说明因果",
               "词频为当前帖文的Unicode词元计数；使用英文停用词，中文连续文本未进行专业分词",
               "回复/转发图只表示公开引用关系，不代表身份、影响力或观点改变；文本分析使用当前版本",
               "快照在SQL按范围筛选后逐帖处理；内存仍随所选帖子、时间桶、词汇和单帖最长历史增长",
               "growth/network明细受detail_limit限制且标明总数；统计覆盖全部选中数据，完整原始数据请用CSV导出"]
    caveats.extend([
        "热榜仅展示实际观测排名；两次观测之间及首次/末次观测之外是否在榜未知，观测跨度不是连续在榜时长",
        "回复树汇总仅计筛选内帖子与作者；筛选外祖先和缺失父帖仅作上下文，环按最小帖子ID断边并保留原父帖ID",
        "采样漏斗要求三项互动计数完整，且任何可用指标均未下降；状态或阶段筛选仅改变采样明细",
    ])
    if not posts:
        caveats.insert(0, "当前筛选范围没有真实帖子，结果为空；未生成演示或推断数据")
    network = {"edges": edges, "total_edges": edge_count, "truncated": edge_count > len(edges),
               "account_edges": [{"source_author_id": source, "target_author_id": target, "relation": relation, "count": count}
                                 for (source, target, relation), count in account_edges.most_common(detail_limit)],
               "total_account_edges": len(account_edges), "account_edges_truncated": len(account_edges) > detail_limit}
    dates = [stamp for post in posts if (stamp := stamp_for(post["created_at"])) is not None]
    result = {"generated_at": utc_now(), "parameters": {**params, "window_minutes": window, "tolerance_minutes": tolerance,
                                                       "max_baseline_delay_minutes": baseline_delay, "bucket": bucket,
                                                       "bucket_timezone": bucket_timezone, "detail_limit": detail_limit},
            "summary": {"posts": len(posts), "accounts_in_scope": len(cohorts), "accounts_in_database": accounts_count,
                        "counter_snapshots": snapshot_count, "growth_sample_count": growth_count,
                        "publication_start": normalize_timestamp(min(dates)) if dates else None,
                        "publication_end": normalize_timestamp(max(dates)) if dates else None},
            "timeseries": timeseries, "cohorts": cohorts[:options["cohort_limit"]],
            "cohorts_total": len(cohorts), "cohorts_truncated": len(cohorts) > options["cohort_limit"], "growth": growth,
            "growth_total": growth_count, "growth_truncated": growth_count > detail_limit,
            "correlation": [correlation.result() for correlation in correlations], "keywords": keywords,
            "network": network, "quality": quality, "caveats": caveats,
             "trending": trending, "replies": replies, "sampling": sampling.result() if sampling is not None else None,
            "exportable": {"timeseries": timeseries, "cohorts": cohorts, "growth": growth,
                           "correlation": [correlation.result() for correlation in correlations], "keywords": keywords,
                           "network_edges": edges, "detail_truncation": {"growth": growth_count > len(growth), "network": edge_count > len(edges)}}}
    if not include_exportable:
        result.pop("exportable")
    for section in ("growth", "network", "trending", "replies", "sampling"):
        if section not in sections:
            result.pop(section, None)
    # A missing section is not an empty result; clients can request it on demand.
    result["included_sections"] = sorted(sections)
    return result
