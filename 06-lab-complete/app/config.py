"""Typed application settings loaded from environment variables."""

from dataclasses import dataclass
import math
import os


@dataclass
class Settings:
    port: int
    environment: str
    agent_api_key: str
    redis_url: str
    rate_limit_per_minute: int
    daily_budget_usd: float

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if self.rate_limit_per_minute <= 0:
            raise ValueError("RATE_LIMIT_PER_MINUTE must be positive")
        if not math.isfinite(self.daily_budget_usd) or self.daily_budget_usd < 0:
            raise ValueError("DAILY_BUDGET_USD must be finite and nonnegative")


def load_settings() -> Settings:
    """Read settings afresh so process-level environment is authoritative."""
    daily_budget = os.getenv("DAILY_BUDGET_USD")
    if daily_budget is None:
        daily_budget = os.getenv("MONTHLY_BUDGET_USD", "1.0")

    return Settings(
        port=int(os.getenv("PORT", "8000")),
        environment=os.getenv("ENVIRONMENT", "development"),
        agent_api_key=os.getenv("AGENT_API_KEY", ""),
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "10")),
        daily_budget_usd=float(daily_budget),
    )


settings = load_settings()
