"""Local-only HTTP application. Remote platform access is GET-only in collector."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import csv
import io
import json
import re
import secrets
import sqlite3
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from .api_models import ActivityImport, CollectRequest, ScoreSnapshot, TeamAccounts, SchedulerRequest

from . import queries
from .db import SCHEMA_VERSION, backup, configure_sqlite, connect, initialize, iter_rows, utc_now, is_postgres

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / 'frontend' / 'dist'
if not (ROOT / 'frontend').is_dir():
    WEB_ROOT = Path(sys.prefix) / 'share' / 'ctn-data' / 'frontend'
DEFAULT_CONFIG = dict(team_label='Agent Zero',timezone='Australia/Sydney',interval_seconds=60,
                      page_limit=100,max_pages=3,latest_incremental_pages=3,trending_max_pages=2,counter_limit=250,collect_trending=True,
                      collect_news=True,timeout=15,max_retries=3,read_only_remote=True,
                      account_limit=20,news_limit=10,site_limit=10,request_budget=360,
                      deadline_seconds=180,max_wait_seconds=120,counter_new_share=0.4,counter_recent_share=0.5,
                      counter_window_share=0.4,relation_limit=30,reply_parent_limit=3,body_refresh_limit=10)


class BlueReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    post_id: int = Field(gt=0, le=2**63 - 1)
    # Set only by the Blue page's confirmation dialog: a person files each report.
    confirmed: bool


class BlueAccountReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: int = Field(gt=0, le=2**63 - 1)
    # Set only by the Blue page's confirmation dialog: a person files each report.
    confirmed: bool


class BlueRecheckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    post_id: int = Field(gt=0, le=2**63 - 1)
    limit: int = Field(default=50, ge=1, le=200)


def load_config(filename=None):
    config = dict(DEFAULT_CONFIG)
    if filename:
        content = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
        if not isinstance(content, dict):
            raise ValueError('配置必须是JSON对象')
        unknown=set(content)-set(DEFAULT_CONFIG)
        if unknown: raise ValueError('未知配置字段：'+', '.join(sorted(unknown)))
        config.update(content)
    config['read_only_remote'] = True
    return config


def spreadsheet_cell(value):
    text = '' if value is None else str(value)
    if text.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'[+-]?\d+(\.\d+)?',text):
        text = "'"+text
    return text


class Application:
    def __init__(self, db_path, config=None, backup_dir=None):
        self.db_path = str(db_path) if is_postgres(db_path) else str(Path(db_path).resolve())
        initialize(self.db_path)
        self.config = dict(DEFAULT_CONFIG, **(config or {}))
        self.config['read_only_remote'] = True
        self.backup_dir = Path(backup_dir or (ROOT / 'backups' if is_postgres(self.db_path) else Path(self.db_path).parent / 'backups')).resolve()
        self.token = secrets.token_urlsafe(32)
        self.collect_lock = threading.Lock()
        self.collecting = False

    def collect(self, overrides):
        from .collector import is_collecting, _config
        config = dict(self.config)
        for key in ('page_limit','max_pages','counter_limit'):
            if key in overrides:
                upper = {'page_limit':100,'max_pages':20,'counter_limit':300}[key]
                config[key] = max(0 if key=='counter_limit' else 1,min(upper,int(overrides[key])))
        for key in ('collect_news','collect_trending'):
            if key in overrides:
                if not isinstance(overrides[key],bool):
                    raise ValueError(key+'必须为布尔值')
                config[key] = overrides[key]
        _config(config)
        with self.collect_lock:
            if self.collecting or is_collecting(self.db_path):
                return False
            self.collecting = True
        def task():
            try:
                from .collector import collect_once
                collect_once(self.db_path,config)
            finally:
                self.collecting = False
        threading.Thread(target=task,name='ctn-collector',daemon=True).start()
        return True


class LocalSecurityMiddleware:
    """Check DNS rebinding, CSRF and bounded bodies before parsing requests."""

    def __init__(self, app, token, db_path=None, auth=None):
        self.app, self.token, self.db_path, self.auth = app, token, db_path, auth

    async def __call__(self, scope, receive, send):
        from starlette.responses import JSONResponse
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        origin = headers.get(b'origin', b'').decode('latin1')
        from .worker_api import worker_authorized
        worker = worker_authorized(Request(scope))
        extension = False
        if origin.startswith('chrome-extension://') and self.db_path:
            def paired_extension():
                conn = connect(self.db_path)
                try:
                    paired = conn.execute("SELECT value FROM meta WHERE key='browser_extension_id'").fetchone()
                    return bool(paired and origin=='chrome-extension://'+paired['value'])
                finally: conn.close()
            extension=await asyncio.to_thread(paired_extension)
        extension_path = scope['path'] in ('/api/bootstrap','/api/browser-capture','/api/extension-status','/api/extension-permit')

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                if (self.auth is not None and message['status'] < 400
                        and scope.get('state', {}).get('ctn_session_cookie')
                        and scope['path'].rstrip('/') not in ('/api/auth/login', '/api/auth/logout')):
                    cookies = await asyncio.to_thread(self.auth.cookie_headers, Request(scope))
                    message['headers'] = list(message.get('headers', [])) + cookies
                # Vite fingerprints these files by content. Keep the shell and
                # API responses fresh while allowing unchanged bundles to be
                # reused across reloads. Missing/unversioned assets stay private.
                fingerprinted_asset = (scope['method'] in ('GET', 'HEAD')
                    and message['status'] == 200
                    and re.fullmatch(r'/assets/[^/]+-[A-Za-z0-9_-]{8}\.(?:js|css|woff2?|png|jpe?g|svg|webp|gif|ico)', scope['path']))
                cache_control = b'public, max-age=31536000, immutable' if fingerprinted_asset else b'no-store'
                message['headers'] = [(key, value) for key, value in message.get('headers', [])
                                      if key.lower() != b'cache-control'] + [
                    (b'cache-control', cache_control), (b'x-content-type-options', b'nosniff'),
                    (b'x-frame-options', b'DENY'), (b'referrer-policy', b'no-referrer'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")]
                if extension and extension_path:
                    message['headers'] += [(b'access-control-allow-origin',origin.encode()),(b'vary',b'Origin'),
                        (b'access-control-allow-methods',b'GET, POST, OPTIONS'),
                        (b'access-control-allow-headers',b'content-type,x-ctn-token')]
            await send(message)

        async def reject(message, status):
            await JSONResponse({'error': message}, status)(scope, receive, secure_send)

        try:
            hostname = urlsplit('//' + host).hostname
            valid_host = hostname in ('127.0.0.1', 'localhost', '::1') or (worker and hostname == 'host.docker.internal')
        except ValueError:
            valid_host = False
        if not valid_host:
            return await reject('仅允许本机访问', 403)
        if origin.startswith('chrome-extension://') and (not extension or not extension_path):
            return await reject('扩展未连接或路由未授权',403)
        if scope['method']=='OPTIONS' and extension and extension_path:
            return await JSONResponse({})(scope,receive,secure_send)
        # Bootstrap/health remain available to the existing local launcher. A
        # CSRF token alone never grants dashboard access. Machine exceptions are
        # restricted to the already verified worker and paired-extension routes.
        # Let the router canonicalize a single trailing slash on public routes.
        # Protected data routes still require authentication before redirects.
        public_path = scope['path'][:-1] if scope['path'].endswith('/') else scope['path']
        public = (scope['method'], public_path) in {
            ('GET', '/api/health'), ('GET', '/api/bootstrap'),
            ('GET', '/api/auth/session'), ('POST', '/api/auth/login'), ('POST', '/api/auth/logout'),
        }
        protected = scope['path'].startswith('/api/') or scope['path'] in ('/docs', '/openapi.json')
        if protected and not public and not worker and not (extension and extension_path):
            if self.auth is None or not await asyncio.to_thread(self.auth.user, Request(scope)):
                return await reject('请先登录工作台', 401)
        if scope['method'] not in ('GET', 'HEAD', 'OPTIONS'):
            origin = headers.get(b'origin', b'').decode('latin1')
            if origin and origin != 'http://' + host and not (extension and extension_path):
                return await reject('拒绝跨站请求', 403)
            if not worker and not secrets.compare_digest(headers.get(b'x-ctn-token', b''), self.token.encode()):
                return await reject('本地操作令牌无效，请刷新页面', 403)
            limit = (5 * 1024 * 1024 if scope['path']=='/api/worker/result' else 65536) if worker else 10 * 1024 * 1024
            try:
                declared_length = int(headers.get(b'content-length', b'0'))
            except ValueError:
                return await reject('请求大小无效', 400)
            if declared_length < 0 or declared_length > limit:
                return await reject('请求大小必须在1字节至10MiB之间', 413)
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > limit:
                    return await reject('请求大小必须在1字节至10MiB之间', 413)
                if not message.get('more_body', False):
                    break
            if not body:
                return await reject('请求大小必须在1字节至10MiB之间', 413)
            if 'application/json' not in headers.get(b'content-type', b'').decode('latin1').lower():
                return await reject('请使用 application/json 请求', 415)
            original_receive = receive
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await original_receive()
            receive = replay
        await self.app(scope, receive, secure_send)


# Browser routes served by the frontend (frontend/src/routes.tsx); tests/test_spa_routes.py keeps both in step.
SPA_PATHS = frozenset({'', 'index.html', 'posts', 'accounts', 'score', 'news', 'analysis', 'operations', 'rules', 'blue',
                       'agents', 'ai-settings', 'reading-selection', 'ai-review', 'analysis/clusters', 'analysis/leaderboard',
                       'ai', 'ai/analysis', 'ai/reading', 'ai/settings', 'ai/agents', 'ai/review', 'ai/facts',
                       'processing', 'processing/bots', 'processing/risk', 'strategy', 'agents/pipeline', 'results/attribution',
                       'results/exposure', 'settings', 'system/maintenance'})
SPA_AGENT_PATH = re.compile(r'(?:ai/)?agents/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')


def create_app(db_path, config=None, backup_dir=None, web_root=None, auto_collect=False):
    """Create the ASGI app; each sync route gets FastAPI's worker thread pool."""
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
    from .analysis_cache import AnalysisCache
    from .imports import add_score_snapshot, import_activity_csv
    from .scoring import calculate_scenario, rules_context, score_overview

    application = Application(db_path, config, backup_dir)
    from .scheduler import CollectionScheduler
    scheduler = CollectionScheduler(application.db_path, application.config, auto_collect)
    from .retention import RetentionWorker, prune_history
    retention = RetentionWorker(application.db_path)
    from .annotations import AnnotationWorker
    annotations = AnnotationWorker(application.db_path)
    from .vote_tracker import VoteTracker

    @asynccontextmanager
    async def lifespan(app):
        scheduler.start()
        app.state.agent_control.start()
        app.state.research_automation.start()
        retention.start()
        annotations.start()
        app.state.vote_tracker.start()
        app.state.stance_labeller.start()
        app.state.hidden_posts.start()
        app.state.risk_assessment.start()
        app.state.style_groups.start()
        try:
            yield
        finally:
            app.state.ai_jobs.stop()
            await asyncio.to_thread(app.state.style_groups.stop)
            await asyncio.to_thread(app.state.risk_assessment.stop)
            await asyncio.to_thread(app.state.hidden_posts.stop)
            await asyncio.to_thread(app.state.stance_labeller.stop)
            await asyncio.to_thread(app.state.vote_tracker.stop)
            await asyncio.to_thread(annotations.stop)
            await asyncio.to_thread(retention.stop)
            await asyncio.to_thread(app.state.research_automation.stop)
            await asyncio.to_thread(app.state.agent_control.stop)
            await asyncio.to_thread(scheduler.stop)

    app = FastAPI(lifespan=lifespan, title='CTN Data API', version='2.0.0', docs_url=None, redoc_url=None,
                  description='CTN 数据与 Agent 工作台。采集器只读；角色 Agent 按成员配置在赛事平台注册、阅读、生成与发布。')
    app.state.application = application
    app.state.retention = retention
    app.state.annotations = annotations
    from .auth import AuthStore, register_auth_routes, session_database_path
    app.state.auth = AuthStore(session_database_path(application.db_path, ROOT / 'data'))
    register_auth_routes(app, app.state.auth, application.token)
    app.add_middleware(LocalSecurityMiddleware, token=application.token, db_path=application.db_path, auth=app.state.auth)
    db = application.db_path
    analysis_cache = AnalysisCache(db)
    from .worker_api import register_worker_routes
    register_worker_routes(app, db, config=application.config, is_enabled=lambda: scheduler.enabled)
    from .ai_routes import register_ai_routes
    ai_secret_dir = ROOT / 'data' / 'secrets' if is_postgres(db) else Path(db).parent / 'secrets'
    register_ai_routes(app, db, ai_secret_dir)
    from .agent_routes import register_agent_routes
    register_agent_routes(app, db, ai_secret_dir)
    root = Path(web_root or WEB_ROOT).resolve()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        errors = [{'field': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors()]
        return JSONResponse({'error': '请求参数无效', 'details': errors}, status_code=400)

    async def invalid_value(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=400)
    app.add_exception_handler(ValueError, invalid_value)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        import logging
        logging.getLogger(__name__).error('Request failed: path=%s exception_type=%s',
                                         request.url.path, type(exc).__name__)
        return JSONResponse({'error': '服务器内部错误，请查看服务日志'}, status_code=500)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({'error': str(exc.detail)}, status_code=exc.status_code)

    def filters(request):
        return dict(request.query_params)

    @app.get('/api/bootstrap', tags=['System'])
    def bootstrap():
        return dict(csrf_token=application.token, timezone=application.config['timezone'],
                    team_label=application.config['team_label'], db_path='PostgreSQL / ctn' if is_postgres(db) else db, readonly_remote=True)

    @app.get('/api/health', tags=['System'])
    def health():
        conn=connect(db)
        try:
            mode=conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone()[0]
            version=conn.execute('SELECT version()').fetchone()[0] if is_postgres(db) else sqlite3.sqlite_version
            last_run=conn.execute('SELECT started_at FROM collection_runs ORDER BY id DESC LIMIT 1').fetchone()
        finally:conn.close()
        from .scheduler import collection_health
        return dict(ok=True, system_performance_policy=2, ai_execution_policy=2, agent_feedback_policy=1, agent_decision_guidance=2,
                    agent_reliability_policy=1, agent_review_policy=2, agent_waiting_policy=1,
                    research_recovery_policy=1, agent_heartbeat_policy=1, ai_connection_setup_policy=1,
                    agent_memory_policy=2,
                    database='postgresql' if is_postgres(db) else 'sqlite', database_version=version,
                    sqlite_version=None if is_postgres(db) else version, schema_version=SCHEMA_VERSION, mode=mode, backend='fastapi',
                    collection=collection_health(scheduler.status(), last_run[0] if last_run else None))

    @app.get('/api/overview', tags=['Data'])
    def overview():
        return queries.overview(db)

    from .nav_status import NavStatus
    nav_status = NavStatus(db, controller=app.state.agent_control, providers=app.state.ai_providers,
                           research=app.state.research_automation, scheduler=scheduler)

    @app.get('/api/nav/status', tags=['System'])
    def navigation_status():
        return nav_status.get()

    from .blue_view import BlueCandidates
    # candidates() costs ~17 s CPU a round: a child process keeps it off the backend (timing in the response).
    blue_candidates = BlueCandidates(db, isolated=True)

    @app.get('/api/blue/candidates', tags=['Score'])
    def blue_report_candidates(limit: int = 50):
        # Read-only suggestions; POST /api/blue/report files one planned report.
        return blue_candidates.get(limit)

    @app.get('/api/blue/reports', tags=['Score'])
    def blue_reports_today():
        from . import blue_submit
        return {'day': blue_submit.blue_reports.sydney_day(), 'reports': blue_submit.today(db),
                'reporter_account_id': blue_submit.REPORTER_ACCOUNT_ID}

    @app.get('/api/blue/reports/history', tags=['Score'])
    def blue_report_history(limit: int = 20, offset: int = 0):
        from . import blue_submit
        return blue_submit.history(db, limit=limit, offset=offset)

    @app.post('/api/blue/recheck', tags=['Score'])
    def blue_report_recheck(payload: BlueRecheckRequest):
        return blue_candidates.recheck(payload.post_id, limit=payload.limit)

    @app.post('/api/blue/report', tags=['Score'])
    def blue_report_submit(payload: BlueReportRequest):
        # The user files each report by hand on the Blue page (D58); only today's planned posts, each once.
        from . import blue_submit
        if payload.confirmed is not True:
            raise ValueError('举报需要在页面上确认后才能提交')
        try:
            return blue_submit.submit(db, app.state.agent_control, blue_candidates.get(), payload.post_id)
        finally:
            blue_candidates.invalidate()

    @app.post('/api/blue/report-account', tags=['Score'])
    def blue_account_report_submit(payload: BlueAccountReportRequest):
        # Same as a post report: the user confirms each account report on the Blue page; only ready accounts.
        from . import blue_submit
        if payload.confirmed is not True:
            raise ValueError('举报需要在页面上确认后才能提交')
        try:
            return blue_submit.submit_account(db, app.state.agent_control, blue_candidates.get(), payload.account_id)
        finally:
            blue_candidates.invalidate()

    from .pipeline import PipelineStatus
    pipeline_status = PipelineStatus(db, nav=nav_status, blue=blue_candidates)

    @app.get('/api/pipeline/status', tags=['System'])
    def pipeline_overview():
        return pipeline_status.get()

    @app.get('/api/diagnostics/cpu', tags=['System'])
    def cpu_diagnostics(seconds: float = 30):
        """Per-thread CPU and busy stacks of this process over a short window (read-only)."""
        from .cpu_profile import profile
        return profile(seconds)

    @app.get('/api/diagnostics/memory', tags=['System'])
    def memory_diagnostics(trace: str = ''):
        """Process memory, the most numerous object types and our caches; trace=start|top|stop drives tracemalloc."""
        from .memory_profile import snapshot
        return snapshot(trace)

    from .clusters import Clusters
    account_clusters = Clusters(db, blue=blue_candidates)

    @app.get('/api/clusters', tags=['Analysis'])
    def behaviour_clusters():
        # Read-only; clusters are inferred operator batches, not confirmed teams.
        return account_clusters.get()

    from .leaderboard import Leaderboard
    leaderboard_view = Leaderboard(db)

    @app.get('/api/leaderboard', tags=['Analysis'])
    def competition_leaderboard(teams: str = '', hours: int = 48):
        # Read-only: every team's score from the extension's leaderboard reads; teams are newline-separated.
        return leaderboard_view.get(teams=teams, hours=hours)

    from .account_identity import AccountIdentity
    account_identity = AccountIdentity(db)

    @app.get('/api/accounts/identity', tags=['Data'])
    def account_identity_view(label: str = '', q: str = '', limit: int = 200):
        # Read-only labels from the Agents' targeting rules: citizen, bot, non-citizen, ours, public figure.
        return account_identity.get(label=label, q=q, limit=limit)

    from . import npc_calibration
    from .view_cache import CachedView
    calibration_view = CachedView(lambda: npc_calibration.snapshot(db), seconds=600, name='npc-calibration')

    @app.get('/api/accounts/calibration', tags=['Data'])
    def account_calibration():
        # The citizen rule scored against every official hourly NPC reply count; read-only.
        return calibration_view.get()

    from . import vote_tracker
    app.state.vote_tracker = VoteTracker(db, app.state.ai_providers)
    # D77: model labels for citizen posts' stance and concerns (targeting A/B).
    from .stance_labels import StanceLabeller
    app.state.stance_labeller = StanceLabeller(db, app.state.ai_providers)
    from .hidden_posts import HiddenPostMonitor
    app.state.hidden_posts = HiddenPostMonitor(db)
    # Hidden posts of every account from the recent feed, confirmed by a second 404 CONFIRM_MINUTES (10) later.
    from . import risk_assessment
    app.state.risk_assessment = risk_assessment.RiskMonitor(db)
    # Style groups (D99/D100): an hourly child-process round, kept in memory for Blue's account cards.
    from .style_groups import StyleGroupMonitor
    app.state.style_groups = StyleGroupMonitor(db)
    risk_view = CachedView(lambda: risk_assessment.overview(db), seconds=60, name='risk-overview')

    @app.get('/api/risk/overview', tags=['Data'])
    def risk_overview():
        # Scan status, confirmed and suspect hidden posts, and each account's standing; read-only.
        return risk_view.get()

    @app.get('/api/polls/forecast', tags=['Data'])
    def poll_forecast():
        # Hourly Jev voting intention of known NPCs, calibrated on Poll 1 (D74); read-only.
        return vote_tracker.snapshot(db)

    # Read-only page views (stage pages ②–⑥ and system); each keeps one result and refreshes it in the background.
    from . import config_view, pipeline_steps, processing_view, results_view
    app.state.reports_dir = ROOT / 'reports' / 'generated'
    page_views = {
        'exposure': CachedView(lambda: results_view.exposure(db), seconds=300, name='exposure'),
        'attribution': CachedView(lambda: results_view.attribution(db), seconds=300, name='attribution'),
        'steps': CachedView(lambda: pipeline_steps.build(db, nav=nav_status), seconds=60, name='pipeline-steps'),
        'processing': CachedView(lambda: processing_view.build(db, worker=annotations), seconds=120, name='processing'),
        'strategy': CachedView(lambda: config_view.strategy(db), seconds=60, name='strategy'),
        'settings': CachedView(lambda: config_view.settings(db, nav=nav_status, providers=app.state.ai_providers),
                               seconds=30, name='settings'),
        'maintenance': CachedView(lambda: config_view.maintenance(
            db, reports_dir=app.state.reports_dir, backup_dir=application.backup_dir, retention=retention),
            seconds=30, name='maintenance')}

    @app.get('/api/results/exposure', tags=['Score'])
    def results_exposure():
        # Official hourly NPC engagement next to what we published in the same official hour.
        return page_views['exposure'].get()

    @app.get('/api/results/attribution', tags=['Score'])
    def results_attribution():
        return page_views['attribution'].get()

    @app.get('/api/pipeline/steps', tags=['System'])
    def pipeline_step_detail():
        return page_views['steps'].get()

    @app.get('/api/processing/annotations', tags=['Data'])
    def processing_annotations():
        return page_views['processing'].get()

    @app.get('/api/strategy/rules', tags=['System'])
    def strategy_rules():
        # The rules in force, read from the code constants the Agents act on; nothing is editable here.
        return page_views['strategy'].get()

    @app.get('/api/settings/summary', tags=['System'])
    def settings_summary():
        return page_views['settings'].get()

    @app.get('/api/maintenance/history', tags=['System'])
    def maintenance_history():
        # Read-only: reload reports and the current hold; this never starts or ends maintenance.
        return page_views['maintenance'].get()

    @app.get('/api/posts', tags=['Data'])
    def posts(request: Request):
        return queries.posts(db, filters(request))

    @app.get('/api/posts/{post_id}', tags=['Data'])
    def post_detail(post_id: int):
        detail = queries.post_detail(db, post_id)
        if detail is None:
            raise HTTPException(404, '帖子未采集')
        return detail

    from .insights import Insights, DEFAULT_HOURS as INSIGHT_HOURS
    insights = Insights(db)

    @app.get('/api/insights', tags=['Analysis'])
    def competition_insights(hours: int = INSIGHT_HOURS):
        return insights.get(hours)

    @app.get('/api/accounts', tags=['Data'])
    def accounts(request: Request):
        # Engagement reuses the cached insights window and never waits for it;
        # only an engagement sort starts a missing or stale build.
        values = filters(request)
        cached = insights.peek(INSIGHT_HOURS, start=values.get('sort') == 'engagement')
        return queries.accounts(db, values, cached['author_points'] if cached else None)

    @app.get('/api/news', tags=['Data'])
    def news(request: Request):
        return queries.news(db, filters(request))

    @app.get('/api/social-context', tags=['Data'])
    def social_context(request: Request):
        return queries.social_context(db, filters(request))

    @app.get('/api/activity', tags=['Score'])
    def activity(request: Request):
        return queries.activity(db, filters(request))

    @app.get('/api/score', tags=['Score'])
    def score():
        from .browser_status import latest_portal, collection_status
        conn=connect(db)
        try:
            values={row['key']:row['value'] for row in conn.execute("SELECT key,value FROM meta WHERE key IN ('browser_extension_id','browser_heartbeat')")}
            portal, ingested_at=latest_portal(conn)
            status=collection_status(values.get('browser_extension_id'),json.loads(values.get('browser_heartbeat','null')),portal,ingested_at)
        finally:conn.close()
        return {**score_overview(db), 'automatic_collection': {**status,'enabled':status['healthy']}}

    @app.get('/api/analysis', tags=['Analysis'])
    def analysis(request: Request):
        return analysis_cache.get(filters(request))

    @app.get('/api/rules', tags=['Score'])
    def rules():
        return rules_context()

    @app.get('/api/proxies', tags=['System'])
    def proxies():
        from .proxy_status import get_proxy_status
        return get_proxy_status()

    @app.get('/api/operations', tags=['System'])
    def operations():
        state=scheduler.status()
        secret_dir=ROOT/'data'/'secrets'
        configured=all((secret_dir/name).is_file() and (secret_dir/name).stat().st_size>0 for name in ('nord_username','nord_password'))
        conn=connect(db)
        try:workers=[json.loads(row['value']) for row in conn.execute("SELECT value FROM meta WHERE key LIKE 'worker_status:%'")]
        finally:conn.close()
        return {**queries.operations(db, application.config, application.collecting or state['running']),
                'scheduler': state, 'database':'postgresql' if is_postgres(db) else 'sqlite',
                'retention': {'retention_days': retention.retention_days,
                              'interval_seconds': retention.interval_seconds,
                              'last_result': retention.last_result},
                'proxy_status':{'configured':configured,'enabled':(ROOT/'data'/'proxy-enabled').exists(),
                                'workers':workers,
                                'state':'enabled' if (ROOT/'data'/'proxy-enabled').exists() else 'ready' if configured else 'not_configured'}}

    @app.get('/api/maintenance/retention', tags=['System'])
    def retention_preview():
        return prune_history(db)

    def csv_rows(table):
        # Starlette may resume this iterator on different worker threads. This
        # dedicated read-only connection is used sequentially by this stream.
        connection = connect(db) if is_postgres(db) else configure_sqlite(sqlite3.connect(db, timeout=15, check_same_thread=False))
        connection.execute('BEGIN READ ONLY' if is_postgres(db) else 'PRAGMA query_only=ON')
        try:
            buffer = io.StringIO(newline='')
            writer = csv.writer(buffer)
            def flush():
                output = buffer.getvalue().encode('utf8')
                buffer.seek(0)
                buffer.truncate(0)
                return output
            with iter_rows(connection, queries.EXPORTS[table]) as cursor:
                yield b'\xef\xbb\xbf'
                writer.writerow([column[0] for column in cursor.description])
                yield flush()
                for row in cursor:
                    writer.writerow([spreadsheet_cell(value) for value in row])
                    # Each yield crosses the ASGI worker-thread boundary. Batch
                    # rows without retaining the full export in Python memory.
                    if buffer.tell() >= 64 * 1024:
                        yield flush()
                if buffer.tell():
                    yield flush()
        finally:
            connection.close()

    @app.get('/api/export', tags=['Data'])
    def export(table: str = 'posts'):
        if table not in queries.EXPORTS:
            raise HTTPException(400, '导出类型无效')
        return StreamingResponse(csv_rows(table), media_type='text/csv; charset=utf-8',
                                 headers={'Content-Disposition': f'attachment; filename="ctn-{table}.csv"'})

    @app.get('/api/backups/{name:path}', tags=['System'])
    def download_backup(name: str):
        if not re.fullmatch(r'ctn-(?:pg-)?\d{8}T\d{12}Z\.sqlite3', name):
            raise HTTPException(400, '备份名无效')
        target = application.backup_dir / name
        if not target.is_file() or target.resolve().parent != application.backup_dir:
            raise HTTPException(404, '备份不存在')
        return FileResponse(target, media_type='application/octet-stream', filename=name)

    @app.post('/api/scheduler', tags=['System'])
    def configure_scheduler(payload: SchedulerRequest):
        return scheduler.configure(payload.enabled, payload.interval_seconds)

    @app.post('/api/collect', tags=['System'], status_code=202)
    def collect(payload: CollectRequest):
        return {'started':True, **scheduler.collect_now(payload.model_dump(exclude_none=True))}

    @app.post('/api/import-activity', tags=['Score'])
    def import_activity(payload: ActivityImport):
        return import_activity_csv(db, payload.csv, Path(payload.filename).name)

    @app.get('/api/extension-status', tags=['System'])
    def extension_status():
        from .browser_status import latest_portal, collection_status
        conn=connect(db)
        try:
            values={r['key']:r['value'] for r in conn.execute("SELECT key,value FROM meta WHERE key IN ('browser_extension_id','browser_team_id','browser_last_sync','browser_heartbeat')")}
            observations={r['kind']:dict(count=r['count'],last_at=r['last_at']) for r in conn.execute("SELECT json_extract(value,'$.kind') AS kind,count(*) AS count,max(updated_at) AS last_at FROM checkpoints WHERE key LIKE 'browser_receipt:%' GROUP BY kind")}
            portal, ingested_at=latest_portal(conn)
            heartbeat=json.loads(values.get('browser_heartbeat','null'))
            status=collection_status(values.get('browser_extension_id'),heartbeat,portal,ingested_at)
            from .coordination import status as coordination_status
            return dict(coordination=coordination_status(conn),extension_id=values.get('browser_extension_id'),team_id=values.get('browser_team_id'),last_sync=json.loads(values.get('browser_last_sync','null')),heartbeat=heartbeat,observations=observations,last_portal=portal,collection_status=status)
        finally: conn.close()

    @app.post('/api/extension-status', tags=['System'])
    def extension_heartbeat(payload: dict = Body(...)):
        from .browser_status import page_diagnostics, diagnostic_text
        conn=connect(db)
        try:
            paired=conn.execute("SELECT value FROM meta WHERE key='browser_extension_id'").fetchone()
            if not paired or payload.get('extension_id')!=paired['value']:
                raise ValueError('扩展绑定不一致')
            version=payload.get('version','')
            if not isinstance(version,str) or not re.fullmatch(r'[0-9.]{1,24}',version): raise ValueError('版本无效')
            queue=payload.get('queue_count',0)
            if type(queue) is not int or not 0<=queue<=100000: raise ValueError('队列长度无效')
            pages=payload.get('pages',[])
            if not isinstance(pages,list) or len(pages)>3: raise ValueError('页面状态无效')
            clean=[];roles=set()
            for page in pages:
                if not isinstance(page,dict) or page.get('role') not in ('trending','latest','portal') or page['role'] in roles: raise ValueError('页面角色无效')
                if type(page.get('tab_id')) is not int or page['tab_id']<0: raise ValueError('标签页 ID 无效')
                pause=page.get('pause_until')
                if pause is not None and (type(pause) is not int or not 0<=pause<10**14): raise ValueError('暂停时间无效')
                clean.append(dict(role=page['role'],tab_id=page['tab_id'],enabled=page.get('enabled') is True,pause_until=pause,bucket=page.get('bucket') if page.get('bucket') in ('feeds','counters') else None,reason=diagnostic_text(page.get('reason'), '暂停原因'),**page_diagnostics(page)));roles.add(page['role'])
            from .coordination import extend_cooldown
            import time
            for page in clean:
                if page['role']!='portal' and page['pause_until'] and '429' in page['reason']:
                    until=min(page['pause_until']/1000,time.time()+604800)
                    for bucket in ([page['bucket']] if page['bucket'] else ['feeds','counters']):
                        extend_cooldown(conn,bucket,until)
            heartbeat=dict(at=utc_now(),version=version,auto_enabled=payload.get('auto_enabled') is True,queue_count=queue,buffer_full=payload.get('buffer_full') is True,pages=clean,error=diagnostic_text(payload.get('error'), '同步错误'))
            with conn:
                conn.execute("INSERT INTO meta(key,value) VALUES('browser_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(heartbeat),))
            return {'ok':True}
        finally: conn.close()

    @app.post('/api/extension-permit', tags=['System'])
    async def extension_permit(payload: dict = Body(...)):
        from .transport import validate_path,TransportError
        from .coordination import bucket_for,permit
        path=payload.get('path','')
        try:validate_path('social',path)
        except TransportError as exc:raise ValueError(str(exc)) from None
        if not bucket_for(path):raise ValueError('该路由无需浏览器采样许可')
        # Short asynchronous wait reduces local IPC churn for a burst of visible
        # cards. Never hold a DB transaction while sleeping.
        end=asyncio.get_running_loop().time()+8
        while True:
            def request_permit():
                conn=connect(db)
                try:return permit(conn,path,'browser')
                finally:conn.close()
            result=await asyncio.to_thread(request_permit)
            if result['allowed'] or result['retry_after_ms']>2000 or asyncio.get_running_loop().time()>=end:return result
            await asyncio.sleep(result['retry_after_ms']/1000)

    @app.post('/api/extension-pair', tags=['System'])
    def extension_pair(payload: dict = Body(...)):
        extension_id=payload.get('extension_id',''); team_id=str(payload.get('team_id',''))
        if not re.fullmatch('[a-p]{32}',extension_id) or not re.fullmatch('[1-9][0-9]*',team_id):
            raise ValueError('请输入有效的 Chrome 扩展 ID 与赛事队伍 ID')
        conn=connect(db)
        try:
            with conn:
                previous=conn.execute("SELECT value FROM meta WHERE key='browser_extension_id'").fetchone()
                previous_team=conn.execute("SELECT value FROM meta WHERE key='browser_team_id'").fetchone()
                if not previous or previous['value']!=extension_id or not previous_team or previous_team['value']!=team_id:
                    conn.execute("DELETE FROM meta WHERE key='browser_heartbeat'")
                for key,value in [('browser_extension_id',extension_id),('browser_team_id',team_id)]:
                    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
        finally: conn.close()
        return extension_status()

    @app.post('/api/browser-capture', tags=['System'])
    def browser_capture(payload: dict = Body(...)):
        from .browser_capture import capture
        return capture(db,payload.get('records'))

    @app.post('/api/score-snapshots', tags=['Score'])
    def score_snapshot(payload: ScoreSnapshot):
        return add_score_snapshot(db, payload.total, payload.observed_at, payload.source, payload.note)

    @app.post('/api/scenario', tags=['Score'])
    def scenario(payload: dict = Body(...)):
        return calculate_scenario(payload)

    @app.post('/api/team-accounts', tags=['Data'])
    def team_accounts(payload: TeamAccounts):
        evidence = payload.evidence.strip()
        label = (payload.team_label if payload.team_label is not None else application.config['team_label']).strip()
        if not evidence or not label:
            raise ValueError('请填写归属证据和队伍名称')
        connection = connect(db)
        try:
            with connection:
                connection.executemany('INSERT INTO team_accounts VALUES(?,?,1,?,?) ON CONFLICT(account_id) DO UPDATE SET team_label=excluded.team_label,is_own=1,evidence=excluded.evidence,updated_at=excluded.updated_at',
                                       [(account_id, label, evidence, utc_now()) for account_id in set(payload.account_ids)])
        finally:
            connection.close()
        return {'updated': len(set(payload.account_ids))}

    @app.post('/api/analysis/run', tags=['Analysis'])
    def run_analysis(payload: dict = Body(...)):
        result = analysis_cache.get(payload)
        connection = connect(db)
        try:
            with connection:
                connection.execute('INSERT INTO analysis_runs(created_at,kind,parameters_json,result_json) VALUES(?,?,?,?)',
                                   (utc_now(), 'descriptive', json.dumps(payload), json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)))
        finally:
            connection.close()
        return result

    @app.post('/api/backup', tags=['System'])
    def create_backup(payload: dict = Body(...)):
        result = backup(db, application.backup_dir)
        return {'filename': result.name, 'url': '/api/backups/' + result.name}

    @app.get('/docs', include_in_schema=False, response_class=HTMLResponse)
    def docs():
        # Offline reference: avoid Swagger UI's external CDN scripts and fonts.
        from html import escape
        rows = ''.join(f'<tr><td>{escape(method.upper())}</td><td><code>{escape(path)}</code></td><td>{escape(operation.get("summary", ""))}</td></tr>'
                       for path, operations in app.openapi()['paths'].items() for method, operation in operations.items())
        return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>CTN API</title><body><h1>CTN Data API</h1><p>本机接口参考 · <a href="/openapi.json">OpenAPI JSON（含请求模型）</a></p><p>POST 请求需携带 /api/bootstrap 返回的 csrf_token，使用 X-CTN-Token 请求头。</p><table><thead><tr><th>Method</th><th>Path</th><th>Summary</th></tr></thead><tbody>' + rows + '</tbody></table></body></html>'

    @app.get('/favicon.ico', include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get('/{path:path}', include_in_schema=False)
    def frontend(path: str):
        if path in SPA_PATHS or SPA_AGENT_PATH.fullmatch(path):
            target = root / 'index.html'
        elif path.startswith('assets/'):
            target = (root / path).resolve()
            if root not in target.parents:
                raise HTTPException(404, '资源不存在')
        else:
            raise HTTPException(404, '接口或页面不存在')
        if not target.is_file():
            raise HTTPException(404, '前端尚未构建，请先运行前端构建命令')
        return FileResponse(target)

    return app


def serve(db_path, config=None, port=8765, auto_collect=False):
    import uvicorn
    app = create_app(db_path, config, auto_collect=auto_collect)
    print(f'CTN Data: http://127.0.0.1:{port}', flush=True)
    print('Database: PostgreSQL / ctn' if is_postgres(db_path) else f'Database: {Path(db_path).resolve()}', flush=True)
    # Query strings can contain search text; disable Uvicorn's default access log.
    uvicorn.run(app, host='127.0.0.1', port=port, access_log=False)


