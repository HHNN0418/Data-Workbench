"""Server-owned response shape for models with strict JSON output support."""


def partition_notification_bindings(action, target_post_id, notification_ids, sources):
    """Keep only explicit acknowledgements belonging to a direct post target.

    The caller first validates that every selected ID exists in this reading.
    A parent/root in the same discussion never inherits a child's notification.
    Original-post responses and deliberate skips retain their explicit choices.
    """
    selected = list(dict.fromkeys(notification_ids))
    if action not in ('reply', 'repost', 'like'):
        return selected, []
    bound = {value for source in sources if source.get('post_id') == target_post_id
             for value in source.get('notification_ids', []) if type(value) is int and value > 0}
    return ([value for value in selected if value in bound],
            [value for value in selected if value not in bound])


POST_TARGET_ACTIONS = ('reply', 'repost', 'like')
ACCOUNT_TARGET_ACTIONS = ('follow', 'unfollow')
# The mix (D57, 22 Oct 20:00). Replying first (D52) cut our originals from
# 16-23 to 1-6 an hour, and official NPC views fell from 14-22 to 3-13 an hour:
# views and likes follow originals. So a citizen answering us always gets the
# reply; apart from that a role writes two originals per reply, and that reply
# goes to a precise citizen target (a reply-queue pick or a post younger than
# PRECISE_TARGET_SECONDS). Targets are already citizen-only (target_blocked).
PRECISE_TARGET_SECONDS = 600
ORIGINALS_PER_REPLY = 2
_MIX_STATES = ('pending', 'sending', 'uncertain', 'published')


