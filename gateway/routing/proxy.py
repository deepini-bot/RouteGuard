import base64
import json
import logging
import uuid

import httpx
from fastapi import Request, Response

from gateway.errors import GatewayError
from gateway.routing.circuit_breaker import CircuitBreaker
from gateway.services.routing_service import RouteInfo
from gateway.utils import client_ip

log = logging.getLogger("gateway.proxy")

# Headers that only make sense for a single connection and must not be passed along
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
# Gateway credentials: backend services must never see these
_CREDENTIALS = {"x-api-key", "authorization", "x-admin-key", "cookie"}

_IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}


def build_upstream_headers(request: Request, client_id: int, request_id: str) -> dict[str, str]:
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _CREDENTIALS
    }
    headers["X-Client-Id"] = str(client_id)  # lets backends know who called, without any secret
    headers["X-Request-Id"] = request_id
    # Replace whatever the caller sent, so the backend can trust this value
    headers = {k: v for k, v in headers.items() if k.lower() != "x-forwarded-for"}
    headers["X-Forwarded-For"] = client_ip(request)
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    # httpx already decoded the body, so content-encoding / content-length would be wrong
    skip = _HOP_BY_HOP | {"content-encoding"}
    return {k: v for k, v in upstream.headers.items() if k.lower() not in skip}


async def _cache_get(redis, key: str) -> Response | None:
    try:
        raw = await redis.get(key)
    except Exception:
        return None  # cache problems should never break the request
    if not raw:
        return None
    data = json.loads(raw)
    headers = dict(data["headers"])
    headers["X-Cache"] = "HIT"
    return Response(content=base64.b64decode(data["body"]), status_code=data["status"], headers=headers)


async def _cache_set(redis, key: str, ttl: int, status: int, headers: dict, body: bytes) -> None:
    payload = json.dumps({"status": status, "headers": headers, "body": base64.b64encode(body).decode()})
    try:
        await redis.set(key, payload, ex=ttl)
    except Exception:
        log.warning("Could not write to cache")


async def forward(
    *,
    http: httpx.AsyncClient,
    redis,
    breaker: CircuitBreaker,
    route: RouteInfo,
    request: Request,
    body: bytes,
    client_id: int,
    request_id: str,
) -> Response:
    """Sends the request to the backend and turns the result (or failure) into a response."""
    method = request.method
    path = request.url.path
    query = request.url.query

    # 1. Cache (GET only, only when the route has a TTL). Auth and rate limiting already happened.
    cache_key = None
    if method == "GET" and route.cache_ttl > 0:
        cache_key = f"cache:{route.id}:{path}?{query}"
        hit = await _cache_get(redis, cache_key)
        if hit is not None:
            return hit

    # 2. Circuit breaker: fail fast if this backend is known to be down
    if not breaker.allow():
        raise GatewayError(503, f"{route.service_name} is temporarily unavailable", "circuit_open")

    # path = what the client called, upstream_path = what the backend expects (differs for versioned routes)
    url = route.base_url.rstrip("/") + route.upstream_path(path) + (f"?{query}" if query else "")
    headers = build_upstream_headers(request, client_id, request_id)

    # 3. Call the backend. Retry only if (a) the method is idempotent and (b) we never reached the
    #    backend (connection refused). Retrying a POST could create a duplicate order or payment.
    attempts = 2 if method in _IDEMPOTENT else 1
    upstream: httpx.Response | None = None
    for _ in range(attempts):
        try:
            upstream = await http.request(method, url, headers=headers, content=body)
            break
        except httpx.ConnectError:
            continue
        except httpx.TimeoutException:
            breaker.record_failure()
            raise GatewayError(504, f"{route.service_name} timed out", "upstream_timeout")
        except httpx.HTTPError:
            breaker.record_failure()
            raise GatewayError(502, f"Bad response from {route.service_name}", "upstream_error")

    if upstream is None:
        breaker.record_failure()
        raise GatewayError(502, f"{route.service_name} is unreachable", "upstream_unreachable")

    if upstream.status_code >= 500:
        breaker.record_failure()
    else:
        breaker.record_success()

    out_headers = _response_headers(upstream)
    if cache_key and upstream.status_code == 200:
        await _cache_set(redis, cache_key, route.cache_ttl, 200, out_headers, upstream.content)
        out_headers["X-Cache"] = "MISS"

    return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)


def new_request_id() -> str:
    return uuid.uuid4().hex
