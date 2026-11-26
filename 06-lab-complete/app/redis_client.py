"""Shared lazy Redis client used by readiness, rate limiting, and cost guard."""

import redis

from .config import settings


# Creating the client is local-only; redis-py opens a connection on first use.
r = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,
)


def redis_healthy() -> bool:
    try:
        return bool(r and r.ping())
    except (redis.RedisError, OSError):
        return False
