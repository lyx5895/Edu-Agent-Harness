"""Re-run a stale computation without repeating model calls.

A snapshot that loses the commit race is recomputed against fresh data. Model
calls whose inputs are unchanged return the recorded result; calls whose inputs
changed (because the conflicting write was relevant) go to the model again.
"""

import json
import time
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

from harness.persistence.storage import StaleSnapshot, check_model_call

# The first run is optimistic; reruns hold the write lock, so a second rerun is a safety margin only.
ATTEMPTS = 3
_active = ContextVar('model_replay', default=None)


class Replay:
    def __init__(self):
        self.started = time.time()
        self.attempt = 0
        self.results = {}


def started():
    replay = _active.get()
    return replay.started if replay else time.time()


def rerun():
    replay = _active.get()
    return bool(replay and replay.attempt)


def retry_stale(method):
    @wraps(method)
    def run(*args, **kwargs):
        if _active.get() is not None:
            return method(*args, **kwargs)
        replay = Replay()
        token = _active.set(replay)
        try:
            for attempt in range(ATTEMPTS):
                replay.attempt = attempt
                try:
                    return method(*args, **kwargs)
                except StaleSnapshot:
                    if attempt == ATTEMPTS - 1:
                        raise
        finally:
            _active.reset(token)

    return run


class ReplayableModel:
    """Transparent proxy; attribute writes go to the wrapped model."""

    UNCACHED = frozenset({'budget', 'budget_left'})

    def __init__(self, model, namespace=''):
        object.__setattr__(self, '_model', model)
        object.__setattr__(self, '_namespace', namespace)

    def __getattr__(self, name):
        attr = getattr(self._model, name)
        if name.startswith('_') or name in self.UNCACHED or not callable(attr):
            return attr

        def call(*args, **kwargs):
            replay = _active.get()
            if replay is None:
                check_model_call()
                return attr(*args, **kwargs)
            key = json.dumps([self._namespace, name, args, kwargs], sort_keys=True, ensure_ascii=False, default=repr)
            if key in replay.results:
                return deepcopy(replay.results[key])
            check_model_call()
            result = attr(*args, **kwargs)
            replay.results[key] = deepcopy(result)
            return result

        return call

    def __setattr__(self, name, value):
        setattr(self._model, name, value)
