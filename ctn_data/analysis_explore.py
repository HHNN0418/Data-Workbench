"""Exploratory views of observed data, with full-sample summaries and bounded details."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from itertools import groupby
import heapq
import math

from .db import iter_rows
from .imports import normalize_timestamp


SAMPLE_STATUSES = {
    "no_counters": "没有有效计数快照",
    "missing_created_at": "缺少发布时间",
    "no_early_baseline": "缺少发布后早期基线",
    "waiting_endpoint": "等待窗口端点",
    "missed_endpoint": "已错过窗口端点",
    "missing_metrics": "窗口互动指标不完整",
    "negative_delta": "窗口存在计数下降",
    "eligible": "可用于窗口分析",
}
SAMPLE_STAGES = {
    "total": "范围内帖子",
    "with_counters": "有有效计数快照",
    "early_baseline": "有早期基线",
    "window_matched": "窗口端点匹配",
    "complete_metrics": "三项互动指标完整",
    "nonnegative": "没有可用指标下降",
}


def _stamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(normalize_timestamp(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None


def _preview(post):
    return " ".join((post.get("body") or "").split())[:180]


def _chunks(values, size=400):
    values = list(values)
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _bounded(rows, limit, focus_post_id=None):
    """Keep the selected post visible without changing any aggregate."""
    selected = rows[:limit]
    if focus_post_id is not None and not any(row["post_id"] == focus_post_id for row in selected):
        focus = next((row for row in rows if row["post_id"] == focus_post_id), None)
        if focus is not None:
            selected = [*selected[:limit - 1], focus]
    return selected


class SamplingAnalysis:
    """Consume one post's already sorted snapshots; never retain snapshot histories."""

    def __init__(self, window, tolerance, baseline_delay, now, limit, sample_status=None, focus_post_id=None, sample_stage=None, *, growth_fn=None, bucket_fn=None, sample_batch=None, stamp_fn=_stamp):
        self.window, self.tolerance, self.baseline_delay = window, tolerance, baseline_delay
        self.now, self.limit = now, limit
        self.sample_status, self.focus_post_id = sample_status, focus_post_id
        self.sample_stage = sample_stage
        self.stages, self.statuses, self.rows = Counter(), Counter(), []
        self.growth_fn, self.bucket_fn, self.sample_batch = growth_fn, bucket_fn, sample_batch
        self.stamp_fn = stamp_fn
        self.coverage = defaultdict(Counter)
        self.baseline_delays, self.endpoint_offsets = Counter(), Counter()

    def facts(self, post, samples):
        """The snapshot-dependent part of add(); independent of ``now``.

        Returns (has_samples, baseline, windows) where windows holds, for the
        30/60/180 minute windows, (matched, interaction_delta known, negative).
        """
        created = self.stamp_fn(post["created_at"])
        baseline = None
        if created is not None:
            baseline = next((stamp for stamp, _ in samples
                             if created <= stamp <= created + timedelta(minutes=self.baseline_delay)), None)
        windows = []
        if self.growth_fn:
            for minutes in (30, 60, 180):
                matched, _ = self.growth_fn(post, samples, minutes, minutes / 5, self.baseline_delay)
                windows.append((matched is not None,
                                matched is not None and matched['interaction_delta'] is not None,
                                matched is not None and matched['has_negative_delta']))
        return bool(samples), baseline, tuple(windows)

    def add(self, post, samples, growth, reason):
        self.apply(post, self.facts(post, samples), growth)

    def apply(self, post, facts, growth):
        has_samples, baseline, window_facts = facts
        self.stages["total"] += 1
        stages = {"total"}
        created = self.stamp_fn(post["created_at"])
        if has_samples:
            self.stages["with_counters"] += 1
            stages.add("with_counters")
        if baseline is not None:
            self.stages["early_baseline"] += 1
            stages.add("early_baseline")
        if growth is not None:
            self.stages["window_matched"] += 1
            stages.add("window_matched")
            if growth["interaction_delta"] is not None:
                self.stages["complete_metrics"] += 1
                stages.add("complete_metrics")
                if not growth["has_negative_delta"]:
                    self.stages["nonnegative"] += 1
                    stages.add("nonnegative")

        if not has_samples:
            status = "no_counters"
        elif created is None:
            status = "missing_created_at"
        elif baseline is None:
            status = "no_early_baseline"
        elif growth is None:
            deadline = baseline + timedelta(minutes=self.window + self.tolerance)
            status = "waiting_endpoint" if self.now <= deadline else "missed_endpoint"
        elif growth["interaction_delta"] is None:
            status = "missing_metrics"
        elif growth["has_negative_delta"]:
            status = "negative_delta"
        else:
            status = "eligible"
        self.statuses[status] += 1
        batch = self.bucket_fn(created) if created and self.bucket_fn else None
        windows = []
        if self.growth_fn:
            for minutes, (matched, complete, negative) in zip((30, 60, 180), window_facts):
                tolerance = minutes / 5
                # Only completed observation opportunities enter the rate denominator.
                # Without an early baseline, use the latest allowable baseline deadline.
                deadline = (baseline or (created + timedelta(minutes=self.baseline_delay) if created else None))
                mature = deadline is not None and self.now > deadline + timedelta(minutes=minutes + tolerance)
                valid = matched and complete and not negative
                state = ('eligible' if valid else 'missing_metrics' if matched and not complete
                         else 'negative_delta' if matched else 'waiting_endpoint' if not mature and baseline
                         else 'missed_endpoint' if baseline else status)
                windows.append({'minutes': minutes, 'status': state, 'mature': mature})
                if batch:
                    cell = self.coverage[(batch, minutes)]
                    cell['total'] += 1
                    cell['mature'] += int(mature)
                    cell['eligible'] += int(mature and valid)
                    cell['pending'] += int(not mature)
                    cell['missing_baseline'] += int(mature and baseline is None)
                    cell['missed_endpoint'] += int(mature and baseline is not None and not matched)
                    cell['invalid_metrics'] += int(mature and matched and not valid)
        if baseline and created:
            delay = (baseline - created).total_seconds() / 60
            self.baseline_delays[int(delay // 2) * 2] += 1
        if growth:
            offset = round(growth['elapsed_minutes'] - self.window)
            self.endpoint_offsets[offset] += 1
        if ((self.sample_status is None or status == self.sample_status)
                and (self.sample_stage is None or self.sample_stage in stages)
                and (self.sample_batch is None or batch == self.sample_batch)):
            # Only detail_limit rows are shown; build each dict after selection.
            # Normalized UTC microsecond text sorts exactly like the datetime.
            self.rows.append(((created is not None, created, post["post_id"]),
                              (post, status, created, baseline, growth, batch, windows)))

    @staticmethod
    def _row(post, status, created, baseline, growth, batch, windows):
        return {
            "post_id": post["post_id"], "status": status,
            "created_at": normalize_timestamp(created) if created else None,
            "author_id": post["author_id"], "username": post["username"],
            "body_preview": _preview(post),
            "baseline_at": normalize_timestamp(baseline) if baseline else None,
            "endpoint_at": growth["endpoint_at"] if growth else None,
            "batch": batch, "windows": windows,
        }

    def result(self):
        self.rows.sort(key=lambda row: row[0])
        selected = self.rows[:self.limit]
        if self.focus_post_id is not None and not any(key[2] == self.focus_post_id for key, _ in selected):
            focus = next((row for row in self.rows if row[0][2] == self.focus_post_id), None)
            if focus is not None:
                selected = [*selected[:self.limit - 1], focus]
        return {
            "stages": [{"key": key, "label": label, "count": self.stages[key]} for key, label in SAMPLE_STAGES.items()],
            "statuses": [{"key": key, "label": label, "count": self.statuses[key]} for key, label in SAMPLE_STATUSES.items()],
            "posts": [self._row(*payload) for _, payload in selected],
            "posts_total": len(self.rows), "truncated": len(self.rows) > self.limit,
            "selected_stage": self.sample_stage, "selected_status": self.sample_status,
            "coverage": {
                "cells": [{"batch": batch, "minutes": minutes, **dict(counts),
                           "rate": counts['eligible'] / counts['mature'] if counts['mature'] else None}
                          for (batch, minutes), counts in sorted(self.coverage.items())],
                "baseline_delays": [{"minutes": minutes, "count": count} for minutes, count in sorted(self.baseline_delays.items())],
                "endpoint_offsets": [{"minutes": minutes, "count": count} for minutes, count in sorted(self.endpoint_offsets.items())],
            },
        }


def _trending_points(conn, post_ids):
    """Yield normalized, deduplicated points with only one pending observation.

    The database sorts by the full UTC microsecond key, not julianday's rounded
    value or the original timestamp spelling. Last valid ID wins at one instant
    for one source, matching the historical ingestion contract.
    """
    for batch in _chunks(post_ids):
        placeholders = ",".join("?" for _ in batch)
        query = f"""SELECT post_id,rank_position,global_hot_score,source,
                           ctn_timestamp(observed_at) AS observed_key
                    FROM feed_observations WHERE source IN ('trending','browser_trending')
                    AND post_id IN ({placeholders})
                    ORDER BY post_id,observed_key,source,id"""
        pending_key = pending = None
        with iter_rows(conn, query, batch) as cursor:
            for record in cursor:
                rank = record["rank_position"]
                if (isinstance(rank, bool) or not isinstance(rank, (int, float))
                        or not math.isfinite(rank) or rank <= 0 or not float(rank).is_integer()):
                    continue
                observed_at = record["observed_key"]
                if observed_at is None:
                    continue
                hot_score = record["global_hot_score"]
                if (isinstance(hot_score, bool) or not isinstance(hot_score, (int, float))
                        or not math.isfinite(hot_score)):
                    hot_score = None
                key = (record["post_id"], observed_at, record["source"])
                if pending_key is not None and key != pending_key:
                    yield pending_key[0], pending
                pending_key = key
                pending = {"observed_at": observed_at, "rank": int(rank),
                           "hot_score": hot_score, "source": record["source"]}
            if pending_key is not None:
                yield pending_key[0], pending


def trending_analysis(conn, posts, detail_limit, focus_post_id=None, *, stamp_fn=_stamp):
    """Full-history summaries with bounded top trajectories and uniform details.

    Pass one retains summary state and at most detail_limit points per post,
    keeping only the best requested trajectories. Pass two reads selected long
    histories and picks the same evenly spaced observation indexes as before.
    A single post with millions of observations never requires a history buffer.
    """
    post_map = {post["post_id"]: post for post in posts}
    selected, focus = [], None
    observations = repeated_posts = total_posts = 0
    first_observed_at = last_observed_at = None
    with closing(_trending_points(conn, post_map)) as points:
        for post_id, records in groupby(points, key=lambda row: row[0]):
            first = last = None
            count = best_rank = 0
            shown_points = []
            for _, point in records:
                if first is None:
                    first = point
                    best_rank = point["rank"]
                last = point
                count += 1
                best_rank = min(best_rank, point["rank"])
                if count <= detail_limit:
                    shown_points.append(point)
                elif shown_points:
                    # Its final count is needed for exact uniform sampling.
                    # Drop this bounded prefix and rescan only if selected.
                    shown_points = []
            observations += count
            total_posts += 1
            repeated_posts += first["observed_at"] != last["observed_at"]
            first_observed_at = min(first_observed_at, first["observed_at"]) if first_observed_at else first["observed_at"]
            last_observed_at = max(last_observed_at, last["observed_at"]) if last_observed_at else last["observed_at"]
            post = post_map[post_id]
            created = stamp_fn(post["created_at"])
            trajectory = {
                "post_id": post_id, "author_id": post["author_id"], "username": post["username"],
                "body_preview": _preview(post), "created_at": normalize_timestamp(created) if created else None,
                "best_rank": best_rank,
                "first_rank": first["rank"], "last_rank": last["rank"],
                "rank_change": first["rank"] - last["rank"],
                "first_observed_at": first["observed_at"], "last_observed_at": last["observed_at"],
                "observed_span_minutes": round((stamp_fn(last["observed_at"]) - stamp_fn(first["observed_at"])).total_seconds() / 60, 3),
                "observations": count, "points": shown_points,
                "points_total": count, "points_truncated": count > detail_limit,
            }
            if post_id == focus_post_id:
                focus = trajectory
            priority = (last["observed_at"], count, -best_rank, -post_id)
            if len(selected) < detail_limit:
                heapq.heappush(selected, (priority, trajectory))
            elif priority > selected[0][0]:
                heapq.heapreplace(selected, (priority, trajectory))
    # Keep recent observations reachable when detail_limit excludes older history.
    trajectories = [row for _, row in sorted(selected, key=lambda entry: entry[0], reverse=True)]
    if focus is not None and not any(row["post_id"] == focus_post_id for row in trajectories):
        trajectories = [*trajectories[:detail_limit - 1], focus]
    trajectory_map = {row["post_id"]: row for row in trajectories if row["points_truncated"]}
    with closing(_trending_points(conn, trajectory_map)) as points:
        for post_id, records in groupby(points, key=lambda row: row[0]):
            trajectory = trajectory_map[post_id]
            count = trajectory["points_total"]
            indexes = (set(range(count)) if count <= detail_limit else {count - 1} if detail_limit == 1 else
                       {round(index * (count - 1) / (detail_limit - 1)) for index in range(detail_limit)})
            for index, (_, point) in enumerate(records):
                if index in indexes:
                    trajectory["points"].append(point)
    return {
        "summary": {"posts": total_posts, "observations": observations, "repeated_posts": repeated_posts,
                    "first_observed_at": first_observed_at, "last_observed_at": last_observed_at},
        "trajectories": trajectories,
        "total_posts": total_posts, "truncated": total_posts > detail_limit,
    }


def reply_analysis(conn, posts, detail_limit, focus_post_id=None, *, stamp_fn=_stamp):
    """Reply components include ancestors as context; all summaries count only scoped posts."""
    scoped = {post["post_id"]: post for post in posts}
    records = dict(scoped)
    outside_parents = {post["reply_to_post_id"] for post in posts
                       if post["reply_to_post_id"] is not None and post["reply_to_post_id"] not in scoped}
    # UNION visits each ancestor once per batch, so cycles and chains beyond Python's recursion
    # limit terminate. No unrelated descendants or sibling threads enter the selected scope.
    for batch in _chunks(outside_parents):
        placeholders = ",".join("?" for _ in batch)
        query = f"""WITH RECURSIVE ancestry(post_id) AS (
                       SELECT post_id FROM posts WHERE post_id IN ({placeholders})
                       UNION
                       SELECT p.reply_to_post_id FROM posts p JOIN ancestry x ON p.post_id=x.post_id
                       WHERE p.reply_to_post_id IS NOT NULL
                    ) SELECT p.post_id,p.author_id,p.reply_to_post_id,p.body,p.created_at,a.username
                      FROM ancestry x JOIN posts p ON p.post_id=x.post_id
                      LEFT JOIN accounts a ON a.account_id=p.author_id"""
        for record in conn.execute(query, batch):
            records[record["post_id"]] = dict(record)
    missing_ids = {post["reply_to_post_id"] for post in records.values()
                   if post["reply_to_post_id"] is not None and post["reply_to_post_id"] not in records}
    for post_id in missing_ids:
        records[post_id] = {"post_id": post_id, "reply_to_post_id": None, "author_id": None,
                            "username": None, "body": None, "created_at": None}
    parents = {post_id: post["reply_to_post_id"] for post_id, post in records.items()}
    resolved, cycle_breaks = {}, set()
    for start in records:
        if start in resolved:
            continue
        while start not in resolved:
            path, positions, cursor = [], {}, start
            while cursor is not None and cursor not in resolved and cursor not in positions:
                positions[cursor] = len(path)
                path.append(cursor)
                cursor = parents[cursor]
            if cursor in positions:
                root = min(path[positions[cursor]:])
                parents[root] = None
                cycle_breaks.add(root)
                continue
            if cursor is None:
                root, depth = path[-1], -1
            else:
                root, depth = resolved[cursor]
            for post_id in reversed(path):
                depth += 1
                resolved[post_id] = (root, depth)

    components = defaultdict(list)
    for post_id in scoped:
        components[resolved[post_id][0]].append(post_id)
    threads = []
    for root_id, members in components.items():
        root = records[root_id]
        threads.append({
            "root_id": root_id, "posts": len(members),
            "replies": sum(scoped[post_id]["reply_to_post_id"] is not None for post_id in members),
            "participants": len({scoped[post_id]["author_id"] for post_id in members if scoped[post_id]["author_id"] is not None}),
            "max_depth": max(resolved[post_id][1] for post_id in members),
            "root_missing": root_id in missing_ids, "body_preview": _preview(root), "username": root["username"],
        })
    threads.sort(key=lambda row: (-row["posts"], -row["max_depth"], row["root_id"]))
    selected_root_id = resolved[focus_post_id][0] if focus_post_id in resolved else (threads[0]["root_id"] if threads else None)
    selected_ids = [post_id for post_id in records if resolved[post_id][0] == selected_root_id]
    selected_ids.sort(key=lambda post_id: (resolved[post_id][1], post_id))
    focus_path = []
    if focus_post_id in selected_ids:
        cursor = focus_post_id
        while cursor is not None:
            focus_path.append(cursor)
            cursor = parents[cursor]
        focus_path.reverse()
    path_truncated = len(focus_path) > detail_limit
    if path_truncated:
        shown_ids = ([focus_path[0]] + focus_path[-(detail_limit - 1):]) if detail_limit > 1 else [focus_post_id]
    else:
        shown = set(focus_path)
        shown_ids = focus_path + [post_id for post_id in selected_ids if post_id not in shown][:detail_limit - len(focus_path)]
    shown_ids.sort(key=lambda post_id: (resolved[post_id][1], post_id))
    shown_set = set(shown_ids)
    nodes = []
    for post_id in shown_ids:
        post = records[post_id]
        created = stamp_fn(post["created_at"])
        nodes.append({
            "post_id": post_id, "parent_id": parents[post_id], "author_id": post["author_id"],
            "username": post["username"], "body_preview": _preview(post),
            "created_at": normalize_timestamp(created) if created else None,
            "depth": resolved[post_id][1], "in_scope": post_id in scoped, "missing": post_id in missing_ids,
            "original_parent_id": post["reply_to_post_id"], "cycle_broken": post_id in cycle_breaks,
            "context_gap": parents[post_id] is not None and parents[post_id] not in shown_set,
        })
    shown_threads = threads[:detail_limit]
    if selected_root_id is not None and not any(row["root_id"] == selected_root_id for row in shown_threads):
        focused_thread = next(row for row in threads if row["root_id"] == selected_root_id)
        shown_threads = [*shown_threads[:detail_limit - 1], focused_thread]
    return {
        "summary": {"reply_posts": sum(post["reply_to_post_id"] is not None for post in posts),
                    "threads": len(threads), "missing_parents": len(missing_ids),
                    "max_depth": max((resolved[post_id][1] for post_id in scoped), default=0),
                    "participants": len({post["author_id"] for post in posts if post["author_id"] is not None}),
                    "cycle_breaks": len(cycle_breaks)},
        "threads": shown_threads, "threads_total": len(threads), "threads_truncated": len(threads) > detail_limit,
        "selected_root_id": selected_root_id, "nodes": nodes,
        "total_nodes": len(selected_ids), "nodes_truncated": len(selected_ids) > detail_limit,
        "path_truncated": path_truncated, "omitted_ancestor_count": max(0, len(focus_path) - detail_limit),
    }
