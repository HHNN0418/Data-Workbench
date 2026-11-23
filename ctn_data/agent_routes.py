"""Authenticated dashboard endpoints for independent competition actors."""
import gzip
import json
import threading
import time
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from typing import Annotated, Literal

from .agent_control import AgentController, AgentError


SNAPSHOT_SECONDS = 5.0


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SettingsRequest(StrictModel):
    enabled: StrictBool | None = None
    block_agent_interactions: StrictBool | None = None
    team_side: str | None = Field(default=None, max_length=80)
    max_accounts: StrictInt | None = Field(default=None, ge=1, le=50)
    global_interval_seconds: StrictInt | None = Field(default=None, ge=25, le=86400)
    timezone: str | None = Field(default=None, max_length=80)
    team_code: str | None = Field(default=None, max_length=500)
    # D84: where post stance/concerns come from; 'keyword' switches back to the rules.
    stance_source: Literal['model', 'keyword'] | None = None


class RocketModeRequest(StrictModel):
    on: StrictBool


class QuotaRequest(StrictModel):
    bucket: Literal['post', 'like', 'unlike', 'follow', 'unfollow', 'delete_post',
                    'feed', 'read', 'auth', 'notification', 'resolve']
    cap: Annotated[StrictInt, Field(gt=0)] | None


class ReviewSettingsRequest(StrictModel):
    enabled: StrictBool
    provider_id: StrictStr = Field(max_length=64)
    platform_policy: Literal['hold', 'warn']
    expected_revision: StrictInt = Field(ge=1)


class CompetitionRequest(StrictModel):
    expected_revision: StrictInt = Field(ge=0)
    mode: Literal['off', 'observe', 'resources', 'models'] | None = None
    enhanced_provider_id: StrictStr | None = Field(default=None, max_length=64)
    premium_provider_id: StrictStr | None = Field(default=None, max_length=64)
    enhanced_hourly_calls: StrictInt | None = Field(default=None, ge=0, le=1000)
    # Legacy clients may still send this field; it is ignored.
    enhanced_max_output_tokens: StrictInt | None = None


class SaveRequest(StrictModel):
    id: str | None = Field(default=None, max_length=64)
    version: StrictInt | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=80)
    username: str | None = Field(default=None, max_length=50)
    background: str | None = Field(default=None, max_length=3000)
    stance: str | None = Field(default=None, max_length=2000)
    voice: str | None = Field(default=None, max_length=1000)
    interests: list[str] | str | None = None
    language: str | None = Field(default=None, max_length=80)
    provider_id: str | None = Field(default=None, max_length=64)
    enabled: StrictBool | None = None
    mode: Literal['review', 'auto'] | None = None
    daily_limit: StrictInt | None = Field(default=None, ge=1, le=50)
    interval_seconds: StrictInt | None = Field(default=None, ge=25, le=86400)
    allowed_actions: list[Literal['post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'skip']] | None = Field(default=None, min_length=1, max_length=8)
    freshness_minutes: StrictInt | None = Field(default=None, ge=5, le=1440)
    news_freshness_hours: StrictInt | None = Field(default=None, ge=1, le=168)
    notification_interval_seconds: StrictInt | None = Field(default=None, ge=30, le=3600)
    # Model-based reading was removed from the product (D67): every role reads by rules.
    reading_selection_mode: Literal['rules'] | None = None


class AgentID(StrictModel):
    agent_id: str = Field(min_length=1, max_length=64)


class NotificationReconsiderRequest(AgentID):
    notification_id: StrictInt = Field(gt=0, le=2**63 - 1)


class PersonaFields(StrictModel):
    name: StrictStr | None = Field(default=None, max_length=80)
    username: StrictStr | None = Field(default=None, max_length=50)
    background: StrictStr | None = Field(default=None, max_length=3000)
    stance: StrictStr | None = Field(default=None, max_length=2000)
    voice: StrictStr | None = Field(default=None, max_length=1000)
    interests: (Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=100)]], Field(max_length=20)]
                | Annotated[StrictStr, Field(max_length=2020)] | None) = None
    language: StrictStr | None = Field(default=None, max_length=80)


