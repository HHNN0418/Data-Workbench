"""Leased feed/counter sampler. Remote traffic has no direct fallback."""
import hashlib
import errno
import http.client
import json
import math
import os
from pathlib import Path
import random
import re
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, ProxyHandler, HTTPHandler, HTTPRedirectHandler, build_opener

REMOTE_ORIGIN = 'https://social.legitreal.com'
COUNTER_LIMIT = 64 * 1024
FEED_LIMIT = 4 * 1024 * 1024
RESULT_LIMIT = 5 * 1024 * 1024
FEED_KINDS = frozenset(('latest', 'latest_head', 'history', 'trending', 'trending_head'))
# Backend validation failures. 401/408/429/5xx and network errors stay retryable.
REJECTED_RESULT_STATUSES = frozenset((400, 404, 413, 422))
BACKEND_CONNECT_TIMEOUT = 3.0
BACKEND_CONNECT_ATTEMPTS = 3
_ROUTING_ERRORS = frozenset((errno.ENETUNREACH, errno.EHOSTUNREACH))
_CONNECT_RETRY_ERRORS = _ROUTING_ERRORS | frozenset((
    errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.ECONNABORTED, errno.ENETDOWN,
))


def _connect_backend(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Retry only TCP establishment, before any HTTP bytes can be sent."""
    read_timeout = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    addresses = socket.getaddrinfo(*address, 0, socket.SOCK_STREAM)
    if not addresses:
        raise OSError('Backend resolution returned no addresses')
    deadline = time.monotonic() + BACKEND_CONNECT_TIMEOUT * BACKEND_CONNECT_ATTEMPTS
    failure = None
    for attempt in range(BACKEND_CONNECT_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        family, kind, protocol, _, endpoint = addresses[attempt % len(addresses)]
        connected = None
        try:
            connected = socket.socket(family, kind, protocol)
            connect_timeout = min(BACKEND_CONNECT_TIMEOUT, remaining)
            if read_timeout is not None:
                connect_timeout = min(connect_timeout, read_timeout)
            connected.settimeout(connect_timeout)
            if source_address:
                connected.bind(source_address)
            connected.connect(endpoint)
            # The short connect budget must not shorten response reads.
            connected.settimeout(read_timeout)
            return connected
        except OSError as exc:
            if connected is not None:
                connected.close()
            if not isinstance(exc, TimeoutError) and exc.errno not in _CONNECT_RETRY_ERRORS:
                raise
            # A final unroutable IPv6 address must not hide an earlier IPv4
            # timeout. Keep the first useful error, retaining normal DNS order.
            if failure is None or (failure.errno in _ROUTING_ERRORS and exc.errno not in _ROUTING_ERRORS):
                failure = exc
    if failure is not None:
        raise failure
    raise TimeoutError('Backend connection deadline exceeded')


class BackendHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_backend


class BackendHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(BackendHTTPConnection, request)


def validate_task(task):
    """The backend supplies a path, never a URL or arbitrary fetch command."""
    kind=task.get('kind','counter')
    path=task.get('path')
    if not isinstance(path,str) or len(path)>8192 or re.search(r'[\x00-\x20\x7f#]',path):
        raise ValueError('Invalid task path')
    if kind=='counter':
        post_id=task.get('post_id')
        if isinstance(post_id,bool) or not isinstance(post_id,int) or post_id<1:
            raise ValueError('Invalid counter identifier')
        if not re.fullmatch(r'/api/posts/[1-9][0-9]*/counters',path) or path!=f'/api/posts/{post_id}/counters':
            raise ValueError('Invalid counter URL')
        return kind,COUNTER_LIMIT
    if kind not in FEED_KINDS:
        raise ValueError('Unsupported task kind')
    parts=urlsplit(path)
    if parts.scheme or parts.netloc or parts.path!='/api/posts' or parts.fragment:
        raise ValueError('Invalid feed URL')
    if re.search(r'%(?![0-9A-Fa-f]{2})',parts.query):
        raise ValueError('Invalid query encoding')
    pairs=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True,encoding='utf-8',errors='strict',max_num_fields=3)
    params=dict(pairs)
    if len(params)!=len(pairs) or set(params) not in ({'sort','limit'},{'sort','limit','cursor'}):
        raise ValueError('Invalid feed query parameters')
    expected='trending' if kind.startswith('trending') else 'recent'
    if params['sort']!=expected or not re.fullmatch(r'(?:[1-9][0-9]?|100)',params['limit']):
        raise ValueError('Invalid feed order or page size')
    if 'cursor' in params and (not params['cursor'] or len(params['cursor'])>4096 or re.search(r'[\x00-\x1f\x7f]',params['cursor'])):
        raise ValueError('Invalid feed cursor')
    return kind,FEED_LIMIT


def validate_payload(task,payload):
    if not isinstance(payload,dict):
        raise ValueError('Invalid response object')
    if task.get('kind','counter') in FEED_KINDS:
        items=payload.get('items')
        if not isinstance(items,list) or len(items)>100 or any(not isinstance(item,dict) for item in items):
            raise ValueError('Invalid feed items')
        cursor=payload.get('next_cursor')
        if cursor is not None and not isinstance(cursor,str):
            raise ValueError('Invalid next cursor')
        return
    if payload.get('post_id',task['post_id'])!=task['post_id']:
        raise ValueError('Invalid counter payload')
    for key in ('like_count','reply_count','repost_count','view_count'):
        value=payload.get(key)
        if value is not None and (isinstance(value,bool) or not isinstance(value,int) or value<0):
            raise ValueError('Invalid counter value')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Remote redirects are not allowed')


def retry_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp()-now
        except (TypeError, ValueError, OverflowError):
            seconds = 60
    return min(86400, max(1, seconds))


def rate_headers(headers):
    result={}
    for key in ('Limit','Remaining','Reset'):
        try:
            value=float(headers.get('X-RateLimit-'+key))
            if math.isfinite(value) and value>=0:
                result['rate_'+key.lower()]=value
        except (TypeError,ValueError):
            pass
    return result


def retry_diagnostics(exc):
    """Only emit types and numeric codes; exception strings may contain secrets."""
    reason = exc.reason if isinstance(exc, URLError) and not isinstance(exc, HTTPError) else exc
    fields = ['reason='+type(reason).__name__]
    for key in ('errno', 'winerror'):
        value = getattr(reason, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            fields.append(key+'='+str(value))
    if isinstance(exc, HTTPError) and isinstance(exc.code, int) and not isinstance(exc.code, bool):
        fields.append('http_status='+str(exc.code))
    return ' '.join(fields)


class ProxyWorker:
    def __init__(self, backend, proxy, worker, token_file, state_dir):
        if not proxy.startswith('http://'):
            raise ValueError('An explicit HTTP VPN proxy is required')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', worker):
            raise ValueError('Invalid worker identifier')
        self.backend = backend.rstrip('/')
        self.worker = worker
        self.token_file = Path(token_file)
        self.state = Path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.pending = self.state / 'pending.json'
        self.local = build_opener(ProxyHandler({}), NoRedirect(), BackendHTTPHandler())
        self.remote = build_opener(ProxyHandler({'http':proxy,'https':proxy}), NoRedirect())
        self._backend_error = None
        self._backend_retry_started = None

    def api(self, action, body):
        phase = action if action in ('claim', 'permit', 'result') else 'unknown'
        started = time.monotonic()
        self._backend_error = None
        try:
            token = self.token_file.read_text(encoding='utf-8').strip()
            if len(token) < 32:
                raise ValueError('Worker token is missing')
            encoded=json.dumps({'worker':self.worker,**body},ensure_ascii=False,separators=(',',':')).encode('utf-8')
            if len(encoded)>RESULT_LIMIT:
                raise ValueError('Worker request exceeds 5 MiB')
            request = Request(self.backend+'/api/worker/'+action,
                data=encoded,
                headers={'Content-Type':'application/json','Authorization':'Bearer '+token},method='POST')
            with self.local.open(request,timeout=15) as response:
                result = json.load(response)
        except Exception as exc:
            self._backend_error = (exc, phase, (time.monotonic()-started)*1000)
            raise
        if self._backend_retry_started is not None:
            elapsed = (time.monotonic()-self._backend_retry_started)*1000
            print(f'Worker backend recovered: action={phase} retry_elapsed_ms={elapsed:.1f}',flush=True)
            self._backend_retry_started = None
        return result

    def beat(self):
        (self.state/'heartbeat').write_text(str(time.time()),encoding='ascii')

    def wait(self, seconds):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            self.beat()
            time.sleep(max(0,min(5,end-time.monotonic())))

    def sample(self, task):
        _,limit=validate_task(task)
        path = task['path']
        started=time.monotonic()
        result={key:task[key] for key in ('task_key','lease_token')}
        result['observation_id']=hashlib.sha256((task['task_key']+':'+task['lease_token']).encode()).hexdigest()
        try:
            request=Request(REMOTE_ORIGIN+path,headers={'Accept':'application/json','User-Agent':'CTN-ReadOnly-Collector/2.0'})
            with self.remote.open(request,timeout=20) as response:
                result.update(rate_headers(response.headers))
                raw=response.read(limit+1)
                if len(raw)>limit:
                    raise ValueError('Remote response too large')
                payload=json.loads(raw)
                validate_payload(task,payload)
                result.update(status=200,payload=payload)
        except HTTPError as exc:
            result.update(rate_headers(exc.headers))
            result['status']=exc.code if exc.code in (400,401,403,404,408,410,429,500,502,503,504) else 0
            result['retry_after']=retry_seconds(exc.headers.get('Retry-After'))
            exc.close()
        except (OSError,ValueError):
            result['status']=0
        result['observed_at']=datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00','Z')
        result['duration_ms']=round((time.monotonic()-started)*1000,3)
        return result

    def flush(self):
        if not self.pending.exists():
            return
        result=json.loads(self.pending.read_text(encoding='utf-8'))
        try:
            response=self.api('result',result)
            if not response.get('accepted'):
                raise ValueError('Backend did not acknowledge result')
        except HTTPError as exc:
            if exc.code == 409:
                # An expired lease cannot write over its replacement's observation.
                print('Result lease expired; scheduler will reassign',flush=True)
            elif exc.code in REJECTED_RESULT_STATUSES:
                # A definitive rejection never succeeds on replay. Keep it for
                # diagnosis instead of retrying it forever and claiming nothing.
                rejected=self.state/'rejected'
                rejected.mkdir(exist_ok=True)
                self.pending.replace(rejected/f'{time.time_ns()}-{exc.code}.json')
                print(f'Result rejected: http_status={exc.code}; moved to rejected/',flush=True)
                return
            else:
                raise
        self.pending.unlink()

    def once(self):
        self.beat()
        self.flush()
        task=self.api('claim',{'capabilities':['counter','feed']}).get('task')
        if not task:
            self.wait(3)
            return
        lease={key:task[key] for key in ('task_key','lease_token')}
        while time.time()+25<task['lease_until']:
            allowed=self.api('permit',lease)
            if allowed.get('released'):
                return
            if allowed.get('allowed'):
                result=self.sample(task)
                temp=self.pending.with_suffix('.tmp')
                temp.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
                temp.replace(self.pending)
                self.flush()
                return
            self.wait(min(10,max(.05,allowed.get('retry_after_ms',1000)/1000)))

    def run(self):
        failures=0
        while True:
            try:
                self.once()
                failures=0
            except Exception as exc:
                failures+=1
                # Never log URLs, headers, credentials or remote response bodies.
                details = retry_diagnostics(exc)
                if self._backend_error is not None and self._backend_error[0] is exc:
                    _, phase, elapsed = self._backend_error
                    details = f'action={phase} elapsed_ms={elapsed:.1f} '+details
                    if self._backend_retry_started is None:
                        self._backend_retry_started = time.monotonic()
                print('Worker retry: '+type(exc).__name__+' '+details,flush=True)
                self.wait(min(60,2**min(failures,6))+random.random())


if __name__=='__main__':
    ProxyWorker(os.environ.get('CTN_BACKEND','http://host.docker.internal:8765'),
        os.environ['CTN_PROXY'],os.environ['CTN_WORKER_ID'],
        os.environ.get('CTN_WORKER_TOKEN_FILE','/run/secrets/worker_token'),
        os.environ.get('CTN_STATE_DIR','/state')).run()
