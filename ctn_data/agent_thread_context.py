"""Pack real discussion units without letting reply targets crowd out context.

The loader owns local/remote access. This module only copies supplied sources,
loads the directly identified parent/root in at most two batches, and budgets
the exact JSON list returned to the caller. Missing text is never reconstructed.
"""
from __future__ import annotations

import json


MIN_EXCERPT = 80


def _positive(value):
    return value if type(value) is int and value > 0 else None


def _length(rows):
    return len(json.dumps(rows, ensure_ascii=False))


def _reply(source):
    return (source.get('post_kind') == 'reply' or source.get('kind') == 'reply'
            or _positive(source.get('reply_to_post_id')) is not None
            or _positive(source.get('reply_to')) is not None)


def _source(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id']:
        return None
    result = dict(raw)
    body = raw.get('text', raw.get('body', ''))
    result['text'] = body if isinstance(body, str) else ''
    # source dicts normally use text. Normalize the older body-only shape once.
    if 'text' not in raw:
        result.pop('body', None)
    original_length = raw.get('text_length')
    result['text_length'] = max(len(result['text']), original_length) if type(original_length) is int else len(result['text'])
    truncated = (bool(raw.get('text_truncated')) or raw.get('coverage') == 'excerpt'
                 or result['text_length'] > len(result['text']))
    result['text_truncated'] = truncated
    result['coverage'] = ('title_only' if raw.get('coverage') == 'title_only' else
                          'excerpt' if truncated else 'full')
    return result


def _shorten(rows, cap):
    result = []
    for row in rows:
        copy = dict(row)
        body = row['text']
        copy['text'] = body[:cap]
        if len(copy['text']) < len(body):
            copy.update(text_truncated=True, coverage='excerpt')
        result.append(copy)
    return result


def _fit(rows, budget):
    """Keep short quotes, then divide remaining space fairly among long ones."""
    if _length(rows) <= budget:
        return rows
    largest = max((len(row['text']) for row in rows), default=0)
    smallest = min(MIN_EXCERPT, largest)
    if _length(_shorten(rows, smallest)) > budget:
        return None
    low, high = smallest, largest
    best = _shorten(rows, smallest)
    while low <= high:
        cap = (low + high) // 2
        candidate = _shorten(rows, cap)
        if _length(candidate) <= budget:
            best, low = candidate, cap + 1
        else:
            high = cap - 1
    return best


def pack(selected_targets, context_loader, *, max_sources=20, max_context=12000):
    """Return ``(sources, metadata)`` with strict source-count/JSON-char bounds.

    ``context_loader(ids)`` returns real source dicts, normally via
    ``agent_observation._local_posts``. Replies need their direct parent and
    root; a parent can reveal a missing root ID. No ancestor recursion occurs.
    Targets retain identity even when another target uses them as background.
    The metadata is diagnostic and is not part of the source-list budget.
    """
    if type(max_sources) is not int or max_sources < 0:
        raise ValueError('max_sources must be a nonnegative integer')
    if type(max_context) is not int or max_context < 2:
        raise ValueError('max_context must fit at least an empty JSON list')
    targets, invalid, duplicates = {}, 0, 0
    for raw in selected_targets or []:
        source = _source(raw)
        if source is None:
            invalid += 1
            continue
        if source['id'] in targets:
            duplicates += 1
            previous = targets[source['id']]
            previous['notification_ids'] = list(dict.fromkeys(
                list(previous.get('notification_ids') or []) + list(source.get('notification_ids') or [])))
            continue
        targets[source['id']] = source

    cache = {source['post_id']: source for source in targets.values() if _positive(source.get('post_id'))}
    unavailable, loaded_ids = {}, set()

    def load(ids):
        requested = sorted({value for value in ids if _positive(value) and value not in cache and value not in unavailable})
        if not requested:
            return
        try:
            result = context_loader(requested)
            for raw in result or []:
                source = _source(raw)
                post_id = _positive((source or {}).get('post_id'))
                if post_id in requested and raw.get('available') is not False and post_id not in cache:
                    cache[post_id] = source
                    loaded_ids.add(post_id)
        except Exception:
            for post_id in requested:
                if post_id not in cache:
                    unavailable[post_id] = 'load_failed'
        for post_id in requested:
            if post_id not in cache:
                unavailable.setdefault(post_id, 'unavailable')

    plans = {}
    parent_ids = []
    for key, target in targets.items():
        post_id = _positive(target.get('post_id'))
        parent = (_positive(target.get('reply_to_post_id')) or _positive(target.get('reply_to'))) if _reply(target) else None
        root = _positive(target.get('root_post_id')) if _reply(target) else None
        invalid_parent = bool(post_id and parent == post_id)
        invalid_root = bool(post_id and root == post_id)
        plans[key] = {'reply': _reply(target), 'parent': None if invalid_parent else parent,
                      'root': None if invalid_root else root,
                      'parent_reason': 'self_reference' if invalid_parent else 'unknown_id',
                      'root_reason': 'self_reference' if invalid_root else 'unknown_id'}
        if parent and not invalid_parent:
            parent_ids.append(parent)
    if max_sources and targets:
        load(parent_ids)

    roots = []
    for key, plan in plans.items():
        parent = cache.get(plan['parent'])
        if plan['reply'] and parent:
            inferred = _positive(parent.get('root_post_id'))
            if inferred is None and parent.get('post_kind') == 'post':
                inferred = _positive(parent.get('post_id'))
            if plan['root'] and inferred and inferred != plan['root']:
                plan['conflicting_parent_root'] = inferred
            elif plan['root'] is None and plan['root_reason'] != 'self_reference' and inferred and inferred != _positive(targets[key].get('post_id')):
                plan.update(root=inferred, root_inferred_from_parent=True)
        if plan['root']:
            roots.append(plan['root'])
    if max_sources and targets:
        load(roots)

    def dependencies(key):
        plan = plans[key]
        result = []
        for role in ('parent', 'root'):
            value = plan[role]
            if value and all(item[0] != value for item in result):
                result.append((value, role))
        return result

    def render(pool):
        output = []
        for key, raw in pool.items():
            row = dict(raw)
            is_target = key in targets
            row.update(context_only=not is_target, context_role='target' if is_target else 'background')
            if is_target and plans[key]['reply']:
                plan = plans[key]
                missing = []
                for role in ('parent', 'root'):
                    post_id = plan[role]
                    if post_id is None:
                        missing.append({'post_id': None, 'role': role, 'reason': plan[role + '_reason']})
                    else:
                        source = cache.get(post_id)
                        if source is None or source['id'] not in pool:
                            reason = unavailable.get(post_id, 'budget')
                        elif not source['text'].strip() or source.get('coverage') == 'title_only':
                            reason = 'text_unavailable'
                        else:
                            continue
                        previous = next((item for item in missing if item['post_id'] == post_id), None)
                        if previous:
                            previous['role'] = 'parent/root'
                        else:
                            missing.append({'post_id': post_id, 'role': role, 'reason': reason})
                if plan.get('conflicting_parent_root'):
                    missing.append({'post_id': plan['root'], 'role': 'root', 'reason': 'conflicting_root_ids',
                                    'parent_root_post_id': plan['conflicting_parent_root']})
                row['thread_context'] = {'parent_post_id': plan['parent'], 'root_post_id': plan['root'],
                    'missing': missing, 'complete': not missing}
                if plan.get('root_inferred_from_parent'):
                    row['thread_context']['root_inferred_from_parent'] = True
            output.append(row)
        return output

    pool, output, deferred = {}, [], []
    for key, target in targets.items():
        trial = dict(pool)
        trial.setdefault(key, target)
        for post_id, _ in dependencies(key):
            source = cache.get(post_id)
            if source:
                trial.setdefault(source['id'], source)
        fitted = _fit(render(trial), max_context) if len(trial) <= max_sources else None
        if fitted is None:
            deferred.append(key)
        else:
            pool, output = trial, fitted

    # Whole units take priority. Fill context for already included targets
    # before using spare capacity for an explicitly incomplete new target.
    for key in ([key for key in deferred if key in pool] + [key for key in deferred if key not in pool]):
        # A selected target may already be present as someone else's parent.
        # Spare slots can still supply its own parent/root, not merely new targets.
        trial = dict(pool)
        trial.setdefault(key, targets[key])
        fitted = _fit(render(trial), max_context) if len(trial) <= max_sources else None
        if fitted is None:
            continue
        pool, output = trial, fitted
        for post_id, _ in dependencies(key):
            source = cache.get(post_id)
            if not source or source['id'] in pool:
                continue
            trial = {**pool, source['id']: source}
            fitted = _fit(render(trial), max_context) if len(trial) <= max_sources else None
            if fitted is not None:
                pool, output = trial, fitted

    included = {row['id'] for row in output if not row['context_only']}
    missing = [item for row in output for item in row.get('thread_context', {}).get('missing', [])]
    metadata = {'requested_count': len(targets), 'included_count': len(included),
                'missing_count': len(missing), 'deferred_count': len(targets) - len(included),
                'background_count': sum(row['context_only'] for row in output),
                'loaded_count': len(loaded_ids), 'invalid_count': invalid, 'duplicate_count': duplicates,
                'serialized_characters': _length(output),
                'max_sources': max_sources, 'max_context': max_context}
    return output, metadata
