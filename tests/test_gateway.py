"""End-to-end tests. Each one matches a demo scenario from the project spec."""
import time
from concurrent.futures import ThreadPoolExecutor

import jwt


# --- Test 1: successful routing -------------------------------------------------------------

def test_successful_routing(gw):
    s = gw.bootstrap()
    r = gw.get("/api/products/101?color=red", s["key"])
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "product-svc"
    assert body["path"] == "/api/products/101"
    assert body["query"] == "color=red"
    assert "x-request-id" in r.headers


def test_post_body_and_method_are_forwarded(gw):
    s = gw.bootstrap()
    r = gw.client.post("/api/users", json={"name": "Meena"}, headers={"X-API-Key": s["key"]})
    assert r.status_code == 200
    assert r.json()["method"] == "POST"
    assert "Meena" in r.json()["body"]


def test_gateway_credentials_are_not_forwarded(gw):
    s = gw.bootstrap()
    r = gw.get("/api/products/1", s["key"], headers={"Cookie": "session=abc", "X-Custom": "keep-me"})
    sent = r.json()["headers"]
    assert "x-api-key" not in sent
    assert "authorization" not in sent
    assert "cookie" not in sent
    assert sent["x-custom"] == "keep-me"
    assert sent["x-client-id"] == str(s["client_id"])


def test_unknown_path_returns_404(gw):
    s = gw.bootstrap()
    assert gw.get("/api/nothing-here", s["key"]).status_code == 404


def test_longest_prefix_wins(gw):
    s = gw.bootstrap()
    svc = gw.admin("POST", "/admin/services", json={"name": "reviews", "base_url": "http://reviews-svc:9000"}).json()
    route = gw.admin(
        "POST", "/admin/routes", json={"path_prefix": "/api/products/reviews", "service_id": svc["id"]}
    ).json()
    gw.admin(
        "PUT", f"/admin/clients/{s['client_id']}/permissions",
        json={"route_ids": [s["ids"]["product"]["route_id"], route["id"]]},
    )
    assert gw.get("/api/products/reviews/5", s["key"]).json()["service"] == "reviews-svc"
    assert gw.get("/api/products/5", s["key"]).json()["service"] == "product-svc"


# --- Test 2: invalid / missing credentials --------------------------------------------------

def test_invalid_api_key_is_rejected_and_never_reaches_backend(gw):
    gw.bootstrap()
    r = gw.get("/api/products/101", "gw_not_a_real_key")
    assert r.status_code == 401
    assert gw.upstream.calls == []


def test_missing_credentials_returns_401(gw):
    gw.bootstrap()
    r = gw.get("/api/products/101")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    assert gw.upstream.calls == []


def test_unauthenticated_caller_cannot_probe_routes(gw):
    gw.bootstrap()
    # Same 401 whether the route exists or not
    assert gw.get("/api/products/1").status_code == 401
    assert gw.get("/api/does-not-exist").status_code == 401


# --- Authorization --------------------------------------------------------------------------

def test_client_cannot_access_route_without_permission(gw):
    s = gw.bootstrap()
    r = gw.get("/api/orders/1", s["key"])  # app-a has products + users only
    assert r.status_code == 403
    assert gw.upstream.calls_to("order-svc") == 0


def test_disabled_client_is_rejected(gw):
    s = gw.bootstrap()
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    gw.admin("PATCH", f"/admin/clients/{s['client_id']}", json={"status": "disabled"})
    assert gw.get("/api/products/1", s["key"]).status_code == 403


# --- Test 3: rate limiting ------------------------------------------------------------------

def test_rate_limit_five_per_minute(gw):
    s = gw.bootstrap()
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 5, "window_seconds": 60})
    codes = [gw.get("/api/products/101", s["key"]).status_code for _ in range(10)]
    assert codes == [200] * 5 + [429] * 5
    assert gw.upstream.calls_to("product-svc") == 5  # blocked requests never reach the backend


def test_rate_limit_response_headers(gw):
    s = gw.bootstrap()
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 2, "window_seconds": 60})
    ok = gw.get("/api/products/1", s["key"])
    assert ok.headers["X-RateLimit-Limit"] == "2"
    assert ok.headers["X-RateLimit-Remaining"] == "1"
    gw.get("/api/products/1", s["key"])
    blocked = gw.get("/api/products/1", s["key"])
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_rate_limits_are_per_client(gw):
    s = gw.bootstrap()
    gw.admin("POST", "/admin/rate-limits", json={"route_id": s["ids"]["product"]["route_id"], "limit": 2})
    other = gw.admin("POST", "/admin/clients", json={"name": "app-b"}).json()
    other_key = gw.admin("POST", f"/admin/clients/{other['id']}/keys").json()["api_key"]
    gw.admin("PUT", f"/admin/clients/{other['id']}/permissions", json={"route_ids": [s["ids"]["product"]["route_id"]]})

    assert [gw.get("/api/products/1", s["key"]).status_code for _ in range(3)] == [200, 200, 429]
    # app-b has its own bucket and is not affected by app-a using up its limit
    assert gw.get("/api/products/1", other_key).status_code == 200


