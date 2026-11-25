"""Sets up demo data through the admin API: 4 services, 6 routes (incl. v1/v2 products),
2 clients with API keys, rate limits and a daily quota.

Safe to run more than once. Things that already exist are reused, and a fresh API key
is printed each time (old keys keep working).

    python scripts/seed.py                 # backends on localhost:8001-8004
    python scripts/seed.py --docker        # backends reached by docker-compose service names
"""
import argparse
import sys

import httpx


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--gateway", default="http://localhost:8000")
    p.add_argument("--admin-key", default="dev-admin-key")
    p.add_argument("--docker", action="store_true", help="use docker-compose hostnames for the backends")
    args = p.parse_args()

    names = ["user", "product", "order", "payment"]
    ports = {"user": 8001, "product": 8002, "order": 8003, "payment": 8004}
    hosts = {n: (f"http://{n}-service:{ports[n]}" if args.docker else f"http://localhost:{ports[n]}") for n in names}
    http = httpx.Client(base_url=args.gateway, headers={"X-Admin-Key": args.admin_key}, timeout=10)

    def create_or_find(create_path: str, body: dict, list_path: str, match) -> dict:
        r = http.post(create_path, json=body)
        if r.status_code == 201:
            return r.json()
        if r.status_code == 409:
            return next(x for x in http.get(list_path).json() if match(x))
        sys.exit(f"{create_path} failed: {r.status_code} {r.text}")

    services = {}
    for n in names:
        svc = create_or_find(
            "/admin/services", {"name": f"{n}-service", "base_url": hosts[n]},
            "/admin/services", lambda s, n=n: s["name"] == f"{n}-service",
        )
        http.patch(f"/admin/services/{svc['id']}", json={"base_url": hosts[n]})  # fresh if re-run with --docker
        services[n] = svc

    # key -> (service, public path, extra route settings)
    route_specs = {
        "products":    ("product", "/api/products", {"cache_ttl": 30}),
        # versioned: v1 is deprecated and has a sunset date. It reuses the backend's /api/products
        "products_v1": ("product", "/api/v1/products", {
            "version": "v1", "deprecated": True, "sunset_at": "2027-06-30", "upstream_prefix": "/api/products"}),
        "products_v2": ("product", "/api/v2/products", {"version": "v2"}),
        "users":       ("user", "/api/users", {}),
        "orders":      ("order", "/api/orders", {}),
        "payments":    ("payment", "/api/payments", {}),
    }
    routes = {}
    for key, (svc, prefix, extra) in route_specs.items():
        route = create_or_find(
            "/admin/routes", {"path_prefix": prefix, "service_id": services[svc]["id"], **extra},
            "/admin/routes", lambda r, pf=prefix: r["path_prefix"] == pf,
        )
        http.patch(f"/admin/routes/{route['id']}", json=extra)  # keep settings fresh on re-runs
        routes[key] = route

    def make_client(name: str, route_keys: list[str], rate_limit: int, daily_quota: int) -> str:
        client = create_or_find("/admin/clients", {"name": name}, "/admin/clients", lambda c: c["name"] == name)
        http.put(f"/admin/clients/{client['id']}/permissions", json={"route_ids": [routes[k]["id"] for k in route_keys]})

        def has(path: str) -> bool:
            return any(x["client_id"] == client["id"] and x["route_id"] is None for x in http.get(path).json())

        if not has("/admin/rate-limits"):
            http.post("/admin/rate-limits", json={"client_id": client["id"], "limit": rate_limit, "window_seconds": 60})
        if not has("/admin/quotas"):
            http.post("/admin/quotas", json={"client_id": client["id"], "period": "day", "limit": daily_quota})
        return http.post(f"/admin/clients/{client['id']}/keys").json()["api_key"]

    shop_key = make_client("shop-app", ["products", "products_v1", "products_v2", "users"], 100, 5000)
    full_key = make_client("full-access-app", list(routes), 50, 1000)

    print("Seeded.\n")
    print(f"shop-app         (products incl. v1/v2, users; 100 req/min, 5000/day):  {shop_key}")
    print(f"full-access-app  (all routes incl. orders + payments; 50 req/min):      {full_key}")
    print(f"\ntry:  curl -i -H 'X-API-Key: {shop_key}' {args.gateway}/api/v1/products/101")
    print(f"      curl -i -H 'X-API-Key: {shop_key}' {args.gateway}/api/v2/products/101")
    print(f"      open {args.gateway}/dashboard   (admin key: {args.admin_key})")


if __name__ == "__main__":
    main()
