from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass
class RateResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int = 0


# The whole check runs inside Redis as one Lua script, so it is atomic.
# Two gateway workers can never read the same token count and both spend it.
# Time comes from Redis (TIME), so workers with different clocks still agree.
_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + (elapsed / 1000) * refill_per_sec)

local allowed = 0
local retry_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_ms = math.ceil(((1 - tokens) / refill_per_sec) * 1000)
end

redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', key, math.ceil(capacity / refill_per_sec * 1000) + 1000)
return {allowed, math.floor(tokens), retry_ms}
"""


class TokenBucket:
    """Allows bursts up to `burst` requests, then refills at limit/window per second."""

    name = "token_bucket"

    def __init__(self, redis: Redis) -> None:
        self._script = redis.register_script(_LUA)

    async def check(self, key: str, limit: int, window_seconds: int, burst: int | None = None) -> RateResult:
        capacity = burst or limit
        refill_per_sec = limit / window_seconds
        allowed, remaining, retry_ms = await self._script(keys=[key], args=[capacity, refill_per_sec])
        return RateResult(bool(allowed), capacity, int(remaining), int(retry_ms))
