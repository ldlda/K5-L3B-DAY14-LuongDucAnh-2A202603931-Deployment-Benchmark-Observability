import importlib
from unittest.mock import Mock

import pytest
import redis


def test_settings_read_environment_values(monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/4")
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "7")
    monkeypatch.setenv("DAILY_BUDGET_USD", "2.75")

    from app import config

    config = importlib.reload(config)

    assert config.settings.port == 9123
    assert config.settings.environment == "staging"
    assert config.settings.agent_api_key == "test-key"
    assert config.settings.redis_url == "redis://localhost:6379/4"
    assert config.settings.rate_limit_per_minute == 7
    assert config.settings.daily_budget_usd == 2.75


def test_invalid_numeric_setting_fails_fast(monkeypatch):
    monkeypatch.setenv("PORT", "not-a-port")

    from app import config

    with pytest.raises(ValueError):
        importlib.reload(config)


@pytest.mark.parametrize(
    "name,value",
    [
        ("PORT", "0"),
        ("PORT", "65536"),
        ("RATE_LIMIT_PER_MINUTE", "0"),
        ("RATE_LIMIT_PER_MINUTE", "-1"),
        ("DAILY_BUDGET_USD", "-0.01"),
        ("DAILY_BUDGET_USD", "nan"),
        ("DAILY_BUDGET_USD", "inf"),
    ],
)
def test_numeric_settings_reject_invalid_ranges(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    from app import config

    with pytest.raises(ValueError, match=name):
        importlib.reload(config)


def test_redis_client_is_created_without_ping(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/4")
    ping = Mock(side_effect=AssertionError("Redis must not be pinged on import"))
    monkeypatch.setattr(redis.Redis, "ping", ping)

    from app import config, redis_client

    importlib.reload(config)
    redis_client = importlib.reload(redis_client)

    assert isinstance(redis_client.r, redis.Redis)
    assert ping.call_count == 0


def test_redis_healthy_reports_ping_result(monkeypatch):
    from app import redis_client

    client = Mock()
    client.ping.return_value = True
    monkeypatch.setattr(redis_client, "r", client)

    assert redis_client.redis_healthy() is True

    client.ping.side_effect = redis.ConnectionError("Redis is unavailable")
    assert redis_client.redis_healthy() is False
