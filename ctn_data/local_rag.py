"""Local, permission-scoped hybrid retrieval for external-AI evidence packs.

The caller supplies its already-authorized sources. This module never fetches
records. Embedding and reranking backends are explicitly injected by the caller. No
network or model loading occurs when they are absent.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import unicodedata


_STOP = frozenset('a an and are as at be by for from in is it of on or that the this to was what which with'.split())
_VECTOR_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_MAX_MEMORY_VECTORS = 128
_CANDIDATES = 80


def tokenize(text):
    """Unicode words plus Chinese/Japanese/Korean bigrams, without a service."""
    value = unicodedata.normalize('NFKC', str(text or '')).casefold()
    cjk = r'[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+'
    tokens = []
    for run in re.findall(cjk, value):
        tokens.extend([run] if len(run) == 1 else (run[i:i + 2] for i in range(len(run) - 1)))
    for word in re.findall(r'[^\W_]+', re.sub(cjk, ' ', value), re.UNICODE):
        if word not in _STOP:
            tokens.append(word[:100])
    return tokens


def bm25_scores(documents, query):
    """BM25 with document-frequency IDF; scores align with documents."""
    terms = Counter(tokenize(query))
    corpus = [Counter(tokenize(text)) for text in documents]
    if not corpus or not terms:
        return [0.0] * len(corpus)
    lengths = [sum(doc.values()) for doc in corpus]
    average = sum(lengths) / len(corpus) or 1.0
    frequency = Counter(term for doc in corpus for term in doc if term in terms)
    scores = []
    for doc, length in zip(corpus, lengths):
        score = 0.0
        for term in terms:
            count = doc.get(term, 0)
            if count:
                inverse = math.log(1 + (len(corpus) - frequency[term] + .5) / (frequency[term] + .5))
                score += inverse * count * 2.2 / (count + 1.2 * (.25 + .75 * length / average))
        scores.append(score)
    return scores


def reciprocal_rank_fusion(rankings, constant=60):
    result = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            result[key] = result.get(key, 0.0) + 1.0 / (constant + rank)
    return result


def _chunks(text, max_chars=600, overlap=80):
    """Bound cloud retrieval passages, retaining exact offsets into the unmodified body."""
    if not text:
        return [('', 0, 0)]
    result, start = [], 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = max(text.rfind(mark, start + max_chars // 2, end) for mark in ('\n', '. ', '。', '! ', '? '))
            if boundary >= 0:
                end = boundary + 1
        result.append((text[start:end], start, end))
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def _normalize_vector(vector):
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not values or not math.isfinite(norm) or not norm:
        raise ValueError('invalid embedding vector')
    return [value / norm for value in values]


def _cached_vectors(backend, texts, cache_path):
    """Cache vectors only, keyed by model configuration and exact content hash."""
    signature = str(getattr(backend, 'cache_key', '') or '')
    namespace = hashlib.sha256((signature or 'instance:' + str(id(backend))).encode()).hexdigest()
    keys = [(namespace, hashlib.sha256(text.encode()).hexdigest()) for text in texts]
    values, hits = {}, 0
    connection = None
    cache_status = 'memory'
    if signature and cache_path is not False:
        configured = cache_path or os.environ.get('CTN_RAG_CACHE_PATH')
        target = Path(configured) if configured else Path(__file__).resolve().parent.parent / 'data/rag/embeddings.sqlite3'
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(target, timeout=2)
            connection.execute('CREATE TABLE IF NOT EXISTS embeddings (model TEXT, digest TEXT, vector TEXT NOT NULL, PRIMARY KEY(model,digest))')
            cache_status = 'disk'
        except (OSError, sqlite3.Error):
            if connection:
                connection.close()
            connection = None
            cache_status = 'memory_disk_unavailable'
    try:
        for key in dict.fromkeys(keys):
            with _CACHE_LOCK:
                cached = _VECTOR_CACHE.get(key) if signature else None
            if cached is None and connection:
                try:
                    row = connection.execute('SELECT vector FROM embeddings WHERE model=? AND digest=?', key).fetchone()
                    cached = _normalize_vector(json.loads(row[0])) if row else None
                except (sqlite3.Error, ValueError, TypeError):
                    cached = None
            if cached is not None:
                values[key] = cached
                hits += 1
        missing = list(dict.fromkeys(key for key in keys if key not in values))
        text_by_key = dict(zip(keys, texts))
        for offset in range(0, len(missing), 32):
            batch = missing[offset:offset + 32]
            encoded = backend.encode([text_by_key[key] for key in batch], is_query=False)
            if len(encoded) != len(batch):
                raise ValueError('embedding count mismatch')
            for key, vector in zip(batch, encoded):
                values[key] = _normalize_vector(vector)
                if connection:
                    try:
                        connection.execute('INSERT OR REPLACE INTO embeddings VALUES(?,?,?)', (*key, json.dumps(values[key])))
                    except sqlite3.Error:
                        cache_status = 'memory_disk_unavailable'
        if connection:
            try:
                connection.commit()
            except sqlite3.Error:
                cache_status = 'memory_disk_unavailable'
        with _CACHE_LOCK:
            for key, value in (values.items() if signature else []):
                _VECTOR_CACHE[key] = value
                _VECTOR_CACHE.move_to_end(key)
            while len(_VECTOR_CACHE) > _MAX_MEMORY_VECTORS:
                _VECTOR_CACHE.popitem(last=False)
        return [values[key] for key in keys], hits, cache_status
    finally:
        if connection:
            connection.close()


def _fingerprint(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def rank_sources(sources, query, limit=20, diversify=True, *, embedding_backend=None,
                 reranker_backend=None, cache_path=None):
    """Return ranked authorized source copies and a truthful retrieval report.

    Embedding backend: ``encode(texts, *, is_query=False) -> vectors`` and a
    non-secret ``cache_key`` identifying endpoint/model/version/dimensions.
    Reranker backend: ``predict([(query, passage), ...]) -> scores``.
    Neither backend is loaded or contacted unless explicitly supplied. Candidate
    semantic coverage is capped at 200 chunks and reported, never called global.
    ``_index_text`` can carry the complete body; it is removed from every output.
    """
    rows = [deepcopy(source) for source in sources]
    limit = max(0, min(int(limit), len(rows)))
    info = {'mode': 'lexical_only', 'embedding_status': 'not_configured',
            'reranker_status': 'not_configured', 'total_sources': len(rows),
            'chunk_count': 0, 'semantic_chunks': 0, 'cache_hits': 0,
            'cache_status': 'unused', 'diversified': bool(diversify)}
    if not rows or not limit:
        return [], info
    chunks, documents, fingerprints, duplicates = [], [], [], {}
    for index, row in enumerate(rows):
        body = str(row.get('_index_text') or row.get('text') or '')
        fingerprint = _fingerprint(body) or 'empty:' + str(row.get('id', index))
        fingerprints.append(fingerprint)
        duplicates.setdefault(fingerprint, []).append(index)
        title = str(row.get('title') or '')[:160]
        for text, start, end in _chunks(body):
            chunks.append((index, text, start, end, len(body)))
            documents.append((title + '\n' + text).strip())
    info['chunk_count'] = len(chunks)
    lexical = bm25_scores(documents, query)
    lexical_order = sorted(range(len(chunks)), key=lambda index: (-lexical[index], index))
    def balanced(order):
        counts, result = Counter(), []
        for index in order:
            source = chunks[index][0]
            if counts[source] < 2:
                result.append(index)
                counts[source] += 1
            if len(result) >= _CANDIDATES:
                break
        return result
    lexical_hits = balanced(index for index in lexical_order if lexical[index] > 0)
    rankings = [lexical_hits]
    semantic = {}
    if embedding_backend is not None and str(query).strip() and any(document.strip() for document in documents):
        try:
            # Mix lexical candidates with distributed coverage when a scope is large.
            if len(chunks) <= 200:
                selected = list(range(len(chunks)))
            else:
                selected = list(dict.fromkeys(lexical_hits + [min(len(chunks) - 1, int(i * len(chunks) / 120)) for i in range(120)]))[:200]
            selected = [index for index in selected if documents[index].strip()]
            vectors, hits, cache_status = _cached_vectors(embedding_backend, [documents[index] for index in selected], cache_path)
            query_vectors = embedding_backend.encode([str(query)], is_query=True)
            if len(query_vectors) != 1:
                raise ValueError('query embedding count mismatch')
            query_vector = _normalize_vector(query_vectors[0])
            for index, vector in zip(selected, vectors):
                if len(vector) != len(query_vector):
                    raise ValueError('embedding dimension mismatch')
                semantic[index] = sum(a * b for a, b in zip(vector, query_vector))
            rankings.append(balanced(sorted(semantic, key=lambda index: (-semantic[index], index))))
            info.update(mode='hybrid', embedding_status='ready', semantic_chunks=len(selected),
                        cache_hits=hits, cache_status=cache_status)
        except Exception as exc:
            from .ai_providers import ProviderError
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError('向量检索失败，请检查检索连接和模型配置', 502) from None
    fused = reciprocal_rank_fusion(rankings)
    # An empty query or no lexical match still returns a disclosed representative sample.
    if not fused:
        fused = {index: 1 / (60 + rank) for rank, index in enumerate(lexical_order, 1)}
        info['no_query_match'] = True
    candidate_order = balanced(sorted(fused, key=lambda index: (-fused[index], index)))
    reranked = {}
    if reranker_backend is not None and str(query).strip():
        try:
            values = reranker_backend.predict([(str(query), documents[index]) for index in candidate_order])
            if len(values) != len(candidate_order):
                raise ValueError('reranker count mismatch')
            reranked = {index: float(value) for index, value in zip(candidate_order, values)}
            if not all(math.isfinite(value) for value in reranked.values()):
                raise ValueError('invalid reranker score')
            candidate_order.sort(key=lambda index: (-reranked[index], -fused[index], index))
            info['reranker_status'] = 'ready'
        except Exception as exc:
            from .ai_providers import ProviderError
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError('云端重排失败，请检查检索连接和模型配置', 502) from None
    # Keep one best passage per source; repeated messages from other accounts are
    # separate evidence and must remain available, especially in reporting mode.
    best = {}
    for chunk_index in candidate_order:
        best.setdefault(chunks[chunk_index][0], chunk_index)
    pending = list(best)
    chosen, authors, groups, seen_bodies = [], Counter(), Counter(), Counter()
    positions = {index: rank for rank, index in enumerate(pending)}
    while pending and len(chosen) < limit:
        def selection_score(source_index):
            value = 1 / (1 + positions[source_index] / 60)
            if diversify:
                row = rows[source_index]
                author = row.get('author_id')
                if author is not None:
                    value -= .10 * authors[str(author)]
                group = row.get('group')
                if group:
                    value -= .025 * groups[str(group)]
                value -= .40 * seen_bodies[fingerprints[source_index]]
            return value
        source_index = max(pending, key=selection_score)
        pending.remove(source_index)
        chosen.append(source_index)
        authors[str(rows[source_index].get('author_id'))] += 1
        groups[str(rows[source_index].get('group'))] += 1
        seen_bodies[fingerprints[source_index]] += 1
    result = []
    for source_index in chosen:
        row = rows[source_index]
        chunk_index = best[source_index]
        _, text, start, end, length = chunks[chunk_index]
        base_offset = 0 if row.get('_index_text') else int(row.get('text_excerpt_start', row.get('excerpt_start', 0)) or 0)
        row.pop('_index_text', None)
        row['text'] = text
        row['text_excerpt_start'] = base_offset + start
        row['excerpt_start'] = base_offset + start
        row['excerpt_end'] = base_offset + end
        row['excerpt_offset_unit'] = 'unicode_codepoints'
        row['text_length'] = max(int(row.get('text_length') or 0), length)
        row['text_truncated'] = bool(row.get('text_truncated') or start > 0 or end < length)
        row['retrieval'] = {'bm25': round(lexical[chunk_index], 6),
                            'semantic': round(semantic[chunk_index], 6) if chunk_index in semantic else None,
                            'rrf': round(fused[chunk_index], 8),
                            'rerank': reranked.get(chunk_index),
                            'chunk_start': base_offset + start, 'chunk_end': base_offset + end}
        duplicate_indices = duplicates[fingerprints[source_index]]
        row['duplicate_count'] = len(duplicate_indices)
        if len(duplicate_indices) > 1:
            # Bound payload duplication but disclose omitted provenance explicitly.
            row['duplicate_sources'] = [{key: rows[index].get(key) for key in
                                         ('id', 'author_id', 'created_at', 'group')}
                                        for index in duplicate_indices[:32]]
            row['duplicate_sources_omitted'] = max(0, len(duplicate_indices) - 32)
        result.append(row)
    info['returned_sources'] = len(result)
    info['candidate_chunks'] = len(candidate_order)
    info['semantic_coverage_complete'] = info['semantic_chunks'] == len(chunks)
    return result, info
