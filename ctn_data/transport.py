"""Finite, anonymous GET-only transport for explicit official public routes."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import http.client
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .db import begin_write, utc_now

ORIGINS = {'social': 'https://social.legitreal.com', 'news': 'https://sites.legitreal.com'}
_SLUG = r'[A-Za-z0-9][A-Za-z0-9_-]{0,180}'
_SOCIAL = re.compile(r'/api/(?:posts(?:/[1-9]\d*(?:/(?:counters|replies|likes|tags|mentions))?)?'
                      r'|accounts(?:/[1-9]\d*(?:/(?:followers|following|posts|likes|mentions))?)?'
                      r'|tags(?:/popular|/[1-9]\d*/posts)?|feed/lock)\Z')
_NEWS = re.compile(r'/(?:index|' + _SLUG + r'(?:/(?:articles|authors|categories)/' + _SLUG + r')?)\.json\Z')


class TransportError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


class RequestDeferred(TransportError):
    """One route is waiting; other routes can still make progress."""
    pass


class BudgetExceeded(TransportError):
    pass


def validate_path(service, path):
    """Reject arbitrary origins, auth routes, mutations and unrecognised query keys."""
    if service not in ORIGINS or not isinstance(path, str) or len(path) > 4096:
        raise TransportError('unsupported public endpoint')
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith('/'):
        raise TransportError('only relative official public paths are accepted')
    pattern = _SOCIAL if service == 'social' else _NEWS
    if not pattern.fullmatch(parsed.path):
        raise TransportError('endpoint is outside the public GET allowlist')
    try:
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True, max_num_fields=8)
    except ValueError:
        raise TransportError('invalid endpoint query') from None
    if service == 'news' and query:
        raise TransportError('news query parameters are not permitted')
    allowed = {'limit', 'cursor'}
    if parsed.path == '/api/posts' or re.fullmatch(r'/api/tags/[1-9]\d*/posts', parsed.path):
        allowed.add('sort')
    if parsed.path == '/api/accounts':
        allowed.update(('q', 'verified'))
    for key, values in query.items():
        if key not in allowed or len(values) != 1:
            raise TransportError('query is outside the public GET allowlist')
        value = values[0]
        if key == 'limit' and (not value.isdecimal() or not 1 <= int(value) <= 200):
            raise TransportError('page limit must be between 1 and 200')
        if key == 'cursor' and not re.fullmatch(r'[A-Za-z0-9_=-]{1,2048}', value):
            raise TransportError('invalid opaque cursor')
        if key == 'sort' and value not in ('recent', 'trending'):
            raise TransportError('only public feed sorts are permitted')
        if key == 'verified' and value not in ('true', 'false'):
            raise TransportError('invalid verified filter')
        if key == 'q' and not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', value):
            raise TransportError('invalid account prefix')
    return parsed.path


def validate_url(service, url):
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(ORIGINS[service])
    try:
        valid = (parsed.scheme == 'https' and parsed.hostname == expected.hostname
                 and parsed.port in (None, 443) and parsed.username is None and parsed.password is None)
    except ValueError:
        valid = False
    if not valid:
        raise TransportError('redirect outside the original official HTTPS host was blocked')
    return validate_path(service, urllib.parse.urlunsplit(('', '', parsed.path, parsed.query, parsed.fragment)))


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, service):
        self.service = service

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_url(self.service, newurl)
        except TransportError as exc:
            raise TransportError(str(exc), status=code) from None
        # Canonical SDK paths need no redirects. Rejecting even same-host
        # redirects prevents urllib from making hidden, unbudgeted requests.
        raise TransportError('HTTP redirect blocked; canonical public endpoints are required', status=code)


class _ExplicitHTTPProxy(urllib.request.ProxyHandler):
    """Use the selected Gluetun proxy even when the host has NO_PROXY set.

    Official destinations are HTTPS, so HTTPSHandler establishes CONNECT to
    this HTTP proxy. No handler retries a failed tunnel through a direct route.
    """
    def __init__(self, proxy_url):
        parsed = urllib.parse.urlsplit(proxy_url)
        try:
            valid = (parsed.scheme == 'http' and parsed.hostname and parsed.port
                     and parsed.username is None and parsed.password is None
                     and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('proxy_url must be an explicit HTTP proxy host and port without credentials')
        self.proxy_host = parsed.netloc
        super().__init__({'http': proxy_url, 'https': proxy_url})

    def proxy_open(self, req, proxy, type):
        # urllib's default ProxyHandler may silently skip a configured proxy
        # through proxy_bypass(). The selected exit must never be bypassed.
        req.set_proxy(self.proxy_host, 'http')
        return None


def _header_int(headers, key):
    try:
        return max(0, int(headers.get(key)))
    except (ValueError, TypeError):
        return None


def rate_limit_delay(limit, remaining, reset, *, now=None):
    """Wait for one request in the official SDK's steadily refilling bucket.

    capturethenarrative 0.5.1 defines relative Reset as the time to refill the
    entire bucket, so an exhausted bucket regains one token in Reset / Limit.
    Retain the timestamp fallback for absolute Reset values. Retry-After is
    handled separately and must never be shortened by this estimate.
    """
    if remaining != 0 or not limit or reset is None:
        return None
    if reset > 1_000_000_000:
        return max(1, reset - (time.time() if now is None else now))
    return reset / limit


def _retry_delay(headers):
    raw = headers.get('Retry-After')
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(1, int((date - datetime.now(timezone.utc)).total_seconds()) + 1)
        except (TypeError, ValueError, OverflowError):
            return 10


class ReadOnlyTransport:
    def __init__(self, conn, *, timeout=15, max_retries=3, request_budget=160,
                 deadline_seconds=120, max_wait_seconds=30, opener=None,
                 sleeper=None, clock=None, coordinated=True, proxy_url=None, actor='backend'):
        self.conn = conn
        self.timeout = min(60, max(.1, float(timeout)))
        self.max_retries = min(5, max(0, int(max_retries)))
        self.request_budget = min(500, max(1, int(request_budget)))
        self.clock = clock or time.monotonic
        self.coordinated = coordinated
        self.epoch_base = time.time() - self.clock()
        self.sleep = sleeper or time.sleep
        self.deadline = self.clock() + min(300, max(1, float(deadline_seconds)))
        self.max_wait_seconds = min(180, max(0, float(max_wait_seconds)))
        self.waited = 0.0
        self.attempts = 0
        if not isinstance(actor, str) or not re.fullmatch(r'(?:proxy:)?[A-Za-z0-9_-]{1,48}', actor):
            raise ValueError('invalid collection actor')
        self.proxy_url, self.actor = proxy_url, actor
        proxy_handler = _ExplicitHTTPProxy(proxy_url) if proxy_url is not None else urllib.request.ProxyHandler({})
        self.openers = ({service: urllib.request.build_opener(proxy_handler, _OfficialRedirect(service))
                         for service in ORIGINS} if opener is None else {s: opener for s in ORIGINS})
        self.next_allowed = {}
        self.loaded_buckets = set()

    def _load_cooldown(self, service, bucket):
        key = (service, bucket)
        self.loaded_buckets.add(key)
        row = self.conn.execute('SELECT value FROM checkpoints WHERE key=?',
                                ('throttle:' + service + ':' + bucket,)).fetchone()
        if row:
            try:
                remaining = max(0, float(json.loads(row['value'])['not_before_epoch']) - (self.epoch_base+self.clock()))
                self.next_allowed[key] = self.clock() + remaining
            except (TypeError, ValueError, KeyError):
                pass

    def _cooldown(self, service, bucket, seconds):
        key = (service, bucket)
        self.next_allowed[key] = max(self.next_allowed.get(key, 0), self.clock() + seconds)
        delay = max(0, self.next_allowed[key] - self.clock())
        with self.conn:
            from .coordination import cooldown_scope
            begin_write(self.conn, cooldown_scope(service, bucket))
            existing=self.conn.execute('SELECT value FROM checkpoints WHERE key=?',('throttle:' + service + ':' + bucket,)).fetchone()
            previous=float(json.loads(existing['value']).get('not_before_epoch',0)) if existing else 0
            self.conn.execute('''INSERT INTO checkpoints(key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at''',
                ('throttle:' + service + ':' + bucket,
                 json.dumps({'not_before_epoch': max(previous,self.epoch_base+self.clock()+delay)}), utc_now()))

    def _wait(self, seconds):
        if seconds <= 0:
            return
        if self.waited + seconds > self.max_wait_seconds or self.clock() + seconds >= self.deadline:
            raise BudgetExceeded('server wait exceeds the remaining collection budget')
        self.sleep(seconds)
        self.waited += seconds

    def _budget(self):
        if self.attempts >= self.request_budget or self.clock() >= self.deadline:
            raise BudgetExceeded('collection request or time budget exhausted')

    def _log(self, service, path, status, duration, retry, retry_after, headers, error, entity_id):
        log_service = service
        if self.proxy_url is not None:
            log_service = self.actor if self.actor.startswith('proxy:') else 'proxy:' + self.actor
            if service != 'social':
                log_service += ':' + service
        with self.conn:
            self.conn.execute('''INSERT INTO api_call_logs
                (occurred_at,service,method,path,status,duration_ms,retry_count,retry_after,
                 rate_limit,rate_remaining,rate_reset,error,entity_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (utc_now(), log_service, 'GET', path, status, round(duration * 1000, 3), retry,
                 retry_after, _header_int(headers, 'X-RateLimit-Limit'),
                 _header_int(headers, 'X-RateLimit-Remaining'),
                 _header_int(headers, 'X-RateLimit-Reset'), error, str(entity_id) if entity_id is not None else None))

    def get_json(self, service, path, *, entity_id=None):
        clean_path = validate_path(service, path)
        bucket = 'feeds' if clean_path == '/api/posts' else re.sub(r'/[1-9]\d*(?=/|$)', '/:id', clean_path)
        for retry in range(self.max_retries + 1):
            self._load_cooldown(service,bucket)
            if self.coordinated and service=='social':
                from .coordination import bucket_for,permit,cancel_wait
                if bucket_for(clean_path):
                    try:
                        while True:
                            self._budget()
                            slot=permit(self.conn,path,self.actor,self.epoch_base+self.clock())
                            if slot['allowed']:break
                            delay=slot['retry_after_ms']/1000
                            if delay>self.max_wait_seconds-self.waited or self.clock()+delay>=self.deadline:
                                raise RequestDeferred('shared '+slot['reason']+'; checkpoint retained')
                            self._wait(delay)
                    except (BudgetExceeded,RequestDeferred):
                        cancel_wait(self.conn,path,self.actor)
                        raise
            self._wait(max(0, self.next_allowed.get((service, bucket), 0) - self.clock()))
            self._budget()
            self.attempts += 1
            started = self.clock()
            headers, status, retry_after, error, data = {}, None, None, None, None
            retriable = False
            try:
                req = urllib.request.Request(ORIGINS[service] + path,
                    headers={'Accept': 'application/json', 'User-Agent': 'CTN-Data-Readonly/1.0'}, method='GET')
                with self.openers[service].open(req, timeout=min(self.timeout, self.deadline - self.clock())) as response:
                    status = response.status
                    headers = response.headers
                    validate_url(service, response.geturl())
                    body = response.read(8 * 1024 * 1024 + 1)
                    if len(body) > 8 * 1024 * 1024:
                        raise TransportError('public response exceeds the 8 MiB limit', status=status)
                    data = json.loads(body.decode('utf-8-sig'))
            except urllib.error.HTTPError as exc:
                status, headers = exc.code, exc.headers or {}
                error = 'HTTP ' + str(status)
                retriable = status == 429 or 500 <= status <= 599
                retry_after = _retry_delay(headers) if status == 429 or headers.get('Retry-After') else None
                exc.close()
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError,
                    OSError, http.client.HTTPException) as exc:
                error = type(exc).__name__
                retriable = True
            except (json.JSONDecodeError, UnicodeError):
                error = 'invalid JSON response'
            except TransportError as exc:
                error = str(exc)
                if exc.status is not None:
                    status = exc.status
            finally:
                self._log(service, clean_path, status, self.clock() - started, retry,
                          retry_after, headers, error, entity_id)
            limit = _header_int(headers, 'X-RateLimit-Limit')
            remaining = _header_int(headers, 'X-RateLimit-Remaining')
            reset = _header_int(headers, 'X-RateLimit-Reset')
            delay = rate_limit_delay(limit, remaining, reset, now=self.epoch_base+self.clock())
            if delay is not None:
                self._cooldown(service, bucket, delay)
            if retry_after is not None and error is not None:
                self._cooldown(service, bucket, retry_after)
            if error is None:
                return data
            if self.coordinated and status==429:
                raise RequestDeferred('HTTP 429; shared cooldown saved',status=429)
            if not retriable or retry == self.max_retries:
                raise TransportError(error, status=status)
            self._wait(retry_after if retry_after is not None else min(8, 2 ** retry))
        raise AssertionError('unreachable')

    get = get_json
