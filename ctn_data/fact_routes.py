"""Authenticated fact bank endpoints, registered on the Agent router."""
from typing import Literal

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from . import fact_bank


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FactSettingsRequest(StrictModel):
    review_mode: Literal['manual', 'auto']
    extract_enabled: StrictBool
    provider_id: StrictStr = Field(max_length=64)
    pipeline: Literal['single', 'staged'] | None = None
    expected_revision: StrictInt = Field(ge=1)


class FactEdits(StrictModel):
    claim: StrictStr | None = Field(default=None, max_length=fact_bank.MAX_CLAIM)
    concerns: list[StrictStr] | None = Field(default=None, max_length=6)
    topic_key: StrictStr | None = Field(default=None, max_length=60)
    valid_until: StrictStr | None = Field(default=None, max_length=40)


class FactDecisionRequest(StrictModel):
    id: StrictStr = Field(max_length=80)
    action: Literal['approve', 'reject', 'retire', 'restore', 'edit']
    expected_revision: StrictInt = Field(ge=1)
    note: StrictStr = Field(default='', max_length=500)
    edits: FactEdits | None = None


class FactCreateRequest(StrictModel):
    claim: StrictStr = Field(max_length=fact_bank.MAX_CLAIM)
    concerns: list[StrictStr] = Field(max_length=6)
    source_label: StrictStr = Field(max_length=200)
    topic_key: StrictStr | None = Field(default=None, max_length=60)
    valid_until: StrictStr | None = Field(default=None, max_length=40)


class RebuttalRequest(StrictModel):
    concern: StrictStr = Field(max_length=40)
    no_argument: StrictStr = Field(max_length=400)
    line: StrictStr = Field(max_length=400)
    fact_ids: list[StrictStr] = Field(default_factory=list, max_length=8)
    status: Literal['active', 'retired'] = 'active'
    expected_revision: StrictInt | None = None


class GuardrailRequest(StrictModel):
    id: StrictStr | None = Field(default=None, max_length=80)
    rule: StrictStr = Field(max_length=400)
    status: Literal['active', 'retired'] = 'active'
    expected_revision: StrictInt | None = None


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except fact_bank.FactBankError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


def register(router, controller):
    from .agent_review import _providers
    db = controller.db_path

    def overview():
        settings = fact_bank.configuration(controller.store.read())
        return {'settings': settings, 'providers': _providers(controller),
                'concerns': sorted(fact_bank.CONCERNS), 'topics': list(fact_bank.TOPICS)}

    @router.get('/facts/settings')
    def fact_settings():
        return overview()

    @router.post('/facts/settings')
    def save_fact_settings(payload: FactSettingsRequest):
        _call(fact_bank.settings, controller, payload.model_dump(exclude_none=True))
        return overview()

    @router.get('/facts/list')
    def facts(status: Literal['pending', 'active', 'rejected', 'superseded', 'retired'] | None = None,
              layer: Literal['core', 'news'] | None = None, q: str = Query(default='', max_length=200),
              limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)):
        return _call(fact_bank.list_facts, db, status=status, layer=layer, q=q, limit=limit, offset=offset)

    @router.post('/facts/decision')
    def decide(payload: FactDecisionRequest):
        edits = payload.edits.model_dump(exclude_none=True) if payload.edits else None
        return _call(fact_bank.decide, db, payload.id, payload.action,
                     expected_revision=payload.expected_revision, note=payload.note, edits=edits)

    @router.post('/facts/approve-pending')
    def approve_pending():
        return _call(fact_bank.approve_pending, db)

    @router.post('/facts/create')
    def create(payload: FactCreateRequest):
        return _call(fact_bank.create_fact, db, payload.model_dump())

    @router.get('/facts/prompts')
    def prompts():
        """Read-only view of the instructions each model step receives."""
        from . import agent_pipeline, concern_radar, fact_extraction
        # Reading is by rules only (D67), so its old model prompt is not listed.
        return {'pipeline': fact_bank.configuration(controller.store.read())['pipeline'], 'prompts': [
            {'key': 'radar', 'label': '顾虑雷达（内容分析）', 'applies_to': 'research', 'text': concern_radar.SYSTEM},
            {'key': 'decide', 'label': '决策', 'applies_to': 'staged', 'text': agent_pipeline.DECIDE_SYSTEM,
             'appendix': [{'label': '顾虑雷达生效时追加', 'text': agent_pipeline.RADAR_NOTE},
                          {'label': '禁止 Agent 内部互动时追加', 'text': agent_pipeline.BLOCKED_NOTE},
                          {'label': '输出被截断后重试时追加', 'text': agent_pipeline.TRUNCATION_RETRY}]},
            {'key': 'write', 'label': '写作', 'applies_to': 'staged', 'text': agent_pipeline.WRITE_SYSTEM},
            {'key': 'check', 'label': '检查', 'applies_to': 'staged', 'text': agent_pipeline.CHECK_SYSTEM},
            {'key': 'extract', 'label': '新闻事实提取', 'applies_to': 'extraction', 'text': fact_extraction.SYSTEM}],
            'placeholders': {'{name}': '角色名', '{team_goal}': '队伍立场', '{kind}': 'post 或 reply',
                             '{stance}': '角色立场', '{limit}': '字数目标（回复 320，原创帖 480）', '{voice}': '角色说话方式', '{concern_hint}': '本次要回应的顾虑',
                             '{question}': '回复时要求以问题结尾', '{mentions}': '@ 提及上限',
                             '{allowed}': '角色开放的动作', '{action_guide}': '仅列出开放动作的说明',
                             '{target_rules}': '仅针对开放动作的目标与通知规则'}}

    @router.get('/facts/extractions')
    def extractions(limit: int = Query(default=30, ge=1, le=100)):
        from . import fact_extraction
        return fact_extraction.recent(db, limit)

    @router.get('/facts/gaps')
    def fact_gaps():
        """Fact gaps from the newest concern radar, for the operator to fill."""
        from . import content_research
        state = content_research.snapshot(db)
        brief = next((row for row in state['briefs'] if row.get('radar')), None)
        if brief is None:
            return {'brief_id': None, 'generated_at': None, 'gaps': []}
        return {'brief_id': brief['brief_id'], 'generated_at': brief['generated_at'],
                'expired': brief['expired'], 'gaps': brief['radar'].get('fact_gaps', [])}

    @router.get('/facts/rules')
    def rules():
        return fact_bank.list_rules(db)

    @router.post('/facts/rebuttal')
    def save_rebuttal(payload: RebuttalRequest):
        values = payload.model_dump()
        return _call(fact_bank.save_rebuttal, db, values.pop('concern'), values)

    @router.post('/facts/guardrail')
    def save_guardrail(payload: GuardrailRequest):
        values = payload.model_dump()
        return _call(fact_bank.save_guardrail, db, values.pop('id'), values)
