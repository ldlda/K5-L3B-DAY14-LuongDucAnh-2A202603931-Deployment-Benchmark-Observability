from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import cost_guard


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, numkeys, key, amount, ttl_seconds):
        value = float(self.values.get(key, 0.0)) + float(amount)
        self.values[key] = value
        return value


def setup_guard(monkeypatch, budget=1.0):
    client = MemoryRedis()
    monkeypatch.setattr(cost_guard, "r", client, raising=False)
    monkeypatch.setattr(cost_guard, "settings", SimpleNamespace(daily_budget_usd=budget), raising=False)
    return client


def test_record_cost_calculates_and_accumulates_input_and_output(monkeypatch):
    client = setup_guard(monkeypatch)

    cost = cost_guard.record_cost(input_tokens=1000, output_tokens=500)

    assert cost == pytest.approx(0.00045)
    today_key = cost_guard._daily_key(datetime.now(timezone.utc))
    assert client.get(today_key) == pytest.approx(cost)


def test_check_budget_allows_usage_below_limit(monkeypatch):
    client = setup_guard(monkeypatch, budget=1.0)
    client.values[cost_guard._daily_key(datetime.now(timezone.utc))] = "0.5"

    assert cost_guard.check_budget() is None


@pytest.mark.parametrize("spent", ["1.0", "1.01"])
def test_check_budget_rejects_at_or_over_limit(monkeypatch, spent):
    client = setup_guard(monkeypatch, budget=1.0)
    client.values[cost_guard._daily_key(datetime.now(timezone.utc))] = spent

    with pytest.raises(HTTPException) as exc:
        cost_guard.check_budget()

    assert exc.value.status_code == 402


def test_daily_budget_key_changes_at_utc_midnight():
    before = datetime(2026, 11, 26, 23, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc)

    assert cost_guard._daily_key(before) != cost_guard._daily_key(after)
