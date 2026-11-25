"""The demo backend services (payment idempotency and product v2), tested on their own."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend-services"))


def test_payment_service_replays_same_idempotency_key():
    from payment_service.main import BY_IDEMPOTENCY_KEY, PAYMENTS, app

    PAYMENTS.clear()
    BY_IDEMPOTENCY_KEY.clear()
    c = TestClient(app)
    body = {"order_id": 1, "amount": 499.0}

    first = c.post("/api/payments", json=body, headers={"Idempotency-Key": "abc"})
    second = c.post("/api/payments", json=body, headers={"Idempotency-Key": "abc"})
    assert first.status_code == 201
    assert second.status_code == 200 and second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert len(PAYMENTS) == 1  # charged once, not twice

    c.post("/api/payments", json=body)  # no key: a new payment
    assert len(PAYMENTS) == 2

    assert c.post("/api/payments", json={"order_id": 1, "amount": 0}).status_code == 422
    assert c.get(f"/api/payments/{first.json()['id']}").json()["status"] == "succeeded"
    assert c.get("/api/payments/999").status_code == 404


def test_product_service_v2_has_a_different_shape():
    from product_service.main import app

    c = TestClient(app)
    v1 = c.get("/api/products/101").json()
    v2 = c.get("/api/v2/products/101").json()
    assert v1["price"] == 3499.0
    assert v2["version"] == "v2" and v2["price"] == {"amount": 3499.0, "currency": "INR"}
    assert c.get("/api/v2/products/999").status_code == 404
    assert len(c.get("/api/v2/products").json()["items"]) >= 3


def test_gateway_forwards_idempotency_key_to_payment_backend(gw):
    s = gw.bootstrap()
    svc = gw.admin("POST", "/admin/services", json={"name": "payment", "base_url": "http://payment-svc:8004"}).json()
    route = gw.admin("POST", "/admin/routes", json={"path_prefix": "/api/payments", "service_id": svc["id"]}).json()
    gw.admin("PUT", f"/admin/clients/{s['client_id']}/permissions", json={"route_ids": [route["id"]]})

    r = gw.client.post(
        "/api/payments", json={"order_id": 1, "amount": 5},
        headers={"X-API-Key": s["key"], "Idempotency-Key": "pay-123"},
    )
    assert r.status_code == 200
    assert r.json()["headers"]["idempotency-key"] == "pay-123"
