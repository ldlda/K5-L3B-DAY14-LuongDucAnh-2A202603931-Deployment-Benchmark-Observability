from concurrent.futures import ThreadPoolExecutor
import math
import threading

import pytest
from fastapi.testclient import TestClient

from cost_guard import CostGuard
from app import app as api_app
import app as api_module


class MemoryRedis:
    """Small thread-safe Redis double for the cost-guard behavior tests."""

    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def get(self, key):
        return self.values.get(key)

    def eval(self, script, numkeys, key, *args):
        with self.lock:
            current = float(self.values.get(key) or 0.0)
            if "reserve_monthly_budget" in script:
                amount, budget = float(args[0]), float(args[1])
                if current + amount > budget:
                    return 0
                self.values[key] = current + amount
                return 1
            if "settle_monthly_budget" in script:
                reserved, actual = float(args[0]), float(args[1])
                self.values[key] = current - reserved + actual
                return self.values[key]
            if "release_monthly_budget" in script:
                remainder = current - float(args[0])
                if remainder <= 0:
                    self.values.pop(key, None)
                    return 0
                self.values[key] = remainder
                return remainder
            raise AssertionError("unexpected Redis script")


def make_guard(budget=10.0):
    client = MemoryRedis()
    guard = CostGuard(monthly_budget_usd=budget, redis_client=client)
    return guard, client


def test_monthly_budget_reserves_only_when_user_stays_under_limit():
    guard, client = make_guard()

    assert guard.check_budget("student", 8.75)
    assert guard.check_budget("student", 1.25)
    assert not guard.check_budget("student", 0.01)

    key = guard._monthly_key("student")
    assert float(client.get(key)) == pytest.approx(10.0)


def test_monthly_budgets_are_separate_per_user():
    guard, client = make_guard(budget=1.0)

    assert guard.check_budget("student", 0.8)
    assert guard.check_budget("teacher", 0.8)

    assert float(client.get(guard._monthly_key("student"))) == pytest.approx(0.8)
    assert float(client.get(guard._monthly_key("teacher"))) == pytest.approx(0.8)


def test_reservation_settles_to_actual_usage():
    guard, client = make_guard()
    month = guard.current_month()
    assert guard.check_budget("student", 0.5, month_key=month)

    record = guard.record_usage(
        "student", input_tokens=1000, output_tokens=1000,
        reserved_cost=0.5, month_key=month,
    )

    assert record.total_cost_usd == pytest.approx(0.00075)
    assert float(client.get(guard._monthly_key("student", month))) == pytest.approx(0.00075)
    assert guard.get_usage("student")["budget_remaining_usd"] == pytest.approx(9.99925)


def test_failed_llm_call_can_release_reserved_budget():
    guard, client = make_guard()
    month = guard.current_month()
    assert guard.check_budget("student", 2.0, month_key=month)

    guard.release_budget("student", 2.0, month_key=month)

    assert client.get(guard._monthly_key("student", month)) is None


def test_concurrent_reservations_do_not_exceed_monthly_budget():
    guard, client = make_guard()

    with ThreadPoolExecutor(max_workers=20) as pool:
        accepted = list(pool.map(lambda _: guard.check_budget("student", 0.6), range(20)))

    assert sum(accepted) == 16
    assert float(client.get(guard._monthly_key("student"))) == pytest.approx(9.6)


@pytest.mark.parametrize("amount", [-0.01, math.nan, math.inf])
def test_budget_rejects_negative_or_non_finite_estimates(amount):
    guard, _ = make_guard()

    with pytest.raises(ValueError, match="estimated_cost"):
        guard.check_budget("student", amount)


def test_month_key_changes_at_utc_month_boundary():
    guard, _ = make_guard()

    before = guard._monthly_key("student", month_key="2026-11")
    after = guard._monthly_key("student", month_key="2026-12")

    assert before != after


def make_authorized_client(monkeypatch, redis_client, budget=10.0, answer="short answer"):
    api_module.cost_guard.r = redis_client
    api_module.cost_guard.monthly_budget_usd = budget
    api_module.cost_guard._records.clear()
    monkeypatch.setattr(api_module, "ask", lambda _question: answer)
    client = TestClient(api_app)
    login = client.post(
        "/auth/token",
        json={"username": "student", "password": "demo123"},
    )
    assert login.status_code == 200
    client.headers.update({"Authorization": "Bearer " + login.json()["access_token"]})
    return client


def test_ask_settles_budget_to_actual_usage(monkeypatch):
    redis_client = MemoryRedis()
    client = make_authorized_client(monkeypatch, redis_client)

    response = client.post("/ask", json={"question": "hello"})

    assert response.status_code == 200
    key = api_module.cost_guard._monthly_key("student")
    assert float(redis_client.get(key)) == pytest.approx(0.0000027)
    assert response.json()["usage"]["budget_remaining_usd"] == pytest.approx(9.9999973)


def test_ask_does_not_call_llm_when_monthly_reservation_exceeds_budget(monkeypatch):
    redis_client = MemoryRedis()
    client = make_authorized_client(monkeypatch, redis_client, budget=0.0001)
    called = []
    monkeypatch.setattr(api_module, "ask", lambda _question: called.append(True))

    response = client.post("/ask", json={"question": "hello"})

    assert response.status_code == 402
    assert called == []


def test_ask_releases_budget_if_llm_fails(monkeypatch):
    redis_client = MemoryRedis()
    client = make_authorized_client(monkeypatch, redis_client)
    monkeypatch.setattr(api_module, "ask", lambda _question: (_ for _ in ()).throw(RuntimeError("mock failure")))
    client = TestClient(api_app, raise_server_exceptions=False)
    login = client.post(
        "/auth/token",
        json={"username": "student", "password": "demo123"},
    )
    client.headers.update({"Authorization": "Bearer " + login.json()["access_token"]})

    response = client.post("/ask", json={"question": "hello"})

    assert response.status_code == 500
    assert redis_client.get(api_module.cost_guard._monthly_key("student")) is None
