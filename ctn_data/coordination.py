"""Shared endpoint budgets and per-post request leases across every worker."""
import json
import re
import time
from urllib.parse import urlsplit
from .db import begin_write, utc_now
from . import request_allocation

# Feed reads share the official team allowance of ten per minute. Counter
# collection keeps its separate sampling policy; it does not gate actor writes.
INTERVALS = {'feeds':6.0, 'counters':0.3}
OBJECT_LEASE_SECONDS = 30
RECENT_SAMPLE_SECONDS = 60

def bucket_for(path):
    path=urlsplit(path).path
    if path=='/api/posts': return 'feeds'
    if path.startswith('/api/posts/') and path.endswith('/counters'): return 'counters'
    return None


def cooldown_scope(service, bucket):
    """Throttle writers and admissions must share the same atomic domain."""
    if service == 'social':
        bucket = 'counters' if bucket == '/api/posts/:id/counters' else bucket
    return 'endpoint:' + service + ':' + bucket

def _value(conn,key):
    row=conn.execute('SELECT value FROM checkpoints WHERE key=?',(key,)).fetchone()
    try:return json.loads(row['value']) if row else {}
    except (ValueError,TypeError):return {}

def _put(conn,key,data):
    conn.execute('INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,json.dumps(data),utc_now()))

def cooldowns(conn,now=None):
    now=time.time() if now is None else now
    return {bucket:int(_value(conn,'throttle:social:'+('feeds' if bucket=='feeds' else '/api/posts/:id/counters')).get('not_before_epoch',0)*1000) for bucket in INTERVALS}

def extend_cooldown(conn,bucket,until):
    if bucket not in INTERVALS:return
    key='throttle:social:'+('feeds' if bucket=='feeds' else '/api/posts/:id/counters')
    with conn:
        begin_write(conn, cooldown_scope('social', bucket))
        old=float(_value(conn,key).get('not_before_epoch',0))
        _put(conn,key,{'not_before_epoch':max(old,until)})

def cancel_wait(conn,path,actor):
    """Release readiness when a caller cannot afford another permission poll."""
    if bucket_for(path) != 'counters':
        return
    with conn:
        begin_write(conn, cooldown_scope('social', 'counters'))
        state=_value(conn,'coordination:counters')
        if request_allocation.discard(state,actor,path):
            _put(conn,'coordination:counters',state)

def permit(conn,path,actor,now=None):
    """Only grants immediately available slots; callers must retry after waiting.

    Atomic check-and-grant avoids simultaneous backend/browser admissions. An
    unsuccessful call consumes no slot. Long waits do not reserve future quota.
    """
    now=time.time() if now is None else now
    bucket=bucket_for(path)
    if bucket is None:raise ValueError('Only public feed and counter requests are coordinated')
    with conn:
        begin_write(conn, cooldown_scope('social', bucket))
        key='coordination:'+bucket
        state=_value(conn,key)
        def ineligible(result):
            if bucket == 'counters' and request_allocation.discard(state,actor,path):
                _put(conn,key,state)
            return result
        post_match=re.fullmatch(r'/api/posts/([1-9]\d*)/counters',urlsplit(path).path)
        object_key=None
        if post_match:
            post_id=int(post_match.group(1))
            # A committed observation is reusable irrespective of which
            # network exit produced it. No cached response creates fake time.
            fresh=conn.execute('''SELECT MAX(julianday(observed_at)) AS sampled FROM counter_snapshots
                WHERE post_id=? AND julianday(observed_at)<=?''',
                (post_id,(now+1)/86400+2440587.5)).fetchone()
            sampled=fresh['sampled'] if fresh else None
            until=(sampled-2440587.5)*86400+RECENT_SAMPLE_SECONDS if sampled is not None else 0
            if until>now:
                return ineligible({'allowed':False,'retry_after_ms':max(50,int((until-now)*1000)+1),
                        'bucket':bucket,'reason':'recent_sample'})
            object_key='coordination:post:'+str(post_id)
            lease=_value(conn,object_key)
            if lease.get('actor')!=actor and float(lease.get('until',0))>now:
                return ineligible({'allowed':False,'retry_after_ms':max(50,int((lease['until']-now)*1000)+1),
                        'bucket':bucket,'reason':'post_in_flight'})
        blocked=cooldowns(conn,now)[bucket]/1000
        wait=max(float(state.get('next_at',0)),blocked)-now
        if bucket == 'counters' and blocked>now:
            request_allocation.reset(state)
            _put(conn,key,state)
        elif bucket == 'counters':
            deferred=request_allocation.request_turn(state,actor,path,now,float(state.get('next_at',0)))
            if deferred:
                _put(conn,key,state)
                return deferred
        if wait>0:return {'allowed':False,'retry_after_ms':max(50,int(wait*1000)+1),'bucket':bucket,'reason':'cooldown' if blocked>now else 'pacing'}
        state.update(next_at=now+INTERVALS[bucket],last_actor=actor,last_at=now)
        state[actor+'_grants']=int(state.get(actor+'_grants',0))+1
        _put(conn,key,state)
        if object_key:
            _put(conn,object_key,{'actor':actor,'until':now+OBJECT_LEASE_SECONDS})
        return {'allowed':True,'retry_after_ms':0,'bucket':bucket}

def status(conn):
    return {'feed_interval_seconds':INTERVALS['feeds'],'counter_interval_seconds':INTERVALS['counters'],
            'browser_counter_cache_seconds':30,'backend_recent_sample_seconds':60,
            'post_request_lease_seconds':OBJECT_LEASE_SECONDS,
            'cooldown_until':cooldowns(conn),
            'buckets':{name:_value(conn,'coordination:'+name) for name in INTERVALS}}
