"""Bounded cloud embedding/rerank adapters; no local model dependencies.

Contracts: docs.siliconflow.cn/docs/api/{embeddings-post,rerank-post}.
Only callers with an explicit user action should instantiate these adapters.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import socket
from urllib.parse import urlsplit

from . import ai_providers as api


class _Cloud:
    protocol = ''
    endpoint = ''

    def __init__(self, provider, *, cancellation_event=None, on_progress=None):
        if provider.get('protocol') != self.protocol:
            raise ValueError('检索连接类型不匹配，请分别选择向量化和重排服务')
        self.base_url = api._validate_url(provider.get('base_url'))
        self.model = api._text(provider.get('model'), '模型名称', 200)
        self._key = api._validate_key(provider.get('api_key', ''))
        if not self._key and not api._loopback_host(urlsplit(self.base_url).hostname):
            raise api.ProviderError('检索服务尚未配置 API Key', 400)
        self.cache_key = hashlib.sha256((self.protocol + '\n' + self.base_url + '\n' + self.model + '\nv1').encode()).hexdigest()
        self.calls = 0
        self.cancellation_event = cancellation_event
        self.on_progress = on_progress

    def _check_cancelled(self):
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            raise api.ProviderError('云端检索任务已取消', 499)

    def _post(self, payload):
        self._check_cancelled()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if self._key:
            headers['Authorization'] = 'Bearer ' + self._key
        try:
            self.calls += 1
            options = ({'cancellation_event': self.cancellation_event}
                       if self.cancellation_event is not None else {})
            result = api._request_json(self.base_url + self.endpoint,
                json.dumps({'model': self.model, **payload}, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                headers, wait_for_completion=True, on_progress=self.on_progress, **options)
            self._check_cancelled()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except api.ProviderError:
            raise
        except api._TransportCancelled:
            raise api.ProviderError('云端检索任务已取消', 499) from None
        except (TimeoutError, socket.timeout):
            raise api.ProviderError('云端检索服务响应超时，请重试或检查连接', 504) from None
        except (OSError, http.client.HTTPException):
            raise api.ProviderError('无法连接云端检索服务，请检查网络和 API 地址', 502) from None
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise api.ProviderError('云端检索服务返回了无法识别的响应', 502) from None


def _texts(texts, maximum=200):
    if not isinstance(texts, (list, tuple)) or not 1 <= len(texts) <= maximum:
        raise ValueError('云端检索每次最多处理 200 段资料')
    if any(not isinstance(x, str) or not x.strip() or len(x) > 8000 for x in texts):
        raise ValueError('云端检索资料须为非空文本，每段不超过 8000 字')
    return list(texts)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class CloudEmbedding(_Cloud):
    protocol = 'openai_embeddings'
    endpoint = '/embeddings'

    def encode(self, texts, *, is_query=False):
        # No universal E5 prefixes: input conventions are model-specific.
        texts = _texts(texts)
        vectors, dimension = [], None
        for start in range(0, len(texts), 16):
            batch = texts[start:start + 16]
            response = self._post({'input': batch, 'encoding_format': 'float'})
            try:
                rows = response['data']
                if not isinstance(rows, list) or len(rows) != len(batch):
                    raise ValueError
                indexed = {}
                for row in rows:
                    index, vector = row['index'], row['embedding']
                    if type(index) is not int or not 0 <= index < len(batch) or index in indexed:
                        raise ValueError
                    if not isinstance(vector, list) or not 1 <= len(vector) <= 8192 or not all(_finite(x) for x in vector):
                        raise ValueError
                    dimension = len(vector) if dimension is None else dimension
                    if len(vector) != dimension or not any(vector):
                        raise ValueError
                    indexed[index] = [float(x) for x in vector]
                vectors.extend(indexed[i] for i in range(len(batch)))
            except (ValueError, TypeError, KeyError, IndexError):
                raise api.ProviderError('向量服务响应缺少有效的等维向量，请检查接口和模型', 502) from None
        return vectors


class CloudReranker(_Cloud):
    protocol = 'rerank'
    endpoint = '/rerank'

    def predict(self, pairs):
        if not isinstance(pairs, (list, tuple)) or not pairs:
            return []
        try:
            query = pairs[0][0]
            if not isinstance(query, str) or not query.strip() or len(query) > 8000:
                raise ValueError
            if any(len(pair) != 2 or pair[0] != query for pair in pairs):
                raise ValueError
            documents = _texts([pair[1] for pair in pairs])
        except (ValueError, TypeError, IndexError):
            raise ValueError('重排请求须使用同一个问题及有效候选文本') from None
        response = self._post({'query': query, 'documents': documents,
                               'top_n': len(documents), 'return_documents': False})
        try:
            rows = response['results']
            if not isinstance(rows, list) or len(rows) != len(documents):
                raise ValueError
            scores = {}
            for row in rows:
                index, score = row['index'], row['relevance_score']
                if type(index) is not int or not 0 <= index < len(documents) or index in scores or not _finite(score):
                    raise ValueError
                scores[index] = float(score)
            return [scores[i] for i in range(len(documents))]
        except (ValueError, TypeError, KeyError, IndexError):
            raise api.ProviderError('重排服务响应缺少候选索引或有效分数，请检查接口', 502) from None
