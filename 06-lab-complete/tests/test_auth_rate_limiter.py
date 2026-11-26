import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth, rate_limiter


class MemoryRedis:
    """Small atomic sorted-set stand-in for exercising the Redis script contract."""

    def __init__(self, shared=None):
        self.entries = shared if shared is not None else {}
        self.lock = threading.Lock()
        self.keys = []

    def eval(self, script, numkeys, key, now, cutoff, limit, member, window):
        self.keys.append(key)
        with self.lock:
            entries = self.entries.setdefault(key, {})
            for old_member, score in list(entries.items()):
                if score <= float(cutoff):
                    del entries[old_member]
            if len(entries) >= int(limit):
                oldest = min(entries.values())
                retry_ms = max(1, int(float(window) - (float(now) - oldest)))
                return [0, retry_ms]
            entries[member] = float(now)
            return [1, 0]


def test_api_key_dependency_returns_401_for_missing_or_wrong(monkeypatch):
    monkeypatch.setattr(auth, "settings", SimpleNamespace(agent_api_key="correct-key"), raising=False)

    for value in (None, "wrong-key"):
        with pytest.raises(HTTPException) as exc:
            auth.verify_api_key(value)
        assert exc.value.status_code == 401


def test_api_key_dependency_returns_valid_key(monkeypatch):
    monkeypatch.setattr(auth, "settings", SimpleNamespace(agent_api_key="correct-key"), raising=False)

    assert auth.verify_api_key("correct-key") == "correct-key"


def test_rate_limit_rejects_over_limit_with_retry_after(monkeypatch):
    monkeypatch.setattr(rate_limiter, "settings", SimpleNamespace(rate_limit_per_minute=2), raising=False)
    client = MemoryRedis()
    monkeypatch.setattr(rate_limiter, "r", client, raising=False)

    rate_limiter.check_rate_limit("customer/key")
    rate_limiter.check_rate_limit("customer/key")
    with pytest.raises(HTTPException) as exc:
        rate_limiter.check_rate_limit("customer/key")

    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) > 0
    assert "customer/key" not in client.keys


def test_rate_limit_is_per_key_and_survives_client_recreation(monkeypatch):
    monkeypatch.setattr(rate_limiter, "settings", SimpleNamespace(rate_limit_per_minute=1), raising=False)
    shared = {}
    first_client = MemoryRedis(shared)
    monkeypatch.setattr(rate_limiter, "r", first_client, raising=False)
    rate_limiter.check_rate_limit("key-one")

    second_client = MemoryRedis(shared)
    monkeypatch.setattr(rate_limiter, "r", second_client, raising=False)
    rate_limiter.check_rate_limit("key-two")
    with pytest.raises(HTTPException) as exc:
        rate_limiter.check_rate_limit("key-one")

    assert exc.value.status_code == 429


def test_concurrent_requests_cannot_exceed_limit(monkeypatch):
    monkeypatch.setattr(rate_limiter, "settings", SimpleNamespace(rate_limit_per_minute=3), raising=False)
    monkeypatch.setattr(rate_limiter, "r", MemoryRedis(), raising=False)

    def attempt(_):
        try:
            rate_limiter.check_rate_limit("shared-key")
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        allowed = list(pool.map(attempt, range(12)))

    assert sum(allowed) == 3
