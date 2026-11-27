import logging
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from gateway.algorithms import RateResult
from gateway.config import settings
from gateway.database.models import RateLimitConfig
from gateway.errors import GatewayError

log = logging.getLogger("gateway.ratelimit")


@dataclass
class Policy:
    key_id: str  # identifies the config in the Redis key
    algorithm: str
    limit: int
    window_seconds: int
    burst: int | None


def resolve_policy(db: Session, client_id: int, route_id: int) -> Policy:
    """Pick the most specific policy: client+route, then client, then route, then the default."""
    rows = db.scalars(
        select(RateLimitConfig).where(
            RateLimitConfig.enabled.is_(True),
            or_(RateLimitConfig.client_id == client_id, RateLimitConfig.client_id.is_(None)),
            or_(RateLimitConfig.route_id == route_id, RateLimitConfig.route_id.is_(None)),
        )
    ).all()

    def specificity(cfg: RateLimitConfig) -> int:
        return (2 if cfg.client_id == client_id else 0) + (1 if cfg.route_id == route_id else 0)

    candidates = [c for c in rows if specificity(c) > 0]
    if candidates:
        cfg = max(candidates, key=specificity)
        return Policy(f"cfg{cfg.id}", cfg.algorithm, cfg.limit, cfg.window_seconds, cfg.burst)

    return Policy("default", settings.default_algorithm, settings.default_limit, settings.default_window, None)


def rate_headers(result: RateResult) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
    }
    if not result.allowed:
        headers["Retry-After"] = str(max(1, -(-result.retry_after_ms // 1000)))  # ceil to whole seconds
    return headers


async def _run(limiter, key: str, limit: int, window: int, burst: int | None) -> RateResult | None:
    try:
        return await limiter.check(key, limit, window, burst)
    except Exception:
        log.exception("Rate limiter unavailable")
        if settings.rate_limit_fail_open:
            return None
        raise GatewayError(503, "Rate limiter unavailable", "rate_limiter_down")


async def check_rate_limit(limiters: dict, client_id: int, policy: Policy) -> RateResult | None:
    """Runs the limiter in Redis. Returns None if Redis is down and we are set to fail open."""
    key = f"rl:{policy.key_id}:{client_id}"
    return await _run(limiters[policy.algorithm], key, policy.limit, policy.window_seconds, policy.burst)


async def check_ip_limit(limiters: dict, ip: str) -> RateResult | None:
    """Per-IP limit, checked before authentication. Always a token bucket, so short bursts are fine."""
    if settings.ip_rate_limit <= 0:
        return None
    return await _run(limiters["token_bucket"], f"rl:ip:{ip}", settings.ip_rate_limit, settings.ip_rate_window, None)
