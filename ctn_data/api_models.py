"""Validated local-operation request contracts exposed in OpenAPI."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt


class RequestModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CollectRequest(RequestModel):
    page_limit: Annotated[StrictInt, Field(ge=1, le=100)] | None = None
    max_pages: Annotated[StrictInt, Field(ge=1, le=20)] | None = None
    counter_limit: Annotated[StrictInt, Field(ge=0, le=300)] | None = None
    collect_news: StrictBool | None = None
    collect_trending: StrictBool | None = None


class SchedulerRequest(RequestModel):
    enabled: StrictBool
    interval_seconds: Annotated[StrictInt, Field(ge=30, le=86400)] = 300


class ActivityImport(RequestModel):
    csv: str
    filename: str = 'activity.csv'


class ScoreSnapshot(RequestModel):
    total: str | StrictFloat | StrictInt
    observed_at: str | None = None
    source: str = 'manual'
    note: str = ''


class TeamAccounts(RequestModel):
    account_ids: Annotated[list[Annotated[StrictInt, Field(gt=0)]], Field(min_length=1, max_length=1000)]
    evidence: Annotated[str, Field(min_length=1)]
    team_label: str | None = None
