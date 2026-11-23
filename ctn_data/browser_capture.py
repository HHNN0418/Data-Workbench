"""Local, paired-extension observations; remote pages remain untrusted input."""
import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from .db import begin_write, connect, utc_now
from .ingest import save_posts, save_counters, observed_time
from .imports import import_activity_csv, add_score_snapshot, HEADERS, decimal_text, normalize_timestamp


def _portal_csv(content):
    if not isinstance(content, str):
        raise ValueError('活动 CSV 必须为文本')
    if len(content.encode('utf-8')) > 20 * 1024 * 1024:
        raise ValueError('单次 CSV 导入上限为 20 MiB')
    output = io.StringIO(newline='')
    canonical = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=HEADERS)
    digest_writer = csv.DictWriter(canonical, fieldnames=HEADERS, quoting=csv.QUOTE_ALL, lineterminator='\r\n')
    writer.writeheader()
    digest_writer.writeheader()
    latest = None
    try:
        reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff'), newline=''), strict=True)
        if reader.fieldnames != HEADERS:
            raise ValueError('活动 CSV 格式不匹配')
        for number, row in enumerate(reader, 2):
            # Validate before filtering or reserializing; DictWriter otherwise
            # silently turns truncated fields into empty, accepted values.
            if None in row or any(row.get(key) is None for key in HEADERS):
                raise ValueError(f'活动 CSV 第 {number} 行列数错误')
            if not any(row.values()) or row['Type'].strip().upper() == 'TEAMCODE':
                continue
            writer.writerow(row)
            digest_writer.writerow(row)
            try:
                stamp = normalize_timestamp(row['When (UTC)'])
                latest = max(latest, stamp) if latest else stamp
            except ValueError:
                pass  # The importer preserves invalid event times explicitly.
    except csv.Error as exc:
        raise ValueError(f'活动 CSV 格式错误：{exc}') from exc
    digest = hashlib.sha256(canonical.getvalue().removesuffix('\r\n').encode('utf-8')).hexdigest()
    return output.getvalue(), latest, digest


_PREVIEW_WHEN = re.compile(r'\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}')
_PREVIEW_UTC = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z')
_PREVIEW_EVENT = re.compile(r'\[([A-Z_]{2,40})\]\s*(.+)', re.S)


def _save_preview(conn, rows, stamp):
    """Keep the live activity table apart from official_activity (see npc_calibration.preview_rows).

    The CSV export lags the table by up to two hours and its message text may
    differ in spacing, so these rows never enter official_activity: readers use
    them only for hours the export has not reached yet.
    """
    from zoneinfo import ZoneInfo
    from .npc_calibration import PREVIEW_KEY
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('活动表最多 100 行')
    kept = []
    for row in rows:
        row = row if isinstance(row, dict) else {}
        # Extension 1.3.5 sends the row's data-utc time; 1.3.4 sent Sydney text.
        when_utc, when, event = row.get('when_utc'), row.get('when'), row.get('event')
        if not isinstance(event, str):
            raise ValueError('活动表行格式不对')
        if isinstance(when_utc, str) and _PREVIEW_UTC.fullmatch(when_utc.strip()):
            occurred = datetime.fromisoformat(when_utc.strip().replace('Z', '+00:00'))
        elif isinstance(when, str) and _PREVIEW_WHEN.fullmatch(when.strip()):
            occurred = datetime.strptime(when.strip(), '%Y/%m/%d %H:%M:%S').replace(tzinfo=ZoneInfo('Australia/Sydney'))
        else:
            raise ValueError('活动表行格式不对')
        match = _PREVIEW_EVENT.fullmatch(event.strip())
        if not match or match.group(1) == 'TEAMCODE':
            continue
        kept.append({'occurred_at': occurred.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                     'event_type': match.group(1), 'message': match.group(2)[:2000]})
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (PREVIEW_KEY, json.dumps({'observed_at': stamp, 'rows': kept}, ensure_ascii=False)))
    return len(kept)


