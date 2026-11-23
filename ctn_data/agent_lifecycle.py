"""Small shared compatibility rules for durable agent intentions."""


def persona_revision(agent):
    """Keep old profiles comparable until they acquire a separate revision."""
    return agent.get('persona_revision', agent.get('version'))


def draft_matches_persona(draft, agent):
    """Never infer that a legacy, already-stale intention is current.

    Migration may stamp an old draft only while its legacy version exactly
    matches the profile version. Otherwise it retains this strict old check.
    """
    revision = draft.get('persona_revision')
    if revision is not None:
        current = persona_revision(agent)
        return type(revision) is int and type(current) is int and revision == current
    previous, current = draft.get('persona_version'), agent.get('version')
    return type(previous) is int and type(current) is int and previous == current


def waiting_for_publication(state, agent):
    """True while the role's generation waits on a pending draft of its job that can still be sent.

    A draft written before the profile changed never matches the persona and is
    never sent, so it must not hold the generation; it held Maya's for six hours
    on 22 Oct until the draft was discarded by hand.
    """
    job = agent.get('generation', {})
    return (job.get('status') == 'waiting' and job.get('waiting_for') == 'publish'
            and any(d.get('agent_id') == agent['id'] and d.get('generation_job_id') == job.get('id')
                    and d.get('state') == 'pending' and draft_matches_persona(d, agent)
                    for d in state['drafts'].values()))
