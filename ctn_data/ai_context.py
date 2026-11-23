"""Select bounded, auditable evidence for an external model without a cloud call."""
from __future__ import annotations

from collections import Counter, OrderedDict
from copy import deepcopy
import json
import math
import os
import re

DEFAULT_EVIDENCE_TOKENS = 8000
META_FIELDS = ('summary', 'features', 'inspected_features', 'research_context',
               'reporting_context', 'scoring_context', 'tag_context', 'caveats', 'retrieval')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e')


def estimate_tokens(value):
    """Conservative heuristic over the entire serialized JSON, NOT actual tokens.

    Count ASCII words at two characters/token, punctuation individually, CJK at
    two tokens/character and other non-ASCII at up to UTF-8 byte length. The
    provider's tokenizer may differ, so callers must reserve prompt/output space.
    """
    encoded = _encode(value)
    count = 0
    for run in re.findall(r'[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]', encoded):
        if run.isspace():
            count += math.ceil(len(run) / 2)
        elif run.isascii() and re.fullmatch(r'[A-Za-z0-9_]+', run):
            count += math.ceil(len(run) / 2)
        else:
            count += sum(1 if ord(char) < 128 else 2 if '\u3400' <= char <= '\u9fff' else len(char.encode('utf-8')) for char in run)
    return count


def evidence_budget(budget_tokens=None):
    if budget_tokens is None:
        value = os.environ.get('CTN_AI_EVIDENCE_TOKENS', str(DEFAULT_EVIDENCE_TOKENS))
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError('CTN_AI_EVIDENCE_TOKENS must be an integer between 2048 and 32000') from exc
        if not 2048 <= value <= 32000:
            raise ValueError('CTN_AI_EVIDENCE_TOKENS must be between 2048 and 32000')
        return value
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or not 512 <= budget_tokens <= 64000:
        raise ValueError('Evidence token budget must be an integer between 512 and 64000')
    return budget_tokens


def _bound(value, *, items=12, chars=500, depth=6):
    """Return bounded metadata plus whether any content was omitted."""
    if depth <= 0 and isinstance(value, (dict, list)):
        return {'omitted_items': len(value)}, True
    if isinstance(value, str):
        return value[:chars], len(value) > chars
    if isinstance(value, list):
        rows, shortened = [], len(value) > items
        for item in value[:items]:
            row, changed = _bound(item, items=items, chars=chars, depth=depth - 1)
            rows.append(row)
            shortened |= changed
        return rows, shortened
    if isinstance(value, dict):
        # Schema fields describe what a number means. Reduce record counts,
        # never reduce an account to the first two arbitrary dictionary fields.
        width = 128
        rows, shortened = {}, len(value) > width
        for key, item in list(value.items())[:width]:
            child_items = max(3, items) if str(key) in {'accounts', 'candidates', 'items'} else items
            row, changed = _bound(item, items=child_items, chars=chars, depth=depth - 1)
            rows[str(key)[:120]] = row
            shortened |= changed or len(str(key)) > 120
        return rows, shortened
    return deepcopy(value), False


def _metadata(evidence, budget, mode):
    reporting = mode in {'reporting', 'report', 'screening'}
    target = max(80, int(budget * (.55 if reporting else .28)))
    changed = set()
    metadata = {}
    # Rebuild from the originals at every level; never silently re-interpret a
    # numeric metric, source ID or source excerpt as a statistical summary.
    for items, chars in ((16, 600), (8, 320), (4, 160), (2, 80)):
        metadata = {}
        for key in META_FIELDS:
            if key not in evidence:
                continue
            value, shortened = _bound(evidence[key], items=items, chars=chars, depth=12 if reporting and key == 'reporting_context' else 6)
            metadata[key] = value
            if shortened:
                changed.add(key)
        if estimate_tokens(metadata) <= target:
            return metadata, sorted(changed)
    for key in ('tag_context', 'research_context', 'reporting_context', 'scoring_context', 'inspected_features',
                'features', 'retrieval', 'caveats', 'summary'):
        if estimate_tokens(metadata) <= target:
            break
        if reporting and key == 'reporting_context':
            continue
        if key in metadata:
            del metadata[key]
            changed.add(key)
    if reporting and estimate_tokens(metadata) > max(target, int(budget * .65)):
        raise ValueError('Reporting context cannot preserve essential account evidence within budget; reduce accounts or increase budget')
    return metadata, sorted(changed)


def _author(source):
    value = next((source[key] for key in ('account_id', 'author_id', 'username') if source.get(key) is not None), None)
    return str(value) if value is not None else 'unknown:' + str(source.get('id', ''))


def _round_robin(sources):
    buckets = OrderedDict()
    for row in sources:
        buckets.setdefault(_author(row), []).append(row)
    result = []
    while buckets:
        for key in list(buckets):
            result.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return result


def _ordered(sources, mode):
    if mode in {'reporting', 'report', 'screening'}:
        return _round_robin(sources)
    if mode not in {'popular', 'compare', 'write', 'prompt', 'topics', 'briefing', 'news'}:
        return list(sources)
    groups = [[], [], []]
    for row in sources:
        index = 2 if row.get('kind') == 'news' else 1 if row.get('group') in {'comparison', 'control', 'counterexample'} else 0
        groups[index].append(row)
    groups = [_round_robin(group) for group in groups]
    ordered = []
    # News stays in the evidence from the first round; a large post cohort must
    # not consume the whole budget before any fact source can be considered.
    while any(groups):
        for group in groups:
            if group:
                ordered.append(group.pop(0))
    return ordered