class GeneratePersonaRequest(StrictModel):
    brief: StrictStr = Field(default='', max_length=2000)
    provider_id: StrictStr | None = Field(default=None, max_length=64)
    existing_agent_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    persona: PersonaFields | None = None


class RegisterRequest(AgentID):
    password: str | None = Field(default=None, max_length=200)


class BindRequest(AgentID):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class DraftID(StrictModel):
    draft_id: str = Field(min_length=1, max_length=64)


class PublishRequest(DraftID):
    body: str | None = Field(default=None, max_length=500)
    confirmation_post_id: StrictInt | None = Field(default=None, gt=0)


class ManualPrepareRequest(AgentID):
    action: Literal['post', 'reply', 'repost', 'like', 'unlike', 'follow', 'unfollow', 'delete_post']
    body: StrictStr = Field(default='', max_length=500)
    target_post_id: StrictInt | None = Field(default=None, gt=0)
    target_account_id: StrictInt | None = Field(default=None, gt=0)
    idempotency_key: StrictStr = Field(min_length=36, max_length=36)
    acknowledge_warnings: StrictBool | None = None
    fact_ids: Annotated[list[Annotated[StrictStr, Field(min_length=1, max_length=80)]],
                        Field(max_length=6)] | None = None
    concern: StrictStr | None = Field(default=None, max_length=40)


class ManualQueueRequest(DraftID):
    body: StrictStr | None = Field(default=None, max_length=500)


class MemoryRequest(AgentID):
    memory: str = Field(max_length=4000)


class ReconcileRequest(DraftID):
    post_id: StrictInt | None = Field(default=None, gt=0)

class DiscardUnexecutedRequest(DraftID):
    confirmed_not_executed: StrictBool
    reason: StrictStr = Field(min_length=3, max_length=1000)
    expected_version: StrictInt = Field(ge=1, le=2**31 - 1)


