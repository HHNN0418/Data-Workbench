"""One-page Feed plans shared by direct and leased proxy collectors.

Preparing is read-only. Applying does no network I/O and never commits: callers
hold their result/lease transaction so page data and cursor advance commit once.
"""
from datetime import datetime, timezone
import hashlib
import json
import time
from urllib.parse import urlencode, quote

from .db import raw_record
from .ingest import save_posts, observed_time
from .transport import validate_path
from .coordination import status as coordination_status

KINDS = ('latest','latest_head','history','trending','trending_head')


class StaleFeedPlan(ValueError):
    """Another committed page changed this plan's checkpoint."""


def _state(conn, key):
    row=conn.execute('SELECT value FROM checkpoints WHERE key=?',(key,)).fetchone()
    if row is None:
        return {}
    try:
        value=json.loads(row['value'])
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):
        return {}


def _write(conn, key, value, at):
    conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
        (key,json.dumps(value,ensure_ascii=False,sort_keys=True),at))


def _lock_checkpoint(conn, plan):
    """Serialize pages sharing a cursor without blocking unrelated workers."""
    if getattr(conn, 'dialect', None) == 'postgresql':
        if not conn.in_transaction:
            conn.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
        lock_id = int.from_bytes(hashlib.sha256(
            ('ctn:feed:' + plan['checkpoint_key']).encode()).digest()[:8], signed=True)
        conn.execute('SELECT pg_advisory_xact_lock(?)', (lock_id,))


def _position(post):
    try:
        at=datetime.fromisoformat(post['created_at'].replace('Z','+00:00'))
        if at.tzinfo is None:
            return None
        return (at.astimezone(timezone.utc).isoformat(timespec='microseconds'),int(post['post_id']))
    except (KeyError,ValueError,TypeError,AttributeError):
        return None


def prepare(conn, kind, config=None):
    if kind not in KINDS:
        raise ValueError('unknown Feed task')
    limit=(config or {}).get('page_limit',50)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=200:
        raise ValueError('page_limit must be between 1 and 200')
    source='trending' if kind.startswith('trending') else 'latest'
    key='feed:trending' if source=='trending' else 'feed:latest' if kind=='history' else 'feed:latest:live'
    expected=_state(conn,key)
    state=dict(expected)
    if kind=='history' and state.get('reached_end') and not state.get('cursor'):
        return None
    if kind=='latest' and not state:
        history=_state(conn,'feed:latest')
        state={'watermark':history.get('cycle_head') or history.get('watermark')}
    head=kind.endswith('_head')
    if head and not state.get('cursor'):
        return None
    query={'sort':'trending' if source=='trending' else 'recent','limit':limit}
    if state.get('cursor') and not head:
        query['cursor']=state['cursor']
    path='/api/posts?'+urlencode(query)
    validate_path('social',path)
    return {'kind':kind,'path':path,'source':source,'checkpoint_key':key,
            'head_only':head,'expected_state':expected,'effective_state':state}


def apply(conn, plan, payload, observed_at, raw_source=None):
    if plan.get('kind') not in KINDS:
        raise ValueError('unknown Feed plan')
    _lock_checkpoint(conn, plan)
    if _state(conn,plan['checkpoint_key'])!=plan['expected_state']:
        raise StaleFeedPlan('Feed checkpoint changed; discard stale page plan')
    if not isinstance(payload,dict) or not isinstance(payload.get('items'),list):
        raise ValueError('feed must contain an items list')
    posts=payload['items']
    if len(posts)>200 or any(not isinstance(post,dict) for post in posts):
        raise ValueError('feed items must contain at most 200 post objects')
    cursor=payload.get('next_cursor')
    if cursor is not None and not isinstance(cursor,str):
        raise ValueError('feed cursor must be text or null')
    if cursor:
        validate_path('social','/api/posts?sort=recent&cursor='+quote(cursor,safe=''))
    state=plan['effective_state']
    if cursor and not plan['head_only'] and cursor==state.get('cursor'):
        raise ValueError('server repeated a pagination cursor')
    at=observed_time(observed_at)
    positions=[position for post in posts if (position:=_position(post)) is not None]
    boundary=tuple(state['watermark']) if state.get('watermark') else None
    cycle_head=tuple(state['cycle_head']) if state.get('cycle_head') else None
    if cycle_head is None and positions and not plan['head_only']:
        cycle_head=max(positions)
    reached=plan['source']=='latest' and boundary is not None and any(position<=boundary for position in positions)
    complete=not posts or not cursor or reached
    raw_record(conn,raw_source or 'social:'+plan['source'],'/api/posts',payload,at)
    save_posts(conn,posts,plan['source'],at)
    if not plan['head_only']:
        if complete:
            head=max([value for value in (boundary,cycle_head) if value is not None],default=None)
            updated={'cursor':None,'cycle_head':None,'watermark':head}
        else:
            updated={'cursor':cursor,'cycle_head':cycle_head,'watermark':boundary}
        old_dates=[state['oldest_created_at']] if state.get('oldest_created_at') else []
        updated.update(pages_completed=state.get('pages_completed',0)+1,
            items_seen=state.get('items_seen',0)+len(posts),
            oldest_created_at=min([p[0] for p in positions]+old_dates,default=None),
            reached_end=state.get('reached_end',False) or not posts or not cursor)
        _write(conn,plan['checkpoint_key'],updated,at)
    return {'status':'completed','stage':plan['kind'],'posts':len({p['post_id'] for p in posts}),
            'post_ids':[p['post_id'] for p in posts],
            'feeds':{plan['source']:{'pages':1,'cycle_complete':complete and not plan['head_only'],
                'head_refreshed':plan['head_only'],'resumed':bool(state.get('cursor'))}}}


def reset_expired(conn, plan, status):
    """Invalidate only the cursor which actually failed, never a newer one."""
    state=plan['effective_state']
    if status not in (400,404,410) or plan['head_only'] or not state.get('cursor'):
        return False
    _lock_checkpoint(conn, plan)
    if _state(conn,plan['checkpoint_key'])!=plan['expected_state']:
        return False
    _write(conn,plan['checkpoint_key'],{**state,'cursor':None,'cycle_head':None},
           datetime.now(timezone.utc).isoformat().replace('+00:00','Z'))
    return True


def next_due(conn, kind, result, interval=60, now=None):
    if kind not in KINDS:
        raise ValueError('unknown Feed task')
    now=time.time() if now is None else now
    period={'latest':max(30,min(60,interval)),'latest_head':45,'history':30,
            'trending':180,'trending_head':180}[kind]
    if kind=='latest' and _state(conn,'feed:latest:live').get('cursor'):
        period=8
    shared=coordination_status(conn)
    earliest=max(shared['cooldown_until']['feeds']/1000,
                 shared['buckets']['feeds'].get('next_at',0))
    if result.get('status') in ('deferred','stale_plan'):
        return max(now+1,earliest)
    return max(now+period,earliest)
