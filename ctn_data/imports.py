"""Lossless, conservative imports of the team's official portal activity export.

Only an explicitly recorded points amount is treated as an official amount.
Counts of views, reports or contributions are never converted into booked points.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import re

from .db import connect, initialize, raw_record, utc_now

HEADERS = ["When (UTC)", "Type", "Message"]
_NUMBER = r"[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def decimal_text(value) -> str:
    """Finite base-10 values only; avoid binary-float arithmetic."""
    if isinstance(value, bool):
        raise ValueError("积分必须为有限十进制数字")
    text = str(value).replace(",", "").replace("−", "-")
    if len(text) > 256:
        raise ValueError("积分数字过长")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("积分必须为有限十进制数字") from None
    if not amount.is_finite():
        raise ValueError("积分必须为有限十进制数字")
    if amount and abs(amount.adjusted()) > 100:
        raise ValueError("积分数字超出支持范围")
    # Decimal.normalize() can round under the current context; formatting the
    # original coefficient preserves all imported decimal places exactly.
    rendered = format(amount, "f")
    return (rendered.rstrip("0").rstrip(".") if "." in rendered else rendered) if amount else "0"


def normalize_timestamp(value, *, allow_none=False) -> str | None:
    if value is None or str(value).strip() == "":
        if allow_none:
            return None
        raise ValueError("缺少时间")
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        parsed = None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (ValueError, TypeError, OverflowError):
                for fmt in ("%Y-%m-%d %H:%M:%S UTC", "%Y-%m-%d %H:%M UTC",
                            "%d/%m/%Y %H:%M:%S", "%d %b %Y %H:%M:%S",
                            "%b %d, %Y, %I:%M %p"):
                    try:
                        parsed = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        pass
        if parsed is None:
            raise ValueError("无法解析时间；请使用 ISO8601")
    # Portal column explicitly states UTC. Manual naive dates are also documented as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_activity(event_type: str, message: str) -> dict:
    """Parse a reported amount, never infer an amount from engagement counts."""
    kind = event_type.strip().upper()
    lowered = message.lower()
    # Old exports used SCORE_NARRATIVE for several tracks. Explicit row semantics
    # therefore take precedence over a historical type label.
    if "npc engagement" in lowered:
        track = "red"
    elif re.search(r"\bmoderation\b|\binauthentic (?:account )?report\b", lowered):
        track = "blue"
    elif re.search(r"\bpoll\b|\breferendum\b|\bpersuasion pool\b", lowered):
        track = "polling"
    elif "RED" in kind or "ENGAGEMENT" in kind:
        track = "red"
    elif "BLUE" in kind or "MODERATION" in kind:
        track = "blue"
    elif "POLL" in kind:
        track = "polling"
    else:
        track = None
    if kind in {"MEMBERSHIP", "TEAMCODE", "REGISTRATION", "TEAM", "ACCOUNT_CREATED"}:
        return dict(track=track, points_decimal=None, parse_status="non_scoring",
                    parse_note="队伍管理事件；不将其视为积分")
    # A multiplier such as '(×2 points)' is not a recorded amount. Neither are
    # thresholds, pool descriptions, or '312 changes of mind and 840 contributions'.
    text = re.sub(r"[×x]\s*\d+(?:\.\d+)?\s*points?", "", message, flags=re.I)
    matches = {}
    patterns = [
        rf"\b(?:points?\s+(?:awarded|earned|credited|deducted)|score\s+delta|points?)\s*[:=]\s*({_NUMBER})\b",
        rf"\b(?:awarded|earned|credited|gained|received)\s+({_NUMBER})\s+points?\b",
        rf"\b(?:lost|deducted|penalty(?:\s+of)?)\s+({_NUMBER})\s+points?\b",
        rf"(?<![\w×x])([+\-−]\d+(?:\.\d+)?)\s+points?\b",
    ]
    for index, pattern in enumerate(patterns):
        for match in re.finditer(pattern, text, flags=re.I):
            amount = Decimal(decimal_text(match.group(1)))
            if index == 2 or (index == 0 and "deducted" in match.group(0).lower()):
                amount = -abs(amount)
            # Multiple patterns can recognize the very same numeric span. Merge
            # only that overlap; two independent '+5 points' remain ambiguous.
            span = match.span(1)
            if span in matches and matches[span] != amount:
                matches[span] = None
            else:
                matches[span] = amount
    # Avoid interpreting quoted examples, conditional amounts, or cumulative totals.
    hypothetical = re.search(r"\b(?:example|would|could|potential|threshold|up to|if|balance)\b|\btotal\s+(?:score|points?)\b", text, re.I)
    if len(matches) == 1 and next(iter(matches.values())) is not None and not hypothetical:
        return dict(track=track, points_decimal=decimal_text(next(iter(matches.values()))),
                    parse_status="parsed", parse_note="仅采用日志明确记载的积分金额；未据事件计数推算")
    note = "日志没有明确可解析的实得积分；保留原文，不能由互动计数推断"
    if len(matches) > 1:
        note = "日志包含多个不同积分金额，无法确认净入账金额"
    elif hypothetical:
        note = "日志含条件、示例或阈值表达，不作为确定入账金额"
    return dict(track=track, points_decimal=None, parse_status="unparsed", parse_note=note)


def import_activity_csv(db_path, content: str, filename="activity.csv", *, connection=None) -> dict:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("CSV 为空")
    if len(content.encode("utf-8")) > 20 * 1024 * 1024:
        raise ValueError("单次 CSV 导入上限为 20 MiB")
    original = content
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""), strict=True)
    if reader.fieldnames != HEADERS:
        raise ValueError("CSV 必须包含且按顺序排列：When (UTC),Type,Message")
    rows = []
    occurrences = Counter()
    try:
        for row_number, row in enumerate(reader, 2):
            if None in row or any(row.get(key) is None for key in HEADERS):
                raise ValueError(f"CSV 第 {row_number} 行列数错误")
            if not any(row.values()):
                continue
            event_type = row["Type"].strip()
            message = row["Message"]  # Preserve exact original message, including newlines.
            stamp = row["When (UTC)"].strip()
            parsed = parse_activity(event_type, message)
            try:
                occurred_at = normalize_timestamp(stamp)
            except ValueError:
                occurred_at = None
                # Time validity must not erase the independent determination
                # that a membership/account event cannot affect points.
                if parsed["parse_status"] != "non_scoring":
                    parsed["parse_status"] = "invalid_time"
                parsed["points_decimal"] = None
                parsed["parse_note"] += "；无法解析 UTC 时间，保留原始字段，未计入积分对账"
            key = json.dumps([occurred_at or stamp, event_type, message], ensure_ascii=False,
                             separators=(",", ":"))
            fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
            occurrences[fingerprint] += 1
            rows.append((row_number, occurred_at, event_type, message, fingerprint,
                         occurrences[fingerprint], parsed, dict(row)))
    except csv.Error as exc:
        raise ValueError(f"CSV 格式错误：{exc}") from exc
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    from .transactions import write_connection
    with write_connection(db_path, connection) as conn:
        previous = conn.execute("SELECT id,row_count FROM activity_imports WHERE sha256=?", (digest,)).fetchone()
        if previous:
            return {"import_id": previous["id"], "rows": previous["row_count"], "inserted": 0,
                    "duplicates": previous["row_count"], "already_imported": True}
        now = utc_now()
        cursor = conn.execute("INSERT INTO activity_imports(sha256,filename,imported_at,row_count) VALUES(?,?,?,?)",
                              (digest, str(filename), now, len(rows)))
        import_id = cursor.lastrowid
        inserted = 0
        statuses = Counter()
        added_rows, row_numbers = [], []
        for row_number, occurred_at, event_type, message, fingerprint, occurrence, parsed, original_row in rows:
            cursor = conn.execute("""INSERT OR IGNORE INTO official_activity
                (import_id,row_number,occurred_at,event_type,message,fingerprint,occurrence,
                 track,points_decimal,parse_status,parse_note) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (import_id, row_number, occurred_at, event_type, message, fingerprint, occurrence,
                 parsed["track"], parsed["points_decimal"], parsed["parse_status"], parsed["parse_note"]))
            inserted += cursor.rowcount
            if cursor.rowcount:
                statuses[parsed["parse_status"]] += 1
                added_rows.append(original_row)
                row_numbers.append(row_number)
        if added_rows:
            # Keep every newly observed field (including invalid timestamps,
            # whitespace and embedded newlines), once. The full source digest
            # and original row numbers remain available for provenance.
            delta = io.StringIO(newline="")
            writer = csv.DictWriter(delta, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(added_rows)
            raw_id = raw_record(conn, "official_activity_delta_csv", str(filename),
                {"format": "new_rows_v1", "csv": delta.getvalue(),
                 "original_sha256": digest, "original_row_count": len(rows),
                 "original_row_numbers": row_numbers}, now)
            conn.execute("INSERT INTO activity_import_payloads(import_id,raw_id) VALUES(?,?)",
                         (import_id, raw_id))
        return {"import_id": import_id, "rows": len(rows), "inserted": inserted,
                "duplicates": len(rows) - inserted, "already_imported": False,
                "statuses": dict(statuses),
                "deduplication": "同一时间/类型/原文按单次导出中的最大出现次数保留；无事件ID时无法区分完全相同的独立事件"}


def add_score_snapshot(db_path, total, observed_at=None, source="manual", note="", *, connection=None) -> dict:
    amount = decimal_text(total)
    stamp = normalize_timestamp(observed_at or utc_now())
    key = hashlib.sha256(json.dumps([stamp, str(source), amount], separators=(',', ':')).encode()).hexdigest()
    from .transactions import write_connection
    with write_connection(db_path, connection) as conn:
        existing = conn.execute("SELECT s.* FROM score_observation_keys k JOIN score_snapshots s ON s.id=k.snapshot_id WHERE k.observation_key=?", (key,)).fetchone()
        if not existing:
            existing = conn.execute("SELECT * FROM score_snapshots WHERE observed_at=? AND source=? AND total_decimal=? ORDER BY id LIMIT 1", (stamp, str(source), amount)).fetchone()
        if existing:
            conn.execute("INSERT OR IGNORE INTO score_observation_keys(observation_key,snapshot_id) VALUES(?,?)", (key, existing['id']))
            return {**dict(existing), "already_exists": True}
        cursor = conn.execute("INSERT INTO score_snapshots(observed_at,total_decimal,source,note) VALUES(?,?,?,?)", (stamp, amount, str(source), str(note)))
        snapshot_id = cursor.lastrowid
        conn.execute("INSERT INTO score_observation_keys(observation_key,snapshot_id) VALUES(?,?)", (key, snapshot_id))
        return {"id": snapshot_id, "observed_at": stamp, "total_decimal": amount,
                "source": str(source), "note": str(note), "already_exists": False}