def _fragment(source, max_tokens):
    text = source.get('text', '')
    if not isinstance(text, str) or not text:
        return None
    # Prefer complete paragraphs/sentences and the passage matching retrieval.
    spans = [(match.start(), match.end()) for match in re.finditer(r'[^\n.!?。！？]+(?:[.!?。！？]+|\n+|$)', text)]
    terms = [str(term).lower() for term in source.get('matched_terms', []) if term]
    best = None
    for start, end in spans:
        segment = text[start:end]
        if not segment.strip():
            continue
        row = deepcopy(source)
        row['text'] = segment
        row['text_truncated'] = True
        row['coverage'] = 'excerpt'
        original_start = source.get('text_excerpt_start', source.get('excerpt_start', 0))
        if not isinstance(original_start, int):
            original_start = 0
        row['excerpt_start'] = original_start + start
        row['text_excerpt_start'] = original_start + start
        row['excerpt_end'] = original_start + end
        row['excerpt_offset_unit'] = 'unicode_codepoints'
        row['text_length'] = source.get('text_length', len(text))
        score = sum(term in segment.lower() for term in terms)
        if estimate_tokens(row) <= max_tokens and (best is None or score > best[0]):
            best = (score, row)
            # Every term contributes at most one point. The first fitting
            # maximum already wins all later ties, including an empty term list.
            if score == len(terms):
                return row
    return best[1] if best else None


def _stamp(result):
    # Include this diagnostic itself in the estimate; JSON numbers may change
    # length, so converge rather than subtracting an approximate header cost.
    for _ in range(6):
        size = estimate_tokens(result)
        if result['context_pack']['estimated_tokens'] == size:
            return size
        result['context_pack']['estimated_tokens'] = size
    return estimate_tokens(result)


def pack_evidence(evidence, mode='popular', budget_tokens=None):
    """Return a fresh bounded JSON evidence object and explicit selection limits.

    The budget covers this entire object, including all metadata and diagnostics.
    It excludes task instructions, user requirements and model output. Sources
    returned here are exactly the sources that may be sent/cited by the caller.
    """
    if not isinstance(evidence, dict):
        raise ValueError('Evidence must be an object')
    budget = evidence_budget(budget_tokens)
    existing = evidence.get('context_pack', {})
    if (existing.get('method') == 'task_aware_whole_sources_v1'
            and existing.get('mode') == mode and existing.get('input_budget_tokens') == budget
            and estimate_tokens(evidence) <= budget):
        return deepcopy(evidence)
    raw = evidence.get('sources', [])
    if not isinstance(raw, list):
        raise ValueError('Evidence sources must be a list')
    metadata, changed = _metadata(evidence, budget, mode)
    result = {**metadata, 'sources': [], 'context_pack': {
        'method': 'task_aware_whole_sources_v1', 'mode': mode,
        'estimator': 'conservative_json_heuristic_not_provider_tokenizer',
        'estimated_tokens': 0, 'input_budget_tokens': budget,
        'selected_sources': 0, 'available_sources': len(raw), 'dropped_sources': len(raw),
        'text_shortened': False, 'shortened_sources': 0,
        'metadata_shortened_fields': changed, 'selected_groups': {}, 'selected_authors': 0,
        'drop_reasons': {}, 'statistics_basis': 'upstream_cohorts_not_packed_sources',
    }}
    if _stamp(result) > budget:
        raise ValueError('Evidence budget cannot fit selection diagnostics and metadata')
    seen, reasons = set(), Counter()
    ordered = _ordered([row for row in raw if isinstance(row, dict)], mode)
    reasons['invalid_source'] += len(raw) - len(ordered)
    news_count, post_count = 0, 0
    for original in ordered:
        source_id = original.get('id')
        if not isinstance(source_id, str) or not source_id or len(source_id) > 200:
            reasons['invalid_source'] += 1
            continue
        if source_id in seen:
            reasons['duplicate_id'] += 1
            continue
        seen.add(source_id)
        is_news = original.get('kind') == 'news'
        if mode in {'write', 'prompt', 'news'} and ((is_news and news_count >= 6) or (not is_news and post_count >= 10)):
            reasons['task_source_limit'] += 1
            continue
        row = deepcopy(original)
        # Do not cut every source to the same quota. Prefer complete short posts;
        # a long document gets a coherent passage with original character offsets.
        source_cap = max(450, min(2200, int(budget * .23)))
        if estimate_tokens(row) > source_cap:
            row = _fragment(row, source_cap)
            if row is None:
                reasons['no_complete_passage_fits'] += 1
                continue
        before = deepcopy(result['context_pack'])
        result['sources'].append(row)
        context = result['context_pack']
        context['selected_sources'] = len(result['sources'])
        context['dropped_sources'] = len(raw) - len(result['sources'])
        context['shortened_sources'] = sum(bool(item.get('text_truncated')) for item in result['sources'])
        context['text_shortened'] = bool(context['shortened_sources'])
        context['selected_groups'] = dict(Counter(str(item.get('group', item.get('kind', 'unknown'))) for item in result['sources']))
        context['selected_authors'] = len({_author(item) for item in result['sources'] if item.get('kind') != 'news'})
        # Keep a small reserve for the final reason counters, not hidden text.
        if _stamp(result) > budget - 80:
            result['sources'].pop()
            result['context_pack'] = before
            reasons['token_budget'] += 1
            continue
        news_count += int(is_news)
        post_count += int(not is_news)
    result['context_pack']['drop_reasons'] = {key: value for key, value in reasons.items() if value}
    if _stamp(result) > budget:
        raise ValueError('Evidence metadata exceeds its token budget')
    return result
