import uuid

from redis.asyncio import Redis

from gateway.algorithms.token_bucket import RateResult

# Sliding window log: every allowed request is a member of a sorted set, scored by its time.
# Old entries are dropped, then we count what is left. Exact, but uses more memory than a counter.
_LUA = """
local key = KEYS[1]
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

redis.call('ZREMRANGEBYSCORE', key, 0, now - window_ms)
local count = redis.call('ZCARD', key)

if count < limit then
  redis.call('ZADD', key, now, member)
  redis.call('PEXPIRE', key, window_ms)
  return {1, limit - count - 1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_ms = window_ms - (now - tonumber(oldest[2]))
return {0, 0, math.max(retry_ms, 1)}
"""


class SlidingWindow:
    name = "sliding_window"

    def __init__(self, redis: Redis) -> None:
        self._script = redis.register_script(_LUA)

    async def check(self, key: str, limit: int, window_seconds: int, burst: int | None = None) -> RateResult:
        allowed, remaining, retry_ms = await self._script(
            keys=[key], args=[window_seconds * 1000, limit, uuid.uuid4().hex]
        )
        return RateResult(bool(allowed), limit, int(remaining), int(retry_ms))
