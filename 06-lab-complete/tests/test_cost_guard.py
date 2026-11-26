from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import cost_guard


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, numkeys, key, *args):
        with self.lock:
            current = float(self.values.get(key, 0.0))
            if "reserve_daily_budget" in script:
                amount, budget = float(args[0]), float(args[1])
                if current + amount > budget:
                    return 0
                self.values[key] = current + amount
                return 1
            if "settle_daily_budget" in script:
                reserved, actual = float(args[0]), float(args[1])
                self.values[key] = current - reserved + actual
                return self.values[key]
            if "release_daily_budget" in script:
                self.values[key] = current - float(args[0])
                return self.values[key]
            value = current + float(args[0])
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

    assert cost_guard.check_budget() > 0


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


def test_reservation_is_settled_to_actual_cost(monkeypatch):
    client = setup_guard(monkeypatch)
    reserved = cost_guard.check_budget(input_tokens=10, output_tokens=256)

    actual = cost_guard.record_cost(input_tokens=10, output_tokens=5, reserved_cost=reserved)

    assert actual < reserved
    assert client.get(cost_guard._daily_key(datetime.now(timezone.utc))) == pytest.approx(actual)


def test_concurrent_budget_reservations_never_exceed_daily_budget(monkeypatch):
    budget = 0.001
    client = setup_guard(monkeypatch, budget=budget)

    def reserve(_):
        try:
            return cost_guard.check_budget(input_tokens=0, output_tokens=256)
        except HTTPException as exc:
            assert exc.status_code == 402
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        reservations = list(pool.map(reserve, range(20)))

    accepted = [value for value in reservations if value is not None]
    total_reserved = sum(accepted)
    stored = float(client.get(cost_guard._daily_key(datetime.now(timezone.utc))))
    assert accepted
    assert total_reserved <= budget
    assert stored == pytest.approx(total_reserved)