def register_agent_routes(app, db_path, secret_dir: Path):
    controller = AgentController(db_path, secret_dir, provider_store=getattr(app.state, 'ai_providers', None))
    app.state.agent_control = controller

    async def agent_error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status_code)
    app.add_exception_handler(AgentError, agent_error)

    def require_member(request: Request):
        member = app.state.auth.user(request)
        if not member:
            raise HTTPException(401, '请先登录工作台')
        return member

    # The team snapshot takes seconds with 15 roles and ~500 drafts, and the
    # Agents page polls it from each open tab. Concurrent polls piled up and
    # starved saves (22 Oct), so one computation at a time serves every poll
    # for SNAPSHOT_SECONDS; any write drops it. The cache keeps the encoded
    # body: re-encoding megabytes per poll cost about as much as the snapshot.
    snapshot = {'at': 0.0, 'entry': None, 'generation': 0}
    compute_lock = threading.Lock()  # single flight
    cache_lock = threading.Lock()  # guards `snapshot`; never held while computing

    def team_snapshot():
        with compute_lock:
            with cache_lock:
                if snapshot['entry'] is not None and time.monotonic() - snapshot['at'] < SNAPSHOT_SECONDS:
                    return snapshot['entry']
                generation = snapshot['generation']
            body = json.dumps(jsonable_encoder(controller.snapshot(compact=True)), ensure_ascii=False,
                              allow_nan=False, separators=(',', ':')).encode('utf-8')
            entry = {'body': body, 'gzip': gzip.compress(body, 5)}
            with cache_lock:
                # A write that finished meanwhile may postdate what was read.
                if snapshot['generation'] == generation:
                    snapshot.update(at=time.monotonic(), entry=entry)
            return entry

    def drop_snapshot(request: Request):
        try:
            yield
        finally:
            if request.method != 'GET':
                with cache_lock:
                    snapshot.update(generation=snapshot['generation'] + 1, entry=None)

    def receipt(call, *args, **kwargs):
        with controller.receipts():
            return call(*args, **kwargs)
    # 'function' scope drops the cache before the response is sent, so the
    # refetch that follows a write never receives the pre-write snapshot.
    router = APIRouter(prefix='/api/agents', tags=['Agents'],
                       dependencies=[Depends(require_member), Depends(drop_snapshot, scope='function')])
    # Registered first: the trailing /{agent_id} route would otherwise match.
    from . import fact_routes
    fact_routes.register(router, controller)

    @router.get('')
    def status(request: Request):
        entry = team_snapshot()
        if 'gzip' in request.headers.get('accept-encoding', '').lower():
            return Response(entry['gzip'], media_type='application/json',
                            headers={'Content-Encoding': 'gzip', 'Vary': 'Accept-Encoding'})
        return Response(entry['body'], media_type='application/json', headers={'Vary': 'Accept-Encoding'})

    # The compact team snapshot leaves these out; the dashboard loads them
    # when a role's memory or a finished draft's sources are opened.
    @router.get('/drafts/{draft_id}')
    def draft(draft_id: str):
        return {'draft': controller.draft(draft_id)}

    # Writes below return a receipt ({'ok': true, ...}); the dashboard then
    # refetches the team snapshot once instead of receiving a second copy.
    @router.post('/settings')
    def settings(payload: SettingsRequest):
        return receipt(controller.settings, payload.model_dump(exclude_none=True, exclude_unset=True))

    # One switch for full throttle: ON enables every role and removes the rate
    # limit (snapshotting the prior state); OFF restores what it snapshotted.
    @router.post('/rocket-mode')
    def rocket_mode(payload: RocketModeRequest):
        return receipt(controller.rocket_mode, payload.on)

    @router.get('/quotas')
    def quotas():
        return controller.quotas()

    @router.post('/quotas')
    def quota_settings(payload: QuotaRequest):
        return controller.quotas(payload.model_dump())

    @router.get('/reading-selection')
    def reading_selection():
        return controller.reading_selection()

    @router.get('/competition')
    def competition():
        return controller.competition()

    @router.get('/review')
    def review():
        return controller.review()

    @router.post('/review')
    def review_settings(payload: ReviewSettingsRequest):
        return controller.review(payload.model_dump())

    @router.post('/competition')
    def competition_settings(payload: CompetitionRequest):
        return controller.competition(payload.model_dump(exclude_unset=True))

    @router.post('/save')
    def save(payload: SaveRequest):
        return receipt(controller.save, {**payload.model_dump(exclude_none=True, exclude_unset=True),
                                         'reading_selection_mode': 'rules'})

    @router.post('/generate-persona', status_code=202)
    def generate_persona(payload: GeneratePersonaRequest, request: Request, member: dict = Depends(require_member)):
        values = payload.model_dump(exclude_none=True, exclude_unset=True)
        return app.state.ai_jobs.start(member['email'],
            lambda cancelled, progress: controller.generate_persona(
                values, cancellation_event=cancelled, on_progress=progress),
            kind='persona', label='生成角色背景',
            scope=__import__('urllib.parse',fromlist=['unquote']).unquote(request.headers.get('x-ctn-job-scope','')),
            request_key=request.headers.get('x-ctn-job-key'))

    @router.post('/register')
    def register(payload: RegisterRequest):
        return receipt(controller.register, payload.agent_id, payload.password)

    @router.post('/bind')
    def bind(payload: BindRequest):
        return receipt(controller.bind, payload.agent_id, payload.username, payload.password)

    @router.post('/recover-account')
    def recover_account(payload: AgentID):
        return receipt(controller.recover_account, payload.agent_id)

    @router.post('/read')
    def read(payload: AgentID):
        return receipt(controller.read, payload.agent_id)

    @router.post('/run', status_code=202)
    def run(payload: AgentID):
        return receipt(controller.start_generation, payload.agent_id)

    @router.post('/cancel-generation')
    def cancel_generation(payload: AgentID):
        return receipt(controller.cancel_generation, payload.agent_id)

    @router.post('/notifications/confirm-skip')
    def confirm_notification_skip(payload: DraftID):
        from . import agent_notifications
        return receipt(agent_notifications.confirm_skip, controller, payload.draft_id)

    @router.post('/notifications/reconsider')
    def reconsider_notification(payload: NotificationReconsiderRequest):
        from . import agent_notifications
        return receipt(agent_notifications.reconsider, controller, payload.agent_id, payload.notification_id)

    @router.post('/manual/prepare')
    def prepare_manual(payload: ManualPrepareRequest):
        return controller.prepare_manual(payload.model_dump(exclude_none=True))

    @router.post('/manual/queue')
    def queue_manual(payload: ManualQueueRequest, member: dict = Depends(require_member)):
        from . import agent_manual
        return receipt(agent_manual.queue_publish, controller, payload.draft_id, payload.body,
                       requested_by=member.get('email', ''))

    @router.post('/manual/queue/cancel')
    def cancel_manual_queue(payload: DraftID):
        from . import agent_manual
        return receipt(agent_manual.cancel_queue, controller, payload.draft_id)

    @router.post('/publish')
    def publish(payload: PublishRequest):
        if payload.confirmation_post_id is None:
            result = receipt(controller.publish, payload.draft_id, payload.body)
        else:
            result = receipt(controller.publish, payload.draft_id, payload.body,
                             confirmation_post_id=payload.confirmation_post_id)
        if not isinstance(result, dict):
            return result
        # The manual-action panel shows the outcome from the response draft.
        try:
            return {**result, 'draft': controller.draft(payload.draft_id)}
        except AgentError:
            return {**result, 'draft': None}

    @router.post('/discard')
    def discard(payload: DraftID):
        return receipt(controller.discard, payload.draft_id)

    @router.post('/discard-unexecuted')
    def discard_unexecuted(payload: DiscardUnexecutedRequest, request: Request):
        member = app.state.auth.user(request)
        if not member:
            raise HTTPException(401, '请先登录工作台')
        return receipt(controller.discard_unexecuted, payload.draft_id,
            confirmed_not_executed=payload.confirmed_not_executed,
            reason=payload.reason, expected_version=payload.expected_version,
            confirmed_by=member['email'])

    @router.post('/memory')
    def memory(payload: MemoryRequest):
        return receipt(controller.memory, payload.agent_id, payload.memory)

    @router.get('/memory-history')
    def memory_history(agent_id: str, q: str = Query('', max_length=240),
                       kind: Literal['all', 'reading', 'outcome', 'viewpoints', 'questions', 'people'] = 'all',
                       limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return controller.memory_history(agent_id, q=q, kind=kind, limit=limit, offset=offset)

    @router.post('/reset-memory')
    def reset_memory(payload: AgentID):
        return receipt(controller.reset_memory, payload.agent_id)

    @router.post('/reconcile')
    def reconcile(payload: ReconcileRequest):
        return receipt(controller.reconcile, payload.draft_id, payload.post_id)

    @router.get('/{agent_id}/logs/export')
    def export_logs(agent_id: str, format: Literal['csv'] = 'csv',
                    kind: str = '', q: str = ''):
        rows = controller.export_logs(agent_id, kind=kind, q=q)
        def chunks():
            yield '\ufeff'
            yield from rows
        return StreamingResponse(chunks(), media_type='text/csv; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="agent-{agent_id}-logs.csv"',
                     'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @router.get('/{agent_id}/logs')
    def logs(agent_id: str, kind: str = '', q: str = '',
             limit: int = Query(default=50, ge=1, le=200), cursor: str | None = None):
        return controller.logs(agent_id, kind=kind, q=q, limit=limit, cursor=cursor)

    @router.get('/{agent_id}/memory')
    def memory_view(agent_id: str):
        return controller.memory_view(agent_id)

    @router.get('/{agent_id}/social')
    def social_inventory(agent_id: str, kind: Literal['posts', 'likes', 'following'] = 'posts',
                         limit: int = Query(default=20, ge=1, le=50),
                         cursor: str | None = Query(default=None, max_length=500)):
        return controller.social_inventory(agent_id, kind=kind, limit=limit, cursor=cursor)

    @router.get('/{agent_id}')
    def detail(agent_id: str):
        return controller.detail(agent_id)

    app.router.routes.extend(router.routes)
    return controller
