"""Citizen proxy for C's read models, delegating to the shared agent_targeting rule.

agent_targeting.citizen_accounts is the calibrated proxy from
reports/generated/deep-analysis/citizens.md: not ours, not bot-flagged, fewer
than 25 stored actions, not verified, not a repost ring and no batch-numbered
name or LLM artefact. This adapter adds our own accounts and lets callers
exclude members of coordination clusters.
"""
from __future__ import annotations

from . import agent_targeting
from .db import connect


def own_accounts(db_path):
    conn = connect(db_path)
    try:
        return {row['account_id'] for row in conn.execute('SELECT account_id FROM team_accounts WHERE is_own=1')}
    finally:
        conn.close()


def citizen_authors(db_path, author_ids, *, exclude=(), strict=False):
    """The subset of ``author_ids`` that passes the shared citizen proxy (``strict``: see citizen_accounts)."""
    authors = {value for value in author_ids if type(value) is int} - set(exclude)
    if not authors:
        return set()
    return set(agent_targeting.citizen_accounts(db_path, authors, own=own_accounts(db_path), strict=strict))
