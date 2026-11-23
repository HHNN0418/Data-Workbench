"""Bounded, local topic suggestions for writing; tags are never action sources."""
from __future__ import annotations

import re

from .social_topics import read_social_context, topical_context

MAX_TAGS = 8
MAX_TAG_CANDIDATES = 5000
_COMMON = {'about', 'actually', 'afford', 'and', 'approach', 'are', 'from', 'have',
           'into', 'more', 'that', 'their', 'them', 'there', 'these', 'they', 'this',
           'with', 'will', 'would', 'your'}
# These phrases name the same visible topic or slogan. Bare "vaccination"
# deliberately does not imply support for #yes2vax, and aliases never create
# tags absent from the collected catalogue.
TAG_ALIASES = {
    'protectkingstonia': ('protect kingstonia', 'protecting kingstonia'),
    'kingstonia': ('kingstonia',),
    'keepkingstoniaopen': ('keep kingstonia open', 'keeping kingstonia open'),
    'yes2vax': ('yes to vaccines', 'yes to vaccination', 'support vaccination', 'pro vaccination'),
    'h2d2': ('h2d2',),
    'voteno': ('vote no',),
    'choicenotcoercion': ('choice not coercion',),
    'crownharbour': ('crown harbour',),
    'referendum': ('referendum',),
    'proofkeepsusopen': ('proof keeps us open',),
    'voteyes': ('vote yes',),
    'h2d2no': ('h2d2 no', 'no to h2d2'),
    'healthchoices': ('health choices',),
    'freedom': ('freedom',),
    'workersrights': ('workers rights', 'worker rights'),
}


def _alias_match(name, text):
    for alias in TAG_ALIASES.get(name, ()):
        pattern = r'(?<!\w)' + r'[\W_]+'.join(re.escape(word) for word in alias.split()) + r'(?!\w)'
        if re.search(pattern, text):
            return alias
    return None


def tag_context(conn, *, sources=(), interests=(), query='', limit=MAX_TAGS):
    """Choose collected names by textual relevance, never by popularity alone.

    All-tag pages remain local. Only at most eight matched records, their
    observed counts/timestamps, and snapshot dates can enter a model context.
    A refreshed popularity snapshot does not change source versions or create
    a reason to publish.
    """
    limit = max(0, min(MAX_TAGS, int(limit)))
    snapshot = topical_context(conn, limit=50)
    candidates = {}
    for offset in range(0, MAX_TAG_CANDIDATES, 500):
        page = read_social_context(conn, limit=500, offset=offset)
        for item in page.get('items', []):
            if isinstance(item, dict) and isinstance(item.get('name'), str):
                candidates[item['name'].lstrip('#').casefold()] = dict(item)
        if offset + 500 >= page.get('total', 0):
            break
    for item in snapshot.get('tags', []):
        if isinstance(item, dict) and isinstance(item.get('name'), str):
            key = item['name'].lstrip('#').casefold()
            candidates[key] = {**candidates.get(key, {}), **item}
    source_text = ' '.join(str(row.get(key, ''))[:4000]
                           for row in list(sources)[:200] if isinstance(row, dict)
                           for key in ('title', 'text'))[:60000]
    interest_text = ' '.join(str(value)[:200] for value in list(interests)[:30])
    query = query[:12000] if isinstance(query, str) else ''
    content = (query + ' ' + source_text).casefold()
    interests_text = interest_text.casefold()
    hashtags = set(re.findall(r'(?<!\w)#(\w+)', content, re.UNICODE))
    selected = []
    for name, item in candidates.items():
        if not name or len(name) > 100:
            continue
        score, basis, alias = 0, None, None
        if name in hashtags:
            score, basis = 4, 'source_hashtag'
        elif len(name) >= 4 and name not in _COMMON:
            pattern = r'(?<!\w)' + re.escape(name) + r'(?!\w)'
            if re.search(pattern, content):
                score, basis = 2, 'source_or_query_topic'
            elif re.search(pattern, interests_text):
                score, basis = 1, 'persona_interest'
        if score < 2:
            alias = _alias_match(name, content)
            if alias:
                score, basis = 2, 'source_or_query_alias'
        if not score:
            alias = _alias_match(name, interests_text)
            if alias:
                score, basis = 1, 'persona_interest_alias'
        if score:
            selected.append({key: item.get(key) for key in
                             ('tag_id', 'name', 'post_count', 'popular_rank', 'observed_at')}
                            | {'popular_observed_at': item.get('popular_observed_at') or
                               (item.get('observed_at') if type(item.get('popular_rank')) is int else None),
                               'match_basis': basis, 'matched_alias': alias, '_score': score})
    selected.sort(key=lambda row: (-row['_score'],
                                  row['popular_rank'] if type(row.get('popular_rank')) is int else 1000000,
                                  row['name'].casefold()))
    rows = [{key: value for key, value in row.items() if key != '_score'} for row in selected[:limit]]
    return {'tags': rows, 'observed_at': snapshot.get('observed_at'),
            'popular_observed_at': snapshot.get('popular_observed_at'),
            'all_tags_completed_at': snapshot.get('all_tags_completed_at'),
            'selection_basis': 'collected_names_matching_sources_query_or_interests',
            'popularity_is_factual_evidence': False}
