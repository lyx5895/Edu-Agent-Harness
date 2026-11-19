"""Model gateway client (OpenAI-compatible) with schema validation and one repair round.

Design doc 8.3: the gateway's constrained decoding is not trusted. Every structured reply is parsed
and validated with pydantic here; an invalid reply is sent back once for repair, then the call fails
with :class:`ModelUnavailable` (never a silently degraded result). Each call returns a metadata
record (requested and returned model, usage, latency) that the workflow stores as a generation.
"""

from __future__ import annotations

import json
import random
import re
import time
from copy import deepcopy
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError


class ModelUnavailable(RuntimeError):
    """The model could not produce a valid result; the caller should retry later or ask a teacher."""


class LLM(Protocol):
    def complete(
        self, role: str, task: str, system: str, user: str, schema: type[BaseModel]
    ) -> tuple[BaseModel, dict]: ...

    def model_for(self, role: str) -> str: ...


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema rewritten for strict json_schema decoding: inline $refs, no extra keys."""
    schema = model.model_json_schema()
    defs = schema.pop('$defs', {})

    def fix(node):
        if isinstance(node, dict):
            if '$ref' in node:
                return fix(deepcopy(defs[node['$ref'].split('/')[-1]]))
            out = {}
            for k, v in node.items():
                if k in ('title', 'default', 'description'):
                    continue  # schema metadata, not data
                if k in ('exclusiveMinimum', 'exclusiveMaximum'):
                    # Gemini rejects these keywords ("Unknown name exclusiveMinimum"); the bound is
                    # inclusive there, and pydantic re-validates the reply anyway.
                    out[k.replace('exclusive', '').lower()] = v
                elif k == 'properties':
                    out[k] = {name: fix(sub) for name, sub in v.items()}  # property names are data: keep all
                else:
                    out[k] = fix(v)
            if out.get('type') == 'object' and 'properties' in out:
                out['additionalProperties'] = False
                out['required'] = list(out['properties'])
            return out
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(schema)


def parse_json(raw: str):
    """First JSON value in a reply; tolerates code fences and trailing whitespace runs."""
    text = (raw or '').strip()
    fence = re.match(r'^```(?:json)?\s*(.*?)```\s*$', text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text:
        raise ValueError('空输出')
    value, _ = json.JSONDecoder().raw_decode(text)
    return value


def redact_images(messages):
    """Copy of chat messages with inline images replaced by a short note (photos are stored on disk)."""
    out = []
    for m in messages:
        content = m.get('content')
        if isinstance(content, list):
            content = [
                {'type': 'text', 'text': f'[图片，{len(part["image_url"]["url"])} 字符，已省略]'}
                if part.get('type') == 'image_url'
                else part
                for part in content
            ]
        out.append({**m, 'content': content})
    return out


class Gateway:
    def __init__(self, base_url, api_key, models, *, timeout=180.0, max_tokens=6000, client=None, retries=3):
        if not api_key and client is None:
            raise ModelUnavailable('未配置 EDU_LLM_API_KEY，无法调用模型')
        self.base_url = base_url.rstrip('/')
        self.models = dict(models)
        self.max_tokens = max_tokens
        self.retries = retries
        self.client = client or httpx.Client(
            timeout=timeout, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        )

    def model_for(self, role):
        return self.models[role]

    def _post(self, body):
        last = None
        for attempt in range(self.retries):
            try:
                response = self.client.post(self.base_url + '/chat/completions', json=body)
                if response.status_code == 429 or response.status_code >= 500:
                    last = f'HTTP {response.status_code}: {response.text[:200]}'
                else:
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as e:
                raise ModelUnavailable(f'模型请求被拒绝：HTTP {e.response.status_code} {e.response.text[:200]}') from e
            except httpx.TransportError as e:
                last = repr(e)
            time.sleep(min(2**attempt + random.random(), 20))
        raise ModelUnavailable(f'模型服务暂不可用：{last}')

    def complete(self, role, task, system, user, schema):
        model = self.models[role]
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        response_format = {
            'type': 'json_schema',
            'json_schema': {'name': schema.__name__, 'strict': True, 'schema': strict_schema(schema)},
        }
        started = time.time()
        attempts, usage, returned, raw, error = [], {}, None, '', None
        for round_ in range(2):
            body = {
                'model': model,
                'messages': messages,
                'max_tokens': self.max_tokens,
                'temperature': 0,
                'response_format': response_format,
            }
            data = self._post(body)
            choice = (data.get('choices') or [{}])[0]
            raw = (choice.get('message') or {}).get('content') or ''
            returned = data.get('model')
            for key, value in (data.get('usage') or {}).items():
                if isinstance(value, int | float):
                    usage[key] = usage.get(key, 0) + value
            attempts.append({'finish_reason': choice.get('finish_reason'), 'model_returned': returned})
            try:
                if choice.get('finish_reason') == 'length':
                    raise ValueError('输出达到长度上限被截断')
                result = schema.model_validate(parse_json(raw))
                meta = {
                    'task': task,
                    'role': role,
                    'model_requested': model,
                    'model_returned': returned,
                    'usage': usage,
                    'latency_ms': int((time.time() - started) * 1000),
                    'attempts': attempts,
                    'request': redact_images(messages),
                    'response': raw,
                    'repaired': round_ > 0,
                }
                return result, meta
            except (ValueError, ValidationError, json.JSONDecodeError) as e:
                error = str(e)[:500]
                messages = [
                    *messages,
                    {'role': 'assistant', 'content': raw[:4000]},
                    {
                        'role': 'user',
                        'content': f'上次输出不符合要求：{error}\n请只输出一个符合 JSON schema 的 JSON 对象。',
                    },
                ]
        raise ModelUnavailable(f'模型输出两次都不符合格式（{task}）：{error}')
