"""Token-authenticated feed/counter workers; backend owns leases and writes."""
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi import Body, HTTPException, Request

from . import coordination, task_queue, feed_tasks
from .db import connect, utc_now
from .ingest import observed_time, save_counters
from .transport import rate_limit_delay
from .secret_files import read_secret

DEFAULT_TOKEN_PATH = Path(__file__).resolve().parent.parent / 'data/secrets/worker_token'
WORKER_PATHS = frozenset('/api/worker/' + action for action in ('claim', 'permit', 'result'))


def worker_authorized(request, token_path=DEFAULT_TOKEN_PATH):
    if request.method != 'POST' or request.url.path not in WORKER_PATHS:
        return False
    try:
        token = read_secret(token_path).strip()
    except (OSError, ValueError):
        return False
    supplied = request.headers.get('authorization', '')
    return len(token) >= 32 and hmac.compare_digest(supplied, 'Bearer ' + token)


def _identity(body):
    worker = body.get('worker', '')
    if not isinstance(worker, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', worker):
        raise HTTPException(400, 'Invalid worker identifier')
    return worker


def _leased(conn, body, worker):
    key, token = body.get('task_key'), body.get('lease_token')
    if not isinstance(key, str) or not isinstance(token, str):
        raise HTTPException(400, 'Missing task lease')
    row = conn.execute('SELECT * FROM collection_tasks WHERE task_key=?', (key,)).fetchone()
    if (row is None or row['kind'] not in ('counter', *feed_tasks.KINDS) or row['lease_owner'] != 'proxy:' + worker
            or not task_queue.owned(conn, key, token)):
        raise HTTPException(409, 'Task lease expired or reassigned')
    payload = json.loads(row['payload_json'])
    if row['kind'] in feed_tasks.KINDS:
        plan = payload.get('feed_plan')
        if not isinstance(plan, dict) or plan.get('kind') != row['kind']:
            raise HTTPException(409, 'Feed plan unavailable')
        return row, None, plan['path']
    post_id = payload.get('post_id')
    if isinstance(post_id, bool) or not isinstance(post_id, int) or post_id < 1:
        raise HTTPException(400, 'Invalid counter task')
    return row, post_id, f'/api/posts/{post_id}/counters'


def _enabled(conn):
    row=conn.execute("SELECT value FROM meta WHERE key='auto_collection'").fetchone()
    try:
        return bool(row and json.loads(row['value']).get('enabled') is True)
    except (TypeError,ValueError):
        return False


def _heartbeat(conn, worker, status, *, force=False, **fields):
    """No secret, IP or lease token is exposed in operational worker status."""
    key='worker_status:'+worker
    row=conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
    try:
        previous=json.loads(row['value']) if row else {}
    except (TypeError,ValueError):
        previous={}
    now=time.time()
    if not force and now-previous.get('last_seen_epoch',0)<15:
        return
    current={**previous,**fields,'worker':worker,'status':status,'last_seen_at':utc_now(),'last_seen_epoch':now}
    conn.execute('''INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value''',(key,json.dumps(current)))


def register_worker_routes(app, db_path, token_path=DEFAULT_TOKEN_PATH, config=None, *, is_enabled=None):
    """Register on the local FastAPI app; middleware must also allow this token."""
    def enabled(conn):
        # The server's effective switch includes -NoAutoCollect without
        # overwriting the user's saved preference for their next normal start.
        return is_enabled() if is_enabled is not None else _enabled(conn)

    def read_body(request, body):
        if not worker_authorized(request, token_path):
            raise HTTPException(401, 'Worker authentication required')
        limit = 5*1024*1024 if request.url.path.endswith('/result') else 65536
        if len(json.dumps(body, ensure_ascii=False,separators=(',',':')).encode()) > limit:
            raise HTTPException(413, 'Worker result too large')
        if not isinstance(body, dict):
            raise HTTPException(400, 'Expected JSON object')
        return body, _identity(body)

    @app.post('/api/worker/claim', tags=['Workers'])
    def claim(request: Request, body: dict = Body(...)):
        body, worker = read_body(request, body)
        conn = connect(db_path)
        try:
            if not enabled(conn):
                with conn: _heartbeat(conn,worker,'paused')
                return {'task':None,'paused':True,'retry_after_seconds':10}
            # A retry after a lost response returns the same lease, not more work.
            capabilities=body.get('capabilities',[])
            if not isinstance(capabilities,list) or any(not isinstance(cap,str) or cap not in ('counter','feed') for cap in capabilities):
                raise HTTPException(400,'Invalid worker capabilities')
            supports_feed = 'feed' in capabilities
            kinds = ['counter', *feed_tasks.KINDS] if supports_feed else ['counter']
            row = conn.execute("SELECT * FROM collection_tasks WHERE state='leased' AND lease_owner=? AND lease_until>? ORDER BY lease_until LIMIT 1", ('proxy:' + worker, time.time())).fetchone()
            if row and row['kind'] not in kinds:
                with conn:
                    task_queue.defer(conn,row['task_key'],row['lease_token'],time.time(),'capabilities_changed')
                row=None
            task = dict(row) if row else None
            for _ in range(6):
                if task is None:
                    # Feed is scarce and time-sensitive; counters fill the gaps.
                    feed_ready=coordination.cooldowns(conn)['feeds']/1000<=time.time()+8
                    task = task_queue.claim(conn, 'proxy:' + worker, list(feed_tasks.KINDS), lease_seconds=120) if supports_feed and feed_ready else None
                    task = task or task_queue.claim(conn, 'proxy:' + worker, ['counter'], lease_seconds=120)
                if not task or task['kind'] == 'counter':
                    break
                payload = json.loads(task['payload_json'])
                plan = payload.get('feed_plan') if row else None
                if plan is None:
                    proxy_config=dict(config or {})
                    page_limit=proxy_config.get('page_limit',50)
                    if isinstance(page_limit,int) and not isinstance(page_limit,bool):
                        proxy_config['page_limit']=min(100,page_limit)
                    plan = feed_tasks.prepare(conn, task['kind'], proxy_config)
                if plan is None:
                    task_queue.complete(conn, task['task_key'], task['lease_token'], {'status':'idle'},
                        next_due=feed_tasks.next_due(conn, task['kind'], {}))
                    task = None
                    row = None
                    continue
                with conn:
                    conn.execute('UPDATE collection_tasks SET payload_json=? WHERE task_key=? AND lease_token=?',
                        (json.dumps({'feed_plan':plan}),task['task_key'],task['lease_token']))
                task['payload_json'] = json.dumps({'feed_plan':plan})
                break
            if task:
                payload = json.loads(task['payload_json'])
                kind = task['kind']
                task = {key: task[key] for key in ('task_key', 'lease_token', 'lease_until', 'kind')}
                if kind == 'counter':
                    task['post_id'] = payload['post_id']
                    task['path'] = f"/api/posts/{payload['post_id']}/counters"
                else:
                    task['path'] = payload['feed_plan']['path']
            with conn: _heartbeat(conn,worker,'sampling' if task else 'idle',force=True,supports_feed=supports_feed)
            return {'task': task, 'retry_after_seconds': 3}
        finally:
            conn.close()

    @app.post('/api/worker/permit', tags=['Workers'])
    def permit(request: Request, body: dict = Body(...)):
        body, worker = read_body(request, body)
        conn = connect(db_path)
        try:
            row, post_id, path = _leased(conn, body, worker)
            with conn: _heartbeat(conn,worker,'waiting')
            if not enabled(conn):
                with conn:
                    task_queue.defer(conn,row['task_key'],row['lease_token'],time.time()+10,'paused')
                return {'allowed':False,'released':True,'reason':'paused','retry_after_ms':10000}
            result = coordination.permit(conn, path, 'proxy:' + worker)
            if not result['allowed'] and result.get('reason') in ('recent_sample','recent_observation'):
                task_queue.complete(conn,row['task_key'],row['lease_token'],{'status':'reused_recent_observation'})
                return {**result,'released':True}
            # Cooldowns longer than the remaining lease release work immediately.
            if not result['allowed'] and ((row['kind'] in feed_tasks.KINDS and result['retry_after_ms']>8000)
                    or time.time() + result['retry_after_ms']/1000 + 25 >= row['lease_until']):
                with conn:
                    task_queue.defer(conn, row['task_key'], row['lease_token'], time.time()+result['retry_after_ms']/1000, 'shared_cooldown')
                result['released'] = True
            return result
        finally:
            conn.close()

    @app.post('/api/worker/result', tags=['Workers'])
    def result(request: Request, body: dict = Body(...)):
        body, worker = read_body(request, body)
        task_key, token = body.get('task_key'), body.get('lease_token')
        if not isinstance(task_key, str) or not isinstance(token, str):
            raise HTTPException(400, 'Missing task lease')
        expected_id = hashlib.sha256((task_key + ':' + token).encode()).hexdigest()
        if body.get('observation_id') != expected_id:
            raise HTTPException(400, 'Invalid observation identifier')
        receipt_key = 'worker_result:' + expected_id
        conn = connect(db_path)
        try:
            receipt = conn.execute('SELECT value FROM checkpoints WHERE key=?', (receipt_key,)).fetchone()
            if receipt:
                saved = json.loads(receipt['value'])
                if saved['worker'] != worker:
                    raise HTTPException(409, 'Observation belongs to another worker')
                return {'accepted': True, 'duplicate': True}
            row, post_id, path = _leased(conn, body, worker)
            is_feed = row['kind'] in feed_tasks.KINDS
            plan = json.loads(row['payload_json']).get('feed_plan') if is_feed else None
            bucket = 'feeds' if is_feed else 'counters'
            status = body.get('status')
            if isinstance(status, bool) or status not in (0, 200, 400, 401, 403, 404, 408, 410, 429, 500, 502, 503, 504):
                raise HTTPException(400, 'Invalid remote status')
            if not isinstance(body.get('observed_at'), str):
                raise HTTPException(400, 'Observation time is required')
            at = observed_time(body['observed_at'])
            epoch = datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()
            if epoch > time.time()+30 or epoch < time.time()-3600:
                raise HTTPException(400, 'Observation time outside accepted window')
            retry_after = float(body.get('retry_after') or 60)
            duration = float(body.get('duration_ms') or 0)
            if not math.isfinite(retry_after) or not math.isfinite(duration):
                raise HTTPException(400, 'Invalid timing value')
            retry_after = min(86400, max(1, retry_after))
            duration = min(120000, max(0, duration))
            # Counter failures release their queue slot. The attempt checkpoint
            # below lets the shared planner apply the same backoff as direct work.
            # Only endpoint-wide 429 cooldowns retain a pending counter task.
            next_due = None
            if status == 429:
                next_due = time.time() + retry_after
            elif is_feed and status != 200:
                next_due = time.time() + (900 if status in (401,403,404,410) else 60)
            rates=[]
            for key in ('rate_limit','rate_remaining','rate_reset'):
                value=body.get(key)
                if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0):
                    raise HTTPException(400,'Invalid rate limit header')
                rates.append(value)
            rate_limit,rate_remaining,rate_reset=rates
            delay=rate_limit_delay(rate_limit,rate_remaining,rate_reset)
            if delay is not None:
                coordination.extend_cooldown(conn,bucket,time.time()+min(86400,delay))
            if status == 429:
                coordination.extend_cooldown(conn, bucket, next_due)
            outcome = {'status':status,'observation_id':expected_id}
            def ingest(c):
                if status == 200:
                    if is_feed:
                        try:
                            outcome.update(feed_tasks.apply(c,plan,body.get('payload'),at,raw_source='proxy:'+worker+':'+plan['source']))
                        except feed_tasks.StaleFeedPlan:
                            outcome['status'] = 'stale_plan'
                    else:
                        save_counters(c, post_id, body.get('payload'), at, source='proxy:' + worker)
                elif is_feed:
                    feed_tasks.reset_expired(c,plan,status)
                c.execute('''INSERT INTO api_call_logs(occurred_at,service,method,path,status,duration_ms,retry_count,retry_after,rate_limit,rate_remaining,rate_reset,error,entity_id)
                    VALUES(?,?,'GET',?,?,?,0,?,?,?,?,?,?)''', (at,'proxy:'+worker,path.split('?')[0],status or None,duration,retry_after if status==429 else None,*rates,None if status==200 else 'proxy_http_'+str(status),str(post_id) if post_id else None))
                if not is_feed:
                    c.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                        ('counter_attempt:'+str(post_id),json.dumps({'observed_at':at,'status':'saved' if status==200 else 'failed','http_status':status}),utc_now()))
                    from .collector import record_counter_turn
                    record_counter_turn(c, json.loads(row['payload_json']), at)
                c.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)', (receipt_key,json.dumps({'worker':worker,'status':status}),utc_now()))
                _heartbeat(c,worker,'online',force=True,last_http_status=status,last_observed_at=at,
                    last_post_id=post_id,last_request_kind=row['kind'],
                    **({'last_feed_http_status':status} if is_feed else {}))
            # Compute the next page's due time from the checkpoint just applied,
            # within the same fenced transaction as data, receipt and completion.
            with conn:
                task_queue.begin_completion(conn, task_key)
                completed_at=time.time()
                accepted=False
                if task_queue.owned(conn,task_key,token,now=completed_at):
                    ingest(conn)
                    final_due=next_due
                    if is_feed:
                        final_due=max(next_due or 0,feed_tasks.next_due(conn,row['kind'],outcome,
                            interval=(config or {}).get('interval_seconds',60)))
                    accepted=task_queue.finish(conn,task_key,token,outcome,next_due=final_due,now=completed_at)
            if not accepted:
                receipt=conn.execute('SELECT value FROM checkpoints WHERE key=?',(receipt_key,)).fetchone()
                if receipt and json.loads(receipt['value'])['worker']==worker:
                    return {'accepted':True,'duplicate':True}
                raise HTTPException(409, 'Task lease expired or reassigned')
            return {'accepted': True, 'duplicate': False}
        finally:
            conn.close()
