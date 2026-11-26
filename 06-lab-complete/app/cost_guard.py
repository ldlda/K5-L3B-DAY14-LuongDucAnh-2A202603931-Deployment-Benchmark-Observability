"""UTC daily spend accounting backed by Redis."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import redis
from fastapi import HTTPException

from .config import settings
from .redis_client import r

# Giá USD / 1K tokens (tham khảo gpt-4o-mini)
PRICE_INPUT_PER_1K = 0.00015
PRICE_OUTPUT_PER_1K = 0.0006
MAX_OUTPUT_TOKENS = 256
_RECORD_COST_SCRIPT = """
local total = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
redis.call('EXPIREAT', KEYS[1], ARGV[2])
return total
"""
_RESERVE_COST_SCRIPT = """
-- reserve_daily_budget
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local budget = tonumber(ARGV[2])
if current + amount > budget then
    return 0
end
local total = redis.call('INCRBYFLOAT', KEYS[1], amount)
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return 1
"""
_SETTLE_COST_SCRIPT = """
-- settle_daily_budget
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local total = current - tonumber(ARGV[1]) + tonumber(ARGV[2])
redis.call('SET', KEYS[1], string.format('%.17g', total))
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return total
"""
_RELEASE_COST_SCRIPT = """
-- release_daily_budget
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local total = current - tonumber(ARGV[1])
if total <= 0 then
    redis.call('DEL', KEYS[1])
    return 0
end
redis.call('SET', KEYS[1], string.format('%.17g', total))
redis.call('EXPIREAT', KEYS[1], ARGV[2])
return total
"""


def _daily_key(now: Optional[datetime] = None) -> str:
    """Return the UTC date key used to isolate each day's spend."""
    now = now or datetime.now(timezone.utc)
    utc_date = now.astimezone(timezone.utc).date().isoformat()
    return f"agent:cost:{utc_date}"


def _next_utc_midnight(now: datetime) -> int:
    tomorrow = now.astimezone(timezone.utc).date() + timedelta(days=1)
    return int(datetime.combine(tomorrow, datetime.min.time(), timezone.utc).timestamp())


def _cost(input_tokens: int, output_tokens: int) -> float:
    input_cost = max(0, input_tokens) * PRICE_INPUT_PER_1K / 1000
    output_cost = max(0, output_tokens) * PRICE_OUTPUT_PER_1K / 1000
    return input_cost + output_cost


def check_budget(input_tokens: int = 0, output_tokens: int = MAX_OUTPUT_TOKENS) -> float:
    """Atomically reserve estimated cost before calling the LLM."""
    now = datetime.now(timezone.utc)
    reservation = _cost(input_tokens, output_tokens)
    try:
        allowed = r.eval(
            _RESERVE_COST_SCRIPT,
            1,
            _daily_key(now),
            reservation,
            settings.daily_budget_usd,
            _next_utc_midnight(now),
        )
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
    if not allowed:
        raise HTTPException(status_code=402, detail="Daily budget exceeded")
    return reservation


def record_cost(input_tokens: int, output_tokens: int, reserved_cost: Optional[float] = None) -> float:
    """Settle a reservation to actual usage, or record unreserved usage."""
    cost = _cost(input_tokens, output_tokens)
    now = datetime.now(timezone.utc)
    try:
        if reserved_cost is None:
            r.eval(_RECORD_COST_SCRIPT, 1, _daily_key(now), cost, _next_utc_midnight(now))
        else:
            r.eval(
                _SETTLE_COST_SCRIPT,
                1,
                _daily_key(now),
                reserved_cost,
                cost,
                _next_utc_midnight(now),
            )
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
    return cost


def release_budget(reserved_cost: float) -> None:
    """Release a reservation if the LLM call itself fails."""
    now = datetime.now(timezone.utc)
    try:
        r.eval(
            _RELEASE_COST_SCRIPT,
            1,
            _daily_key(now),
            reserved_cost,
            _next_utc_midnight(now),
        )
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
