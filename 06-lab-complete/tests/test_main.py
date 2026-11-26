import json
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, main


@pytest.fixture
def client():
    return TestClient(main.app)


def test_health_does_not_depend_on_redis(client, monkeypatch):
    monkeypatch.setattr(main, "redis_healthy", Mock(side_effect=AssertionError("liveness must not ping Redis")))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("healthy, status", [(True, 200), (False, 503)])
def test_readiness_tracks_redis(client, monkeypatch, healthy, status):
    monkeypatch.setattr(main, "redis_healthy", lambda: healthy)

    assert client.get("/ready").status_code == status


def test_ask_requires_api_key(client, monkeypatch):
    monkeypatch.setattr(auth.settings, "agent_api_key", "expected-key")

    assert client.post("/ask", json={"question": "hello"}).status_code == 401
    assert client.post("/ask", json={"question": "hello"}, headers={"X-API-Key": "wrong"}).status_code == 401


def test_ask_validates_question_length(client, monkeypatch):
    monkeypatch.setattr(auth.settings, "agent_api_key", "expected-key")
    response = client.post(
        "/ask",
        json={"question": ""},
        headers={"X-API-Key": "expected-key"},
    )

    assert response.status_code == 422


def test_ask_runs_guards_and_records_cost(client, monkeypatch, caplog):
    monkeypatch.setattr(auth.settings, "agent_api_key", "expected-key")
    rate = Mock()
    budget = Mock(return_value=0.01)
    llm = Mock(return_value="A useful answer")
    record = Mock(return_value=0.001)
    monkeypatch.setattr(main, "check_rate_limit", rate)
    monkeypatch.setattr(main, "check_budget", budget)
    monkeypatch.setattr(main, "llm_ask", llm)
    monkeypatch.setattr(main, "record_cost", record)
    monkeypatch.setattr(main.logger, "propagate", True)

    with caplog.at_level("INFO", logger="app.main"):
        response = client.post(
            "/ask",
            json={"question": "hello world"},
            headers={"X-API-Key": "expected-key"},
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "A useful answer"
    rate.assert_called_once_with("expected-key")
    budget.assert_called_once_with(input_tokens=2)
    llm.assert_called_once_with("hello world")
    record.assert_called_once_with(input_tokens=2, output_tokens=3, reserved_cost=0.01)
    request_logs = [r for r in caplog.records if r.name == "app.main"]
    assert request_logs
    assert json.loads(request_logs[-1].getMessage())["event"] == "request"


@pytest.mark.parametrize("guard_name, code", [("check_rate_limit", 429), ("check_budget", 402)])
def test_ask_stops_before_llm_when_guard_rejects(client, monkeypatch, guard_name, code):
    monkeypatch.setattr(auth.settings, "agent_api_key", "expected-key")
    rejected = Mock(side_effect=HTTPException(status_code=code, detail="blocked"))
    llm = Mock(return_value="should not run")
    if guard_name != "check_rate_limit":
        monkeypatch.setattr(main, "check_rate_limit", Mock())
    monkeypatch.setattr(main, guard_name, rejected)
    monkeypatch.setattr(main, "llm_ask", llm)

    response = client.post(
        "/ask",
        json={"question": "hello"},
        headers={"X-API-Key": "expected-key"},
    )

    assert response.status_code == code
    llm.assert_not_called()
