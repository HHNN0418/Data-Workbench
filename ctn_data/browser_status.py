"""One database-confirmed freshness model for browser portal collection."""
from datetime import datetime, timedelta, timezone
import json
import re

HEARTBEAT_SECONDS = 90
FRESHNESS_SECONDS = 300


def _instant(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def diagnostic_text(value, label):
    if value is None:
        return ''
    if (not isinstance(value, str) or len(value) > 180 or '\n' in value or '\r' in value
            or re.search(r'<(?:!doctype|/?(?:html|body|script)\b)', value, re.I)):
        raise ValueError(label + '必须为不超过 180 字的简短说明')
    return value


def page_diagnostics(page, *, now=None):
    """Allow only bounded scalar diagnostics, never source HTML or CSV bodies."""
    now = now or datetime.now(timezone.utc)
    clean = {}
    for key in ('last_attempt_at', 'last_collected_at'):
        if key not in page:
            continue
        value = page[key]
        if value is None:
            clean[key] = None
            continue
        stamp = _instant(value) if isinstance(value, str) and len(value) <= 64 else None
        if stamp is None or stamp > now + timedelta(minutes=5):
            raise ValueError(key + '必须为有效的带时区采集时间')
        clean[key] = stamp.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    for key, lower, upper in (('http_status', 100, 599), ('activity_rows', 0, 1000000)):
        if key in page:
            value = page[key]
            if value is not None and (type(value) is not int or not lower <= value <= upper):
                raise ValueError(key + '无效')
            clean[key] = value
    if 'activity_digest' in page:
        value = page['activity_digest']
        if value is not None and (not isinstance(value, str) or not re.fullmatch('[a-fA-F0-9]{64}', value)):
            raise ValueError('activity_digest必须为 SHA256')
        clean['activity_digest'] = value.lower() if value else None
    if 'error' in page:
        clean['error'] = diagnostic_text(page['error'], '页面错误')
    if 'code' in page:
        value = page['code']
        if value is not None and (not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9_]{0,31}', value)):
            raise ValueError('页面状态代码无效')
        clean['code'] = value
    return clean


def latest_portal(connection):
    row = connection.execute("""SELECT value,updated_at FROM checkpoints
        WHERE key LIKE 'browser_receipt:%' AND json_extract(value,'$.kind')='portal'
        ORDER BY ctn_timestamp(json_extract(value,'$.observed_at')) DESC NULLS LAST,
                 updated_at DESC,key DESC LIMIT 1""").fetchone()
    return (json.loads(row['value']), row['updated_at']) if row else (None, None)


def collection_status(extension_id, heartbeat, portal, last_ingested_at, *, now=None):
    now = now or datetime.now(timezone.utc)
    heartbeat = heartbeat or {}
    page = next((item for item in heartbeat.get('pages', []) if item.get('role') == 'portal'), {})
    collected = portal.get('observed_at') if portal else None
    collected_at = _instant(collected)
    age = max(0, int((now - collected_at).total_seconds())) if collected_at else None
    result = dict(state=None, label=None, healthy=False, reason=None,
                  last_collected_at=collected, last_ingested_at=last_ingested_at,
                  age_seconds=age, freshness_seconds=FRESHNESS_SECONDS,
                  heartbeat_at=heartbeat.get('at'), last_attempt_at=page.get('last_attempt_at'),
                  http_status=page.get('http_status'), activity_rows=(portal or {}).get('activity_rows'),
                  error=heartbeat.get('error') or page.get('error') or None,
                  activity_digest=(portal or {}).get('activity_digest'),
                  activity_latest_at=(portal or {}).get('activity_latest_at'),
                  activity_lag_seconds=(portal or {}).get('activity_lag_seconds'))

    def state(name, label, reason):
        return {**result, 'state': name, 'label': label, 'reason': reason, 'healthy': name == 'healthy'}

    heartbeat_at = _instant(heartbeat.get('at'))
    message = page.get('error') or page.get('reason') or ''
    code = page.get('code')
    waiting_login = code == 'auth' or page.get('http_status') in (401, 403) or bool(re.search(r'登录|login|sign.?in', message, re.I))
    pause = page.get('pause_until')
    cooling = isinstance(pause, (int, float)) and pause > now.timestamp() * 1000
    manually_paused = bool(page and not page.get('enabled') and not waiting_login and not cooling
                           and not page.get('error') and not heartbeat.get('error')
                           and code not in ('auth', 'rate_limit', 'network', 'schema', 'buffer', 'error'))
    if not extension_id:
        return state('unpaired', '待连接', '尚未绑定 Chrome 扩展')
    if not heartbeat_at or (now - heartbeat_at).total_seconds() >= HEARTBEAT_SECONDS:
        return state('offline', '连接离线', '超过 90 秒未收到 Chrome 心跳')
    if heartbeat.get('buffer_full'):
        return state('buffer_full', '缓存已满', '浏览器缓存已满，等待恢复入库')
    if not heartbeat.get('auto_enabled') or manually_paused:
        return state('paused', '已暂停', '本队数据同步已暂停')
    if waiting_login:
        return state('waiting_login', '等待登录', message or '队伍页面需要重新登录')
    if cooling:
        return state('cooldown', '等待重试', message or '队伍页面处于重试等待期')
    if heartbeat.get('error') or page.get('error') or code in ('network', 'schema', 'buffer', 'error') or (page.get('http_status') or 0) >= 400:
        return state('error', '同步异常', heartbeat.get('error') or message or '队伍页面请求失败')
    page_collected_at = _instant(page.get('last_collected_at'))
    unconfirmed_page = code == 'pending' and (not collected_at or (page_collected_at and page_collected_at > collected_at))
    if heartbeat.get('queue_count', 0) > 0 or unconfirmed_page:
        return state('pending', '等待入库', '浏览器记录尚未确认入库')
    if not page or not collected_at:
        return state('starting', '等待首次采集', '等待队伍页面首次成功采集并入库')
    if age >= FRESHNESS_SECONDS:
        return state('stale', '数据已过期', '超过 5 分钟没有新的成功采集记录')
    return state('healthy', '同步正常', '最近 5 分钟内已成功采集并入库')