def test_sliding_window_algorithm_via_policy(gw):
    s = gw.bootstrap()
    gw.admin(
        "POST", "/admin/rate-limits",
        json={"client_id": s["client_id"], "algorithm": "sliding_window", "limit": 3, "window_seconds": 60},
    )
    assert [gw.get("/api/products/1", s["key"]).status_code for _ in range(5)] == [200, 200, 200, 429, 429]


def test_more_specific_policy_wins(gw):
    s = gw.bootstrap()
    route_id = s["ids"]["product"]["route_id"]
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 100})
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "route_id": route_id, "limit": 2})
    assert [gw.get("/api/products/1", s["key"]).status_code for _ in range(3)] == [200, 200, 429]
    # a different route still uses the generous client-wide policy
    assert gw.get("/api/users/1", s["key"]).status_code == 200


def test_updating_a_policy_takes_effect(gw):
    s = gw.bootstrap()
    cfg = gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 1, "window_seconds": 3600}).json()
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    assert gw.get("/api/products/1", s["key"]).status_code == 429
    gw.admin("PATCH", f"/admin/rate-limits/{cfg['id']}", json={"enabled": False})
    assert gw.get("/api/products/1", s["key"]).status_code == 200  # falls back to the default limit


# --- Test 4: concurrent requests ------------------------------------------------------------

def test_concurrent_requests_respect_shared_limit(gw):
    s = gw.bootstrap()
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 20, "window_seconds": 3600})

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(lambda _: gw.get("/api/products/1", s["key"]).status_code, range(100)))

    assert codes.count(200) == 20
    assert codes.count(429) == 80
    assert gw.upstream.calls_to("product-svc") == 20


# --- Test 5: backend failure ----------------------------------------------------------------

def test_backend_down_returns_502_not_a_crash(gw):
    s = gw.bootstrap()
    gw.upstream.mode["product-svc"] = "down"
    r = gw.get("/api/products/1", s["key"])
    assert r.status_code == 502
    assert gw.get("/health").status_code == 200  # gateway is still fine


def test_backend_timeout_returns_504(gw):
    s = gw.bootstrap()
    gw.upstream.mode["product-svc"] = "timeout"
    assert gw.get("/api/products/1", s["key"]).status_code == 504


def test_get_is_retried_once_on_connection_failure_but_post_is_not(gw):
    s = gw.bootstrap()
    gw.upstream.mode["user-svc"] = "down"
    gw.get("/api/users/1", s["key"])
    assert gw.upstream.calls_to("user-svc") == 2  # GET: tried twice

    gw.upstream.calls.clear()
    gw.client.post("/api/users", json={}, headers={"X-API-Key": s["key"]})
    assert gw.upstream.calls_to("user-svc") == 1  # POST: never retried (could create duplicates)


def test_backend_error_status_is_passed_through(gw):
    s = gw.bootstrap()
    gw.upstream.mode["product-svc"] = "error500"
    assert gw.get("/api/products/1", s["key"]).status_code == 500


def test_circuit_breaker_opens_then_recovers(gw):
    s = gw.bootstrap()
    gw.upstream.mode["product-svc"] = "error500"
    for _ in range(3):  # threshold is 3 in the test config
        assert gw.get("/api/products/1", s["key"]).status_code == 500
    calls_before = gw.upstream.calls_to("product-svc")

    r = gw.get("/api/products/1", s["key"])
    assert r.status_code == 503  # circuit is open: rejected without calling the backend
    assert gw.upstream.calls_to("product-svc") == calls_before
    assert gw.get("/health").json()["circuits"]["product"] == "open"

    gw.upstream.mode["product-svc"] = "ok"
    time.sleep(1.1)  # reset time is 1s in the test config
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    assert gw.get("/health").json()["circuits"]["product"] == "closed"


# --- Test 6: revoked API key ----------------------------------------------------------------

def test_revoked_key_is_rejected(gw):
    s = gw.bootstrap()
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    gw.admin("PATCH", f"/admin/keys/{s['key_id']}", json={"status": "revoked"})
    assert gw.get("/api/products/1", s["key"]).status_code == 401


