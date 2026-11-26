"""Monthly per-user budget reservations backed by Redis."""
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import redis
from fastapi import HTTPException


PRICE_PER_1K_INPUT_TOKENS = 0.00015
PRICE_PER_1K_OUTPUT_TOKENS = 0.0006
MAX_OUTPUT_TOKENS = 500

_RESERVE_SCRIPT = """
-- reserve_monthly_budget
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local budget = tonumber(ARGV[2])
if current + amount > budget then
    return 0
end
redis.call('INCRBYFLOAT', KEYS[1], amount)
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return 1
"""
_SETTLE_SCRIPT = """
-- settle_monthly_budget
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local total = current - tonumber(ARGV[1]) + tonumber(ARGV[2])
if total < 0 then total = 0 end
redis.call('SET', KEYS[1], string.format('%.17g', total))
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return total
"""
_RELEASE_SCRIPT = """
-- release_monthly_budget
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
def _month_expiry(month_key: str) -> int:
    month = datetime.strptime(month_key, "%Y-%m").replace(tzinfo=timezone.utc)
    if month.month == 12:
        next_month = month.replace(year=month.year + 1, month=1)
    else:
        next_month = month.replace(month=month.month + 1)
    return int(next_month.timestamp())


@dataclass
class UsageRecord:
    user_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0
    month: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m"))

    @property
    def total_cost_usd(self) -> float:
        input_cost = self.input_tokens * PRICE_PER_1K_INPUT_TOKENS / 1000
        output_cost = self.output_tokens * PRICE_PER_1K_OUTPUT_TOKENS / 1000
        return round(input_cost + output_cost, 6)


class CostGuard:
    """Keep the monthly spend budget in Redis; token counters are display stats."""

    def __init__(self, monthly_budget_usd: float = 10.0, redis_client=None):
        if not math.isfinite(monthly_budget_usd) or monthly_budget_usd < 0:
            raise ValueError("monthly_budget_usd must be finite and nonnegative")
        self.monthly_budget_usd = monthly_budget_usd
        self.r = redis_client or redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        self._records = {}
        self._global_cost = 0.0
        # Kept for the existing admin demo; this is process-local display data.
        self.global_daily_budget_usd = 10.0

    @staticmethod
    def estimate_cost(input_tokens: int, output_tokens: int) -> float:
        input_cost = max(0, input_tokens) * PRICE_PER_1K_INPUT_TOKENS / 1000
        output_cost = max(0, output_tokens) * PRICE_PER_1K_OUTPUT_TOKENS / 1000
        return input_cost + output_cost

    @staticmethod
    def current_month(now: Optional[datetime] = None) -> str:
        return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m")

    @staticmethod
    def _monthly_key(user_id: str, month_key: Optional[str] = None) -> str:
        return "budget:{}:{}".format(user_id, month_key or CostGuard.current_month())

    def _get_record(self, user_id: str, month_key: Optional[str] = None) -> UsageRecord:
        month = month_key or self.current_month()
        record = self._records.get(user_id)
        if not record or record.month != month:
            self._records[user_id] = UsageRecord(user_id=user_id, month=month)
        return self._records[user_id]

    def check_budget(self, user_id: str, estimated_cost: float, month_key: Optional[str] = None) -> bool:
        """Atomically reserve estimated cost; return False if it exceeds $10/month."""
        if not math.isfinite(estimated_cost) or estimated_cost < 0:
            raise ValueError("estimated_cost must be finite and nonnegative")
        month = month_key or self.current_month()
        try:
            allowed = self.r.eval(
                _RESERVE_SCRIPT,
                1,
                self._monthly_key(user_id, month),
                estimated_cost,
                self.monthly_budget_usd,
                _month_expiry(month),
            )
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
        return bool(allowed)

    def release_budget(self, user_id: str, reserved_cost: float, month_key: Optional[str] = None) -> None:
        month = month_key or self.current_month()
        try:
            self.r.eval(
                _RELEASE_SCRIPT,
                1,
                self._monthly_key(user_id, month),
                reserved_cost,
                _month_expiry(month),
            )
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="Cost service unavailable") from exc

    def record_usage(
        self,
        user_id: str,
        input_tokens: int,
        output_tokens: int,
        reserved_cost: float,
        month_key: Optional[str] = None,
    ) -> UsageRecord:
        """Settle a reservation to actual cost and update local usage display stats."""
        month = month_key or self.current_month()
        cost = self.estimate_cost(input_tokens, output_tokens)
        key = self._monthly_key(user_id, month)
        try:
            self.r.eval(
                _SETTLE_SCRIPT,
                1,
                key,
                reserved_cost,
                cost,
                _month_expiry(month),
            )
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="Cost service unavailable") from exc

        record = self._get_record(user_id, month)
        record.input_tokens += max(0, input_tokens)
        record.output_tokens += max(0, output_tokens)
        record.request_count += 1
        self._global_cost += cost
        return record

    def get_usage(self, user_id: str) -> dict:
        record = self._get_record(user_id)
        month = self.current_month()
        try:
            spent = float(self.r.get(self._monthly_key(user_id, month)) or 0.0)
        except redis.RedisError as exc:
            raise HTTPException(status_code=503, detail="Cost service unavailable") from exc
        return {
            "user_id": user_id,
            "date": month,
            "requests": record.request_count,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cost_usd": round(spent, 6),
            "budget_usd": self.monthly_budget_usd,
            "budget_remaining_usd": max(0.0, self.monthly_budget_usd - spent),
            "budget_used_pct": round(spent / self.monthly_budget_usd * 100, 1) if self.monthly_budget_usd else 0.0,
        }


cost_guard = CostGuard(
    monthly_budget_usd=float(os.getenv("MONTHLY_BUDGET_USD", "10.0")),
)
