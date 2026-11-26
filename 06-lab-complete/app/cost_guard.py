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
_RECORD_COST_SCRIPT = """
local total = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
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


def check_budget() -> None:
    """Gọi TRƯỚC khi gọi LLM. Hết budget → raise HTTPException(status_code=402)."""
    try:
        current_spend = float(r.get(_daily_key()) or 0.0)
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
    if current_spend >= settings.daily_budget_usd:
        raise HTTPException(status_code=402, detail="Daily budget exceeded")


def record_cost(input_tokens: int, output_tokens: int) -> float:
    """Gọi SAU khi gọi LLM. Cộng chi phí của request vào tổng, trả về chi phí request."""
    input_cost = max(0, input_tokens) * PRICE_INPUT_PER_1K / 1000
    output_cost = max(0, output_tokens) * PRICE_OUTPUT_PER_1K / 1000
    cost = input_cost + output_cost
    now = datetime.now(timezone.utc)
    try:
        r.eval(_RECORD_COST_SCRIPT, 1, _daily_key(now), cost, _next_utc_midnight(now))
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
    return cost