def capture(db, records):
    if not isinstance(records,list) or len(records)>50:
        raise ValueError('每批最多 50 条浏览器记录')
    conn=connect(db)
    accepted, errors = [], []
    try:
        for record in records:
            rid=record.get('id','') if isinstance(record,dict) else ''
            try:
                if not re.fullmatch(r'[a-zA-Z0-9-]{8,80}',rid): raise ValueError('记录 ID 无效')
                with conn:
                    # Inside the block: a lock timeout rolls back this record's
                    # transaction instead of leaving it aborted for the batch.
                    begin_write(conn, 'official_imports')
                    if conn.execute('SELECT 1 FROM checkpoints WHERE key=?',('browser_receipt:'+rid,)).fetchone():
                        accepted.append(rid); continue
                    stamp=observed_time(record['observed_at'])
                    if datetime.fromisoformat(stamp.replace('Z','+00:00'))>datetime.now(timezone.utc)+timedelta(minutes=5):
                        raise ValueError('采集时间位于未来')
                    kind=record['kind']; data=record['data']; receipt={'kind':kind,'observed_at':stamp}
                    if not isinstance(data,dict): raise ValueError('记录内容必须为对象')
                    if kind=='post':
                        fields=('post_id','author_id','body','kind','reply_to_post_id','root_post_id','repost_of_post_id','visibility','created_at','edited_at','embed','rank_position','global_hot_score')
                        data={key:data[key] for key in fields if key in data}
                        if data.get('visibility')!='public': raise ValueError('只接受公开帖子')
                        source=record.get('source','browser_detail')
                        if source not in ('browser_latest','browser_trending','browser_detail'): raise ValueError('来源无效')
                        save_posts(conn,[data],source,stamp)
                    elif kind=='counter':
                        fields=('post_id','like_count','reply_count','repost_count','view_count','score_inputs_updated_at')
                        data={key:data[key] for key in fields if key in data}
                        pid=data['post_id']
                        post=conn.execute('SELECT visibility FROM posts WHERE post_id=?',(pid,)).fetchone()
                        if not post or post['visibility']!='public': raise ValueError('互动快照需要已知公开帖子')
                        save_counters(conn,pid,data,stamp,source='browser:counters')
                    elif kind=='portal':
                        team_id=data['team_id']
                        paired=conn.execute("SELECT value FROM meta WHERE key='browser_team_id'").fetchone()
                        if not paired or str(team_id)!=paired['value']: raise ValueError('队伍与本机绑定不一致')
                        total=decimal_text(data['total'])
                        content, latest, digest = _portal_csv(data['activity_csv'])
                        activity=import_activity_csv(db,content,f'browser-team-{team_id}.csv',connection=conn)
                        snapshot=add_score_snapshot(db,total,stamp,f'browser:team:{team_id}','赛事队伍页面 Score；浏览器观测',connection=conn)
                        lag = (datetime.fromisoformat(stamp.replace('Z','+00:00')) - datetime.fromisoformat(latest.replace('Z','+00:00'))).total_seconds() if latest else None
                        receipt.update(team_id=int(team_id),total=total,score_snapshot_id=snapshot['id'],activity_rows=activity['rows'],activity_inserted=activity['inserted'],activity_duplicates=activity['duplicates'],activity_latest_at=latest,activity_lag_seconds=lag,activity_digest=digest)
                        if 'activity_recent' in data:
                            # A malformed live table never costs the CSV import.
                            try:
                                receipt['activity_preview_rows']=_save_preview(conn,data['activity_recent'],stamp)
                            except ValueError as exc:
                                receipt['activity_preview_error']=str(exc)[:120]
                    elif kind=='leaderboard':
                        # Every team's score from the competition page (extension 1.4.0, D104).
                        from .leaderboard import capture as capture_leaderboard
                        receipt.update(capture_leaderboard(conn,data,stamp))
                    else: raise ValueError('不支持的浏览器记录')
                    conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)',
                                     ('browser_receipt:'+rid,json.dumps(receipt),utc_now()))
                accepted.append(rid)
            except (ValueError,KeyError,TypeError,sqlite3.Error) as exc:
                # Database availability/locking failures are not bad records.
                # Keep their stable receipt IDs retryable after rollback; only
                # validated input/constraint failures may leave the queue.
                retryable = isinstance(exc,sqlite3.Error) and not isinstance(exc,(sqlite3.IntegrityError,sqlite3.DataError))
                errors.append({'id':rid,'error':str(exc)[:180],'retryable':retryable})
        with conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('browser_last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps({'at':utc_now(),'accepted':len(accepted),'errors':len(errors)}),))
        return dict(accepted=accepted,errors=errors)
    finally: conn.close()