def _epoch(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _open(source):
    return (source.get('kind') == 'post' and type(source.get('post_id')) is int and not source.get('target_blocked')
            and not source.get('interaction_blocked') and not source.get('context_only'))


def _precise(source, now):
    if isinstance(source.get('opportunity'), dict):
        return True
    created = _epoch(source.get('created_at')) if source.get('created_at') else None
    return created is not None and now - created < PRECISE_TARGET_SECONDS


def next_mix(sources, drafts, agent_id, now):
    """'reply', 'post', or None to leave the choice to the model.

    'reply' when an open source is a citizen answering us, or when the role's
    last ORIGINALS_PER_REPLY writes were originals and an open source is a
    precise target; otherwise 'post'. None while the role has fewer writes
    than that and no one is answering it.
    """
    open_sources = [source for source in sources or () if _open(source)]
    if any(source.get('notification_ids') for source in open_sources):
        return 'reply'
    recent = sorted((d for d in (drafts or {}).values()
                     if d.get('agent_id') == agent_id and d.get('action') in ('post', 'reply')
                     and d.get('state') in _MIX_STATES),
                    key=lambda d: (d.get('created_at') or '', d.get('id') or ''))
    if len(recent) < ORIGINALS_PER_REPLY:
        return None
    if any(d['action'] == 'reply' for d in recent[-ORIGINALS_PER_REPLY:]):
        return 'post'
    return 'reply' if any(_precise(source, now) for source in open_sources) else 'post'


def effective_actions(agent, sources, relationship_context=None):
    """The allowed actions that have at least one valid target in this reading.

    An action that needs a target is dropped when every candidate is blocked or
    absent, so the model cannot pick an action that is rejected after the call.
    The result can be empty; the caller then skips the model call.
    """
    from . import agent_relationships
    allowed = list(agent.get('allowed_actions', ()))
    evidence = {'action': 'reply', 'sources': sources, 'relationship_context': relationship_context or {}}
    # Policy-blocked (internal Agent) targets keep their terminal local-block
    # path; only target_blocked sources, never valid, remove an action.
    has_posts = bool(agent_relationships.allowed_post_targets(evidence, include_policy_blocked=True))
    has_liked = bool(agent_relationships.allowed_post_targets({**evidence, 'action': 'unlike'},
                                                              include_policy_blocked=True))
    has_accounts = bool(agent_relationships.allowed_account_targets(evidence, include_policy_blocked=True)
                        - {agent.get('account_id')})
    return [action for action in allowed if not (
        (action in POST_TARGET_ACTIONS and not has_posts)
        or (action == 'unlike' and not has_liked)
        or (action in ACCOUNT_TARGET_ACTIONS and not has_accounts))]


def response_schema(agent, sources, relationship_context=None):
    from . import agent_relationships
    source_ids = list(dict.fromkeys(s['id'] for s in sources))
    # Liked posts from relationship_context are valid only for unlike. Offer them
    # only to a role that may unlike, so reply/like/repost are not led to them.
    may_unlike = 'unlike' in agent.get('allowed_actions', ())
    evidence = {'action': 'unlike' if may_unlike else 'reply', 'sources': sources,
                'relationship_context': relationship_context or {}}
    targets = sorted(agent_relationships.allowed_post_targets(evidence))
    source_targets = agent_relationships.allowed_post_targets({**evidence, 'action': 'reply'})
    unlike_only = sorted(set(targets) - source_targets)
    accounts = sorted(agent_relationships.allowed_account_targets(evidence) - {agent.get('account_id')})
    # A role without a post or account action is offered no such target at all.
    if not any(a in agent.get('allowed_actions', ()) for a in ('reply', 'repost', 'like', 'unlike')):
        targets, unlike_only = [], []
    if not any(a in agent.get('allowed_actions', ()) for a in ('follow', 'unfollow')):
        accounts = []
    notifications = sorted({value for source in sources for value in source.get('notification_ids', [])
                            if type(value) is int and value > 0})
    observed_authors = sorted({source['author_id'] for source in sources
                              if type(source.get('author_id')) is int and source['author_id'] > 0
                              and source['author_id'] != agent.get('account_id')})
    allowed = list(agent['allowed_actions'])
    silent = [a for a in ('like', 'unlike', 'follow', 'unfollow', 'skip') if a in allowed]
    direct = [a for a in ('reply', 'repost', 'like') if a in allowed]
    relation = [a for a in ('follow', 'unfollow', 'unlike') if a in allowed]
    body_note = 'Post/reply text, at most 500 characters.' + (
        ' Empty for ' + '/'.join(silent) + '.' if silent else '') + (
        ' Optional repost comment.' if 'repost' in allowed else '')
    notification_note = ('Explicitly selected notifications actually handled by this decision. ' + (
        'For ' + '/'.join(direct) + ', choose only IDs listed on the source whose post_id equals '
        'target_post_id; a parent/root in the same thread does not share those IDs. ' if direct else '') + (
        'Use [] for ' + '/'.join(relation) + ' and unrelated original posts. ' if relation else
        'Use [] for unrelated original posts. ') + (
        'For ' + '/'.join(a for a in ('post', 'skip') if a in allowed) +
        ', select only notifications deliberately addressed or skipped.'
        if any(a in allowed for a in ('post', 'skip')) else ''))
    evidence_ids = {'type': 'array', 'minItems': 1, 'maxItems': 4,
                    'items': {'type': 'string', 'enum': source_ids}}
    note = {'type': 'object', 'additionalProperties': False,
            'required': ['text', 'source_ids'],
            'properties': {'text': {'type': 'string', 'maxLength': 320}, 'source_ids': evidence_ids}}
    person = {'type': 'object', 'additionalProperties': False,
              'required': ['account_id', 'text', 'source_ids'],
              'properties': {'account_id': {'type': 'integer', **({'enum': observed_authors} if observed_authors else {})},
                             'text': {'type': 'string', 'maxLength': 320}, 'source_ids': evidence_ids}}
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['action', 'body', 'target_post_id', 'target_account_id', 'source_ids', 'notification_ids', 'memory', 'reason', 'memory_updates'],
        'properties': {
            'action': {'type': 'string', 'enum': effective_actions(agent, sources, relationship_context) or allowed},
            'body': {'type': 'string', 'maxLength': 500, 'description': body_note},
            'target_post_id': {'type': ['integer', 'null'], 'enum': [None] + targets,
                               **({'description': 'IDs ' + ', '.join(map(str, unlike_only)) +
                                   ' are previously liked posts and valid only for unlike.'} if unlike_only else {})},
            'target_account_id': {'type': ['integer', 'null'], 'enum': [None] + accounts},
            'source_ids': {'type': 'array', 'maxItems': 20,
                           'items': {'type': 'string', 'enum': source_ids}},
            'notification_ids': {'type': 'array', 'maxItems': 100 if notifications else 0,
                                 'description': notification_note,
                                 'items': {'type': 'integer', **({'enum': notifications} if notifications else {})}},
            'memory': {'type': 'string', 'maxLength': 1800,
                       'description': 'Optional brief legacy decision narrative for audit only. It does not update personal memory or establish delivery; may be empty.'},
            'memory_updates': {'type': 'object', 'additionalProperties': False,
                'required': ['viewpoints', 'questions', 'people'],
                'description': 'Small changes in subjective views, open questions (not reply obligations), and impressions of authors actually read. Cite supplied source IDs. No action completion claims. Empty arrays when unchanged.',
                'properties': {
                    'viewpoints': {'type': 'array', 'maxItems': 3, 'items': note},
                    'questions': {'type': 'array', 'maxItems': 3, 'items': note},
                    'people': {'type': 'array', 'maxItems': 3 if observed_authors else 0, 'items': person},
                }},
            'reason': {'type': 'string', 'maxLength': 500},
        },
    }
