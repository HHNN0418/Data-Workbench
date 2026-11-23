"""Dashboard access; independent of collector and platform credentials."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

COOKIE_NAME = 'ctn_session'
MEMBERS_PATH = Path(__file__).resolve().parent.parent / 'data' / 'secrets' / 'dashboard_users.json'
_DUMMY_MEMBER = ('', '00' * 16, '00' * 64)


def load_dashboard_members(path):
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if config.get('version') != 1:
        raise ValueError('Unsupported dashboard credential format')
    members = {}
    for email, item in config['members'].items():
        if len(bytes.fromhex(item['salt'])) != 16 or len(bytes.fromhex(item['digest'])) != 64:
            raise ValueError('Invalid dashboard verifier')
        members[email] = (item['name'], item['salt'], item['digest'])
    return members


MEMBERS = load_dashboard_members(MEMBERS_PATH)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(strict=True, min_length=1, max_length=254)
    password: str = Field(strict=True, min_length=1, max_length=1024)


def session_database_path(database, data_dir):
    database = str(database)
    if not database.startswith(('postgresql://', 'postgres://')):
        path = Path(database)
        return path.with_suffix(path.suffix + '.sessions.sqlite3')
    target = urlsplit(database)
    # Isolate databases/users without persisting a connection URL or making
    # database password rotation invalidate otherwise valid dashboard cookies.
    options = tuple((key, value) for key, value in parse_qsl(target.query, keep_blank_values=True)
                    if key in ('host', 'hostaddr', 'port', 'dbname', 'user', 'service'))
    identity = (target.hostname, target.port or 5432, unquote(target.username or ''),
                unquote(target.path), options)
    namespace = hashlib.sha256(repr(identity).encode()).hexdigest()[:32]
    return Path(data_dir) / 'auth' / f'dashboard_sessions_{namespace}.sqlite3'


class AuthStore:
    """Durable dashboard sessions, separate from collection data and credentials."""
    session_seconds = 30 * 24 * 60 * 60
    renewal_interval = 60 * 60
    attempt_window = 5 * 60
    max_failures = 10
    max_account_failures = 30
    max_unknown_client_failures = 40

    def __init__(self, session_path):
        self.session_path = Path(session_path)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.failures = {}
        self.lock = threading.Lock()
        # Persistent expiry must remain meaningful after a process/OS restart.
        self.clock = time.time
        with self._connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS dashboard_sessions (
                token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
                credential_version TEXT NOT NULL, expires_at REAL NOT NULL)''')

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.session_path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def session_key(raw):
        return hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()

    @staticmethod
    def identity(email):
        return {'name': MEMBERS[email][0], 'email': email}

    @staticmethod
    def credential_version(email):
        # Changing/removing an account also invalidates its durable sessions.
        return hashlib.sha256('\0'.join(MEMBERS[email][1:]).encode()).hexdigest()

    def _prune(self):
        now = self.clock()
        self.failures = {key: value for key, value in self.failures.items() if value[1] > now}

    def user(self, request):
        raw = request.cookies.get(COOKIE_NAME, '')
        if not raw or len(raw) > 128:
            return None
        key = self.session_key(raw)
        now = self.clock()
        with self.lock, self._connection() as connection:
            session = connection.execute('''SELECT email, credential_version, expires_at
                FROM dashboard_sessions WHERE token_hash=?''', (key,)).fetchone()
            if not session:
                return None
            email, version, expires_at = session
            if expires_at <= now:
                connection.execute('DELETE FROM dashboard_sessions WHERE token_hash=? AND expires_at<=?', (key, now))
                return None
            if email not in MEMBERS or version != self.credential_version(email):
                connection.execute('DELETE FROM dashboard_sessions WHERE token_hash=?', (key,))
                return None
            # Bound disk writes while active use keeps both server and browser
            # expiry moving. Other processes cannot shorten a newer renewal.
            if expires_at - now <= self.session_seconds - self.renewal_interval:
                updated = connection.execute('''UPDATE dashboard_sessions
                    SET expires_at=MAX(expires_at, ?) WHERE token_hash=?''',
                    (now + self.session_seconds, key))
                if not updated.rowcount:
                    return None
                expires_at = connection.execute('SELECT expires_at FROM dashboard_sessions WHERE token_hash=?', (key,)).fetchone()[0]
            request.state.ctn_session_cookie = (raw, expires_at)
            return self.identity(email)

    def cookie_headers(self, request):
        renewal = getattr(request.state, 'ctn_session_cookie', None)
        if not renewal:
            return []
        raw, _ = renewal
        # An earlier request may finish after logout/relogin. Never let its
        # response overwrite a new cookie with an already revoked token, and
        # use the latest expiry if another request has renewed the session.
        with self.lock, self._connection() as connection:
            session = connection.execute('''SELECT email, credential_version, expires_at
                FROM dashboard_sessions WHERE token_hash=?''', (self.session_key(raw),)).fetchone()
        if not session:
            return []
        email, version, expires_at = session
        if email not in MEMBERS or version != self.credential_version(email):
            return []
        remaining = math.ceil(expires_at - self.clock())
        if remaining <= 0:
            return []
        response = Response()
        set_session_cookie(response, request, raw, remaining)
        return [(key, value) for key, value in response.raw_headers if key == b'set-cookie']

    def login(self, email, password, client, previous=''):
        email = email.strip().lower()
        with self.lock:
            self._prune()
            # A tunnel/NAT shares a client address. Lock only the attempted
            # account, while an account-wide bucket prevents rotating clients.
            pair = ('pair', client, email)
            account = ('account', email)
            unknown_client = ('unknown_client', client)
            limits = ((pair, self.max_failures), (account, self.max_account_failures))
            if email not in MEMBERS:
                limits += ((unknown_client, self.max_unknown_client_failures),)
            if any(self.failures.get(key, (0, 0))[0] >= limit for key, limit in limits):
                raise HTTPException(429, '登录尝试过多，请五分钟后重试')
            # Bound attacker-selected keys without denying registered members.
            if email not in MEMBERS and len(self.failures) >= 512 and pair not in self.failures:
                raise HTTPException(429, '登录尝试过多，请五分钟后重试')
            # Unknown emails perform the same expensive check and get the same error.
            member = MEMBERS.get(email, _DUMMY_MEMBER)
            try:
                digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(member[1]), n=16384, r=8, p=1)
                valid = secrets.compare_digest(digest, bytes.fromhex(member[2])) and email in MEMBERS
            except UnicodeError:
                valid = False
            if not valid:
                for key, _ in limits:
                    count, until = self.failures.get(key, (0, self.clock() + self.attempt_window))
                    self.failures[key] = (count + 1, until)
                raise HTTPException(401, '邮箱或密码不正确')
            self.failures.pop(pair, None)
            self.failures.pop(account, None)
            raw = secrets.token_urlsafe(32)
            now = self.clock()
            with self._connection() as connection:
                connection.execute('DELETE FROM dashboard_sessions WHERE expires_at<=? OR token_hash=?',
                                   (now, self.session_key(previous)))
                # Retain the existing bounded session count across restarts.
                connection.execute('''DELETE FROM dashboard_sessions WHERE token_hash IN (
                    SELECT token_hash FROM dashboard_sessions ORDER BY expires_at DESC LIMIT -1 OFFSET 255)''')
                connection.execute('INSERT INTO dashboard_sessions VALUES (?, ?, ?, ?)',
                                   (self.session_key(raw), email, self.credential_version(email), now + self.session_seconds))
            return raw, self.identity(email)

    def logout(self, request):
        with self.lock, self._connection() as connection:
            connection.execute('DELETE FROM dashboard_sessions WHERE token_hash=?',
                               (self.session_key(request.cookies.get(COOKIE_NAME, '')),))
        request.scope.get('state', {}).pop('ctn_session_cookie', None)


BRIDGE_HEADER = 'x-ctn-bridge'
CLIENT_IP_HEADER = 'x-ctn-client-ip'


def login_client(request, token_path=None):
    """Rate-limit identity for a login attempt.

    Every tunnel request reaches the backend from loopback, so the bridge names
    the Cloudflare client. Only a request carrying the bridge's secret may do so;
    unverifiable forwarding claims share one public bucket rather than posing as
    the local machine.
    """
    supplied, claimed = request.headers.get(BRIDGE_HEADER), request.headers.get(CLIENT_IP_HEADER)
    if supplied is None and claimed is None:
        return request.client.host if request.client else 'local'
    from .secret_files import TUNNEL_BRIDGE_TOKEN, read_secret
    try:
        expected = read_secret(token_path or TUNNEL_BRIDGE_TOKEN).strip()
    except (OSError, ValueError):
        expected = ''
    if len(expected) >= 32 and supplied and hmac.compare_digest(supplied.encode('latin-1'), expected.encode('latin-1')):
        try:
            return 'public:' + str(ipaddress.ip_address((claimed or '').strip()))
        except ValueError:
            return 'public:unknown'
    return 'public:unverified'


def set_session_cookie(response, request, raw, max_age):
    # HTTP loopback is the existing deployment; the HTTPS bridge adds Secure.
    response.set_cookie(COOKIE_NAME, raw, max_age=max_age, path='/',
                        httponly=True, samesite='strict', secure=request.url.scheme == 'https')


def register_auth_routes(app, store, csrf_token):
    @app.get('/api/auth/session', tags=['Authentication'])
    def session(request: Request):
        return {'user': store.user(request), 'csrf_token': csrf_token}

    @app.post('/api/auth/login', tags=['Authentication'])
    def login(request: Request, payload: LoginRequest):
        client = login_client(request)
        raw, user = store.login(payload.email, payload.password, client, request.cookies.get(COOKIE_NAME, ''))
        response = JSONResponse({'user': user, 'csrf_token': csrf_token})
        set_session_cookie(response, request, raw, store.session_seconds)
        return response

    @app.post('/api/auth/logout', tags=['Authentication'])
    def logout(request: Request):
        store.logout(request)
        response = JSONResponse({'user': None, 'csrf_token': csrf_token})
        response.delete_cookie(COOKIE_NAME, path='/', httponly=True, samesite='strict',
                               secure=request.url.scheme == 'https')
        return response