def test_deactivated_key_can_be_reactivated_but_revoked_cannot(gw):
    s = gw.bootstrap()
    gw.admin("PATCH", f"/admin/keys/{s['key_id']}", json={"status": "inactive"})
    assert gw.get("/api/products/1", s["key"]).status_code == 401
    gw.admin("PATCH", f"/admin/keys/{s['key_id']}", json={"status": "active"})
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    gw.admin("PATCH", f"/admin/keys/{s['key_id']}", json={"status": "revoked"})
    assert gw.admin("PATCH", f"/admin/keys/{s['key_id']}", json={"status": "active"}).status_code == 409


def test_raw_key_is_never_stored(gw):
    from sqlalchemy import select

    from gateway.database.connection import SessionLocal
    from gateway.database.models import ApiKey

    s = gw.bootstrap()
    with SessionLocal() as db:
        key = db.scalar(select(ApiKey))
    assert s["key"] not in (key.key_hash, key.key_prefix)
    assert len(key.key_hash) == 64
    # the list endpoint shows a prefix only, never the key
    listed = gw.admin("GET", f"/admin/clients/{s['client_id']}/keys").json()
    assert "api_key" not in listed[0]


def test_expired_key_is_rejected(gw):
    from sqlalchemy import select

    from gateway.database.connection import SessionLocal
    from gateway.database.models import ApiKey, utcnow
    from datetime import timedelta

    s = gw.bootstrap()
    with SessionLocal() as db:
        key = db.scalar(select(ApiKey))
        key.expires_at = utcnow() - timedelta(days=1)
        db.commit()
    assert gw.get("/api/products/1", s["key"]).status_code == 401


# --- Test 7: dynamic route updates ----------------------------------------------------------

def test_changing_service_url_takes_effect_immediately(gw):
    s = gw.bootstrap()
    assert gw.get("/api/products/1", s["key"]).json()["service"] == "product-svc"
    gw.admin("PATCH", f"/admin/services/{s['ids']['product']['service_id']}", json={"base_url": "http://product-v2:8100"})
    assert gw.get("/api/products/1", s["key"]).json()["service"] == "product-v2"


def test_disabled_route_returns_404(gw):
    s = gw.bootstrap()
    gw.admin("PATCH", f"/admin/routes/{s['ids']['product']['route_id']}", json={"enabled": False})
    assert gw.get("/api/products/1", s["key"]).status_code == 404
    gw.admin("PATCH", f"/admin/routes/{s['ids']['product']['route_id']}", json={"enabled": True})
    assert gw.get("/api/products/1", s["key"]).status_code == 200


def test_route_method_restriction(gw):
    s = gw.bootstrap()
    gw.admin("PATCH", f"/admin/routes/{s['ids']['product']['route_id']}", json={"methods": ["GET"]})
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    assert gw.client.delete("/api/products/1", headers={"X-API-Key": s["key"]}).status_code == 404


# --- JWT ------------------------------------------------------------------------------------

def test_jwt_authentication(gw):
    s = gw.bootstrap()
    token = gw.admin("POST", f"/admin/clients/{s['client_id']}/token").json()["access_token"]
    r = gw.client.get("/api/products/1", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "authorization" not in r.json()["headers"]


def test_jwt_is_rejected_when_expired_forged_or_malformed(gw):
    s = gw.bootstrap()
    from datetime import datetime, timedelta, timezone

    secret = "test-secret-test-secret-test-secret-123"
    now = datetime.now(timezone.utc)
    expired = jwt.encode({"sub": str(s["client_id"]), "exp": now - timedelta(minutes=1)}, secret, algorithm="HS256")
    forged = jwt.encode({"sub": str(s["client_id"]), "exp": now + timedelta(hours=1)}, "x" * 40, algorithm="HS256")
    no_exp = jwt.encode({"sub": str(s["client_id"])}, secret, algorithm="HS256")
    for bad in (expired, forged, no_exp, "not.a.jwt"):
        assert gw.client.get("/api/products/1", headers={"Authorization": f"Bearer {bad}"}).status_code == 401


def test_jwt_clients_still_need_route_permission(gw):
    s = gw.bootstrap()
    token = gw.admin("POST", f"/admin/clients/{s['client_id']}/token").json()["access_token"]
    assert gw.client.get("/api/orders/1", headers={"Authorization": f"Bearer {token}"}).status_code == 403


# --- Caching --------------------------------------------------------------------------------

def test_get_responses_are_cached_when_route_has_ttl(gw):
    s = gw.bootstrap(product_cache_ttl=30)
    first = gw.get("/api/products/1", s["key"])
    second = gw.get("/api/products/1", s["key"])
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"
    assert gw.upstream.calls_to("product-svc") == 1


def test_cache_does_not_bypass_auth_or_rate_limits(gw):
    s = gw.bootstrap(product_cache_ttl=30)
    gw.get("/api/products/1", s["key"])  # fills the cache
    assert gw.get("/api/products/1", "gw_wrong").status_code == 401
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 1, "window_seconds": 3600})
    # one request is already spent on the default policy; the new 1/hour policy starts fresh
    assert gw.get("/api/products/1", s["key"]).status_code == 200
    assert gw.get("/api/products/1", s["key"]).status_code == 429


