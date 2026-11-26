"""Redis-backed, atomic sliding-window rate limiting."""

import hashlib
import math
import time
import uuid

import redis
from fastapi import HTTPException

from .config import settings
from .redis_client import r


WINDOW_MS = 60_000
_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cutoff = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local window = tonumber(ARGV[5])

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = tonumber(redis.call('ZCARD', key))
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_ms = math.max(1, window - (now - tonumber(oldest[2])))
    return {0, retry_ms}
end

redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return {1, 0}
"""


def check_rate_limit(key: str) -> None:
    """Allow at most the configured number of requests in the last 60 seconds."""
    now_ms = int(time.time() * 1000)
    redis_key = f"agent:rate:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    member = f"{now_ms}:{uuid.uuid4().hex}"
    try:
        allowed, retry_ms = r.eval(
            _SLIDING_WINDOW_SCRIPT,
            1,
            redis_key,
            now_ms,
            now_ms - WINDOW_MS,
            settings.rate_limit_per_minute,
            member,
            WINDOW_MS,
        )
    except redis.RedisError as exc:
        raise HTTPException(status_code=503, detail="Rate-limit service unavailable") from exc

    if not int(allowed):
        retry_after = max(1, math.ceil(int(retry_ms) / 1000))
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
