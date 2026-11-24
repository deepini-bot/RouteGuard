import atexit
import os
import shutil
import socket
import subprocess
import tempfile
import time

import httpx
import pytest

# --- Environment must be set BEFORE any gateway module is imported (config is read at import) ---
_tmp = tempfile.mkdtemp(prefix="gwtest-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ADMIN_KEY"] = "test-admin"
os.environ["JWT_SECRET"] = "test-secret-test-secret-test-secret-123"
os.environ["CB_FAILURE_THRESHOLD"] = "3"
os.environ["CB_RESET_SECONDS"] = "1"
os.environ["UPSTREAM_TIMEOUT"] = "2"
os.environ["IP_RATE_LIMIT"] = "0"  # tests switch it on when they need it
os.environ["HEALTH_CHECK_INTERVAL"] = "0"  # tests run the checks by hand
os.environ["USAGE_ROLLUP_INTERVAL"] = "0"

_redis_proc = None
if os.getenv("TEST_REDIS_URL"):
    REDIS_URL = os.environ["TEST_REDIS_URL"]  # e.g. the Redis service container in CI
else:
    if not shutil.which("redis-server"):
        pytest.exit("Need redis-server on PATH, or set TEST_REDIS_URL", returncode=2)
    _port = 6390
    _redis_proc = subprocess.Popen(
        ["redis-server", "--port", str(_port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("localhost", _port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    REDIS_URL = f"redis://localhost:{_port}/0"
os.environ["REDIS_URL"] = REDIS_URL


@atexit.register
def _cleanup():
    if _redis_proc:
        _redis_proc.terminate()
    shutil.rmtree(_tmp, ignore_errors=True)


ADMIN = {"X-Admin-Key": "test-admin"}


class FakeUpstream:
    """Stands in for the backend services, so tests don't need real network calls.

    Set `mode[host]` to "ok", "down" (connection refused), "timeout" or "error500".
    Every request the gateway sends is saved in `calls`.
    """

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.mode: dict[str, str] = {}
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        mode = self.mode.get(request.url.host, "ok")
        if mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout("too slow", request=request)
        if mode == "error500":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200,
            json={
                "service": request.url.host,
                "path": request.url.path,
                "query": request.url.query.decode(),
                "method": request.method,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": request.content.decode(),
            },
        )

    def calls_to(self, host: str) -> int:
        return sum(1 for c in self.calls if c.url.host == host)


class Gw:
    """Small helper around the test client, so tests read like the scenarios in the spec."""

    def __init__(self, client, upstream: FakeUpstream):
        self.client = client
        self.upstream = upstream

    def admin(self, method: str, path: str, **kwargs):
        return self.client.request(method, path, headers=ADMIN, **kwargs)

    def bootstrap(self, product_cache_ttl: int = 0) -> dict:
        """Creates 3 services + 3 routes + one client allowed on products and users (not orders)."""
        ids = {}
        for name, host, port, prefix in [
            ("product", "product-svc", 8002, "/api/products"),
            ("user", "user-svc", 8001, "/api/users"),
            ("order", "order-svc", 8003, "/api/orders"),
        ]:
            svc = self.admin("POST", "/admin/services", json={"name": name, "base_url": f"http://{host}:{port}"})
            assert svc.status_code == 201, svc.text
            ttl = product_cache_ttl if name == "product" else 0
            route = self.admin(
                "POST", "/admin/routes", json={"path_prefix": prefix, "service_id": svc.json()["id"], "cache_ttl": ttl}
            )
            assert route.status_code == 201, route.text
            ids[name] = {"service_id": svc.json()["id"], "route_id": route.json()["id"]}

        client = self.admin("POST", "/admin/clients", json={"name": "app-a"}).json()
        key = self.admin("POST", f"/admin/clients/{client['id']}/keys").json()
        self.admin(
            "PUT", f"/admin/clients/{client['id']}/permissions",
            json={"route_ids": [ids["product"]["route_id"], ids["user"]["route_id"]]},
        )
        return {"client_id": client["id"], "key": key["api_key"], "key_id": key["id"], "ids": ids}

    def get(self, path: str, key: str | None = None, **kwargs):
        headers = kwargs.pop("headers", {})
        if key:
            headers["X-API-Key"] = key
        return self.client.get(path, headers=headers, **kwargs)


@pytest.fixture()
def gw():
    import redis

    from gateway.database.connection import engine
    from gateway.database.models import Base
    from gateway.main import app

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    redis.Redis.from_url(REDIS_URL).flushdb()

    from fastapi.testclient import TestClient

    upstream = FakeUpstream()
    app.state.transport_override = upstream.transport
    with TestClient(app) as client:
        yield Gw(client, upstream)
    app.state.transport_override = None