def test_post_is_never_cached(gw):
    s = gw.bootstrap(product_cache_ttl=30)
    gw.client.post("/api/products", json={}, headers={"X-API-Key": s["key"]})
    gw.client.post("/api/products", json={}, headers={"X-API-Key": s["key"]})
    assert gw.upstream.calls_to("product-svc") == 2


# --- Admin API ------------------------------------------------------------------------------

def test_admin_endpoints_require_admin_key(gw):
    assert gw.client.get("/admin/clients").status_code == 401
    assert gw.client.get("/admin/clients", headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert gw.admin("GET", "/admin/clients").status_code == 200


def test_api_key_is_not_accepted_for_admin(gw):
    s = gw.bootstrap()
    assert gw.client.get("/admin/clients", headers={"X-API-Key": s["key"]}).status_code == 401


def test_admin_validation_errors(gw):
    assert gw.admin("POST", "/admin/services", json={"name": "x", "base_url": "ftp://nope"}).status_code == 422
    svc = gw.admin("POST", "/admin/services", json={"name": "ok", "base_url": "http://ok:1"}).json()
    assert gw.admin("POST", "/admin/routes", json={"path_prefix": "/admin/x", "service_id": svc["id"]}).status_code == 422
    assert gw.admin("POST", "/admin/rate-limits", json={"limit": 5}).status_code == 422
    assert gw.admin("POST", "/admin/rate-limits", json={"client_id": 1, "limit": 0}).status_code == 422


def test_duplicate_names_return_409(gw):
    gw.admin("POST", "/admin/clients", json={"name": "dup"})
    assert gw.admin("POST", "/admin/clients", json={"name": "dup"}).status_code == 409


# --- Analytics ------------------------------------------------------------------------------

def _wait_for_logs(gw, expected: int, timeout: float = 3.0):
    """Logs are written in a background task, so give them a moment."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gw.admin("GET", "/admin/analytics/overview").json()["total_requests"] >= expected:
            return
        time.sleep(0.05)


def test_analytics_overview_and_client_usage(gw):
    s = gw.bootstrap()
    gw.admin("POST", "/admin/rate-limits", json={"client_id": s["client_id"], "limit": 3, "window_seconds": 3600})
    for _ in range(5):
        gw.get("/api/products/1", s["key"])  # 3 allowed, 2 rate limited
    gw.get("/api/orders/1", s["key"])  # 403
    gw.get("/api/products/1", "gw_bad")  # 401
    _wait_for_logs(gw, 7)

    overview = gw.admin("GET", "/admin/analytics/overview").json()
    assert overview["total_requests"] == 7
    assert overview["blocked_requests"] == 4
    assert overview["block_reasons"] == {"rate_limited": 2, "forbidden": 1, "auth": 1}
    assert overview["services"][0]["service"] == "product"
    assert overview["services"][0]["requests"] == 3
    assert overview["requests_per_minute"]["series"]

    usage = gw.admin("GET", f"/admin/analytics/clients/{s['client_id']}").json()
    assert usage["total_requests"] == 6  # the bad-key request has no client
    assert usage["status_codes"] == {"200": 3, "403": 1, "429": 2}
    assert usage["top_endpoints"][0] == {"route": "/api/products", "requests": 5}

    routes = gw.admin("GET", "/admin/analytics/routes").json()
    products = next(r for r in routes if r["route"] == "/api/products")
    assert products["requests"] == 5 and products["blocked"] == 2


def test_analytics_error_rate_per_service(gw):
    s = gw.bootstrap()
    gw.upstream.mode["product-svc"] = "error500"
    gw.get("/api/products/1", s["key"])
    gw.get("/api/products/1", s["key"])
    gw.get("/api/users/1", s["key"])
    _wait_for_logs(gw, 3)
    services = {x["service"]: x for x in gw.admin("GET", "/admin/analytics/overview").json()["services"]}
    assert services["product"]["error_rate"] == 1.0
    assert services["user"]["error_rate"] == 0.0


def test_health(gw):
    body = gw.get("/health").json()
    assert body["status"] == "ok" and body["redis"] is True
