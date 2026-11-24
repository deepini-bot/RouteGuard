import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.background import BackgroundTask

from gateway.algorithms import ALGORITHMS
from gateway.api import analytics, audit, clients, rate_limits, routes
from gateway.config import settings
from gateway.database.connection import SessionLocal, init_db
from gateway.errors import GatewayError
from gateway.middleware.quota import QuotaChecker, enforce_quota
from gateway.middleware.rate_limiter import check_ip_limit, check_rate_limit, rate_headers
from gateway.middleware.request_logger import log_request
from gateway.routing.circuit_breaker import BreakerRegistry
from gateway.routing.proxy import forward, new_request_id
from gateway.services.decision import decide
from gateway.services.health_service import check_services
from gateway.services.routing_service import version_headers
from gateway.services.usage_service import run_usage_job
from gateway.utils import client_ip

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
DASHBOARD_FILE = Path(__file__).parent / "dashboard" / "index.html"


async def _periodic(name: str, interval: float, job) -> None:
    """Runs `job` now and then every `interval` seconds. A failing run is logged, never fatal."""
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background job %s failed", name)
        await asyncio.sleep(interval)


async def _usage_job(app: FastAPI) -> None:
    # With several gateway workers, only one of them wins this lock per interval
    ttl = max(1, int(settings.usage_rollup_interval) - 1)
    if not await app.state.redis.set("lock:usage_rollup", "1", nx=True, ex=ttl):
        return

    def work() -> None:
        with SessionLocal() as db:
            run_usage_job(db, settings.log_retention_days)

    await run_in_threadpool(work)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    redis = aioredis.from_url(settings.redis_url)
    app.state.redis = redis
    app.state.limiters = {name: cls(redis) for name, cls in ALGORITHMS.items()}
    app.state.quota = QuotaChecker(redis)
    app.state.breakers = BreakerRegistry(settings.cb_failure_threshold, settings.cb_reset_seconds)
    app.state.backend_health = {}
    app.state.http = httpx.AsyncClient(
        # tests can swap in a fake transport instead of real network calls
        transport=getattr(app.state, "transport_override", None),
        timeout=httpx.Timeout(settings.upstream_timeout, connect=min(2.0, settings.upstream_timeout)),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        follow_redirects=False,
    )

    tasks = []
    if settings.health_check_interval > 0:
        tasks.append(asyncio.create_task(
            _periodic("health-checks", settings.health_check_interval, lambda: check_services(app))
        ))
    if settings.usage_rollup_interval > 0:
        tasks.append(asyncio.create_task(
            _periodic("usage-rollup", settings.usage_rollup_interval, lambda: _usage_job(app))
        ))

    yield

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.state.http.aclose()
    await redis.aclose()


app = FastAPI(title="API Gateway & Rate Limiting Platform", lifespan=lifespan)
app.include_router(clients.router)
app.include_router(routes.router)
app.include_router(rate_limits.router)
app.include_router(analytics.router)
app.include_router(audit.router)


@app.get("/health", tags=["health"])
async def health(request: Request):
    try:
        await request.app.state.redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok,
        "circuits": request.app.state.breakers.states(),
        "backends": request.app.state.backend_health,
    }


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    """The page itself is public, but all its data comes from /admin endpoints that need the admin key."""
    return HTMLResponse(DASHBOARD_FILE.read_text(encoding="utf-8"))


@app.api_route("/api/{full_path:path}", methods=METHODS, tags=["gateway"])
async def gateway(request: Request, full_path: str):
    """Every client request comes through here:
    IP limit -> authenticate -> find route -> check permission -> rate limit -> quota
    -> (cache) -> forward -> log.
    """
    started = time.perf_counter()
    request_id = new_request_id()
    record = {
        "client_id": None, "route_id": None, "service_name": None,
        "method": request.method, "path": request.url.path[:500],
        "blocked": False, "block_reason": None,
    }
    extra_headers = {"X-Request-Id": request_id}

    try:
        # 1. Per-IP limit first, so floods and key-guessing are stopped before any database work
        ip_result = await check_ip_limit(request.app.state.limiters, client_ip(request))
        if ip_result is not None and not ip_result.allowed:
            extra_headers.update(rate_headers(ip_result))
            raise GatewayError(429, "Too many requests from this IP address", "ip_rate_limited")

        # 2. Who is calling, where is it going, are they allowed
        decision = await run_in_threadpool(
            decide,
            request.headers.get("x-api-key"),
            request.headers.get("authorization"),
            request.url.path,
            request.method,
        )
        record.update(
            client_id=decision.client_id,
            route_id=decision.route.id,
            service_name=decision.route.service_name,
        )

        # 3. Short-term rate limit
        result = await check_rate_limit(request.app.state.limiters, decision.client_id, decision.policy)
        if result is not None:
            extra_headers.update(rate_headers(result))
            if not result.allowed:
                raise GatewayError(429, "Rate limit exceeded", "rate_limited")

        # 4. Daily / monthly quota
        quota_result = await enforce_quota(request.app.state.quota, decision.client_id, decision.quota)
        if quota_result is not None:
            extra_headers.update(quota_result.headers())
            if not quota_result.allowed:
                label = {"day": "daily", "month": "monthly"}[quota_result.period]
                raise GatewayError(429, f"The {label} quota has been used up", "quota_exceeded")

        # 5. Forward
        body = await request.body()
        response = await forward(
            http=request.app.state.http,
            redis=request.app.state.redis,
            breaker=request.app.state.breakers.get(decision.route.service_name),
            route=decision.route,
            request=request,
            body=body,
            client_id=decision.client_id,
            request_id=request_id,
        )
        extra_headers.update(version_headers(decision.route))
        for k, v in extra_headers.items():
            response.headers[k] = v

    except GatewayError as exc:
        for field in ("client_id", "route_id", "service_name"):
            value = getattr(exc, field)
            if value is not None and record[field] is None:
                record[field] = value
        # Upstream failures were forwarded, so they are errors, not "blocked" requests
        record["blocked"] = not exc.reason.startswith("upstream")
        record["block_reason"] = exc.reason
        response = JSONResponse(
            {"detail": exc.detail}, status_code=exc.status, headers={**extra_headers, **exc.headers}
        )
    except Exception:
        log.exception("Unexpected gateway error")
        record["block_reason"] = "internal_error"
        response = JSONResponse({"detail": "Internal gateway error"}, status_code=500, headers=extra_headers)

    record["status_code"] = response.status_code
    record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    response.background = BackgroundTask(log_request, **record)
    return response
