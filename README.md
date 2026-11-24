# API Gateway & Rate Limiting Platform

A configurable API gateway built with FastAPI. Clients send every request to the gateway, and the gateway decides whether it is allowed and which backend service should handle it.

It handles:

- **Authentication**: API keys (stored hashed) and JWT
- **Authorization**: each client can only call the routes it has been given
- **Rate limiting**: Token Bucket and Sliding Window, shared across workers through Redis, plus a per-IP limit
- **Quotas**: daily or monthly caps per client and/or route
- **Routing**: services and routes are stored in the database and can be changed at runtime
- **API versioning**: versioned routes, path rewriting, deprecation and sunset dates
- **Reverse proxy**: timeouts, controlled 502/503/504 errors, a circuit breaker, safe retries
- **Caching**: optional per-route response caching in Redis
- **Usage tracking**: every request is logged, rolled up into a daily table, and shown in analytics APIs and a dashboard
- **Audit log**: every change made through the admin API is recorded
- **Health checks**: backends are checked on a schedule, and the results feed the circuit breakers

```
Client ──► Gateway (:8000) ──► User Service     (:8001)
              │          ├──► Product Service  (:8002)
              │          ├──► Order Service    (:8003)
              │          └──► Payment Service  (:8004)
              ├── Redis  : rate limit state, quota counters, response cache
              └── MySQL  : clients, keys, routes, policies, request logs, usage_daily, audit logs
```

## How a request flows

For `GET /api/products/101` with `X-API-Key: gw_...`:

1. **IP limit.** Count the request against the caller's IP (**429** if over). This runs first, so floods and key guessing are stopped before any database work.
2. Find the API key by its hash. Reject with **401** if it is unknown, revoked, inactive or expired.
3. Check the client is active (**403** if disabled).
4. Find the route with the longest matching prefix (**404** if none).
5. Check the client has permission for that route (**403** if not).
6. If the route's version has been retired (past its sunset date), return **410 Gone**.
7. Pick the rate limit policy (client + route, then client, then route, then default) and check it in Redis (**429** with `Retry-After`).
8. Check the daily/monthly quota, if one applies (**429** with `X-Quota-*` headers).
9. If the route has caching on and there is a cached copy, return it.
10. Check the circuit breaker for that backend (**503** if open).
11. Forward the request, rewriting the path if the route asks for it. A backend that is down gives **502**, a slow one gives **504**.
12. Return the response (with version headers if the route is versioned) and write a usage record in the background.

Steps 1 to 8 happen before anything is sent to the backend, so rejected requests never reach it.

## Run it

### Option A: Docker Compose (gateway + MySQL + Redis + 4 demo services)

```bash
docker compose up --build
python scripts/seed.py --docker      # creates demo services, routes, clients and keys
```

### Option B: without Docker (SQLite + local Redis)

```bash
pip install -r requirements.txt
redis-server &                                                          # or any Redis on :6379

(cd backend-services && uvicorn user_service.main:app    --port 8001 &)
(cd backend-services && uvicorn product_service.main:app --port 8002 &)
(cd backend-services && uvicorn order_service.main:app   --port 8003 &)
(cd backend-services && uvicorn payment_service.main:app --port 8004 &)

uvicorn gateway.main:app --port 8000 &
python scripts/seed.py
```

The seed script prints two API keys. Then open:

- http://localhost:8000/dashboard : usage dashboard (type the admin key, `dev-admin-key` by default)
- http://localhost:8000/docs : interactive docs for the admin endpoints

> If you ran an earlier version of this project with SQLite, delete `gateway.db` first. The tables gained new columns, and `create_all` does not alter existing tables.

## Try the demo scenarios

```bash
KEY=gw_...                 # printed by seed.py (shop-app)
ADMIN="X-Admin-Key: dev-admin-key"
```

**1. Routing works**
```bash
curl -H "X-API-Key: $KEY" localhost:8000/api/products/101
```

**2. Invalid key is rejected** (the backend never sees it)
```bash
curl -i -H "X-API-Key: wrong" localhost:8000/api/products/101      # 401
```

**3. Rate limiting.** Set a limit of 5 per minute, send 10 requests.
```bash
curl -X POST localhost:8000/admin/rate-limits -H "$ADMIN" -H 'content-type: application/json' \
  -d '{"client_id": 1, "limit": 5, "window_seconds": 60}'
for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} " -H "X-API-Key: $KEY" localhost:8000/api/users/1; done
# 200 200 200 200 200 429 429 429 429 429
```

**4. Concurrent requests**
```bash
python scripts/load_test.py --key $KEY --path /api/users/1 --requests 100 --concurrency 50
```

**5. Backend failure.** Stop the product service, then call it. You get `502`, and the gateway keeps running. After a few failures (or a few failed health checks) the circuit opens and you get an instant `503`. Start the service again and it recovers on its own.

**6. Revoke a key**
```bash
curl -X PATCH localhost:8000/admin/keys/1 -H "$ADMIN" -H 'content-type: application/json' -d '{"status":"revoked"}'
curl -i -H "X-API-Key: $KEY" localhost:8000/api/users/1            # 401
```

**7. Change a route without touching code**
```bash
curl -X PATCH localhost:8000/admin/services/2 -H "$ADMIN" -H 'content-type: application/json' \
  -d '{"base_url": "http://localhost:9002"}'
```

**8. API versions.** The seed creates `/api/v1/products` (deprecated, with a sunset date, rewritten to the backend's `/api/products`) and `/api/v2/products` (a different response shape).
```bash
curl -i -H "X-API-Key: $KEY" localhost:8000/api/v1/products/101   # Deprecation: true, Sunset: <date>, X-API-Version: v1
curl -i -H "X-API-Key: $KEY" localhost:8000/api/v2/products/101   # new shape, X-API-Version: v2
```

**9. Daily quota**
```bash
curl -X POST localhost:8000/admin/quotas -H "$ADMIN" -H 'content-type: application/json' \
  -d '{"client_id": 1, "period": "day", "limit": 3}'
# the 4th request that day gets 429 with X-Quota-Limit / X-Quota-Remaining / Retry-After
```

**10. Payments are never retried by the gateway**, and the payment service supports an `Idempotency-Key`:
```bash
curl -X POST localhost:8000/api/payments -H "X-API-Key: $FULL_KEY" -H "Idempotency-Key: k1" \
  -H 'content-type: application/json' -d '{"order_id":1,"amount":499}'     # 201, charged
# run the same command again: 200 with "Idempotent-Replay: true", not charged twice
```

**Analytics, audit log, health**
```bash
curl -H "$ADMIN" localhost:8000/admin/analytics/overview
curl -H "$ADMIN" localhost:8000/admin/analytics/daily?days=7
curl -H "$ADMIN" "localhost:8000/admin/audit-logs?action=key.revoke"
curl localhost:8000/health
```

## Admin API

All `/admin/*` endpoints need the `X-Admin-Key` header.

| Area | Endpoints |
|---|---|
| Clients | `POST/GET /admin/clients`, `PATCH /admin/clients/{id}` |
| API keys | `POST/GET /admin/clients/{id}/keys`, `PATCH /admin/keys/{id}` (active / inactive / revoked) |
| Permissions | `GET/PUT /admin/clients/{id}/permissions` |
| JWT | `POST /admin/clients/{id}/token` |
| Services | `POST/GET /admin/services`, `PATCH /admin/services/{id}` (includes `health_path`) |
| Routes | `POST/GET /admin/routes`, `PATCH /admin/routes/{id}` (includes `version`, `deprecated`, `sunset_at`, `upstream_prefix`, `cache_ttl`) |
| Rate limits | `POST/GET /admin/rate-limits`, `PATCH /admin/rate-limits/{id}` |
| Quotas | `POST/GET /admin/quotas`, `PATCH /admin/quotas/{id}` |
| Analytics | `GET /admin/analytics/overview`, `/clients/{id}`, `/routes`, `/daily` ; `POST /admin/analytics/rollup` |
| Audit log | `GET /admin/audit-logs` (filter by `action`, `resource_type`, `resource_id`) |
| Backend health | `GET /admin/health/backends`, `POST /admin/health/backends/check` |
| Public | `GET /health` (gateway, Redis, circuit states, backend health), `GET /dashboard` |

## The newer features in more detail

### IP-based rate limiting
A token bucket per caller IP, checked before authentication. It is on by default at 300 requests per minute (`IP_RATE_LIMIT`, `IP_RATE_WINDOW`; set the limit to 0 to turn it off). Because it runs before the database lookup, a flood of requests with bad API keys is rejected cheaply. Only `/api/*` is covered, so `/admin`, `/health` and `/dashboard` are not limited.

By default the gateway uses the real connection address and **ignores** `X-Forwarded-For`, because otherwise anyone could fake the header to get a fresh allowance. If the gateway sits behind a load balancer you control, set `TRUST_PROXY_HEADERS=true` and the first address in `X-Forwarded-For` is used. The backend always receives a `X-Forwarded-For` value set by the gateway, never the caller's own.

### Quotas
A rate limit is about short bursts (per minute). A quota is a longer cap, per UTC **day** or **month**, for a client, a route, or both (the most specific one wins, same as rate limits). Counters live in Redis and are updated by a Lua script that checks the limit before counting, so rejected requests do not use up quota. Responses carry `X-Quota-Limit`, `X-Quota-Remaining` and `X-Quota-Period`, and a rejected request gets `Retry-After` set to the seconds until the period resets. Quota is checked after the rate limit, so a request rejected by the rate limit does not cost quota.

### API versioning
Versions are separate routes (`/api/v1/products`, `/api/v2/products`), so each can have its own service, permissions, rate limits and cache setting. A route can also have:

- `version`: a label such as `v1`, returned in `X-API-Version`
- `upstream_prefix`: the public prefix is swapped for this when calling the backend, so `/api/v1/products/5` can reach a backend that only has `/api/products/5`
- `deprecated`: adds `Deprecation: true`
- `sunset_at`: adds a `Sunset` header, and from that date the route returns **410 Gone** with a clear message instead of a confusing 404. Push the date back or clear it with a PATCH (`"sunset_at": null`).

### Audit log
Every successful change made through the admin API (clients, keys, permissions, tokens, services, routes, rate limits, quotas, rollups) writes a row with the action, the resource, what changed (`{"from": ..., "to": ...}`), the caller's IP and the time. The audit row is saved in the same database transaction as the change, so you cannot get one without the other. Raw API keys and tokens are never stored; for keys only the first 8 characters are logged, so a key can still be identified. Requests that fail (404, 409, 422) or change nothing are not logged.

Because there is one shared admin key, the log records *what* happened and *from which IP*, but not *which person*. Per-user admin accounts would be the next step.

### usage_daily rollup
A background job rebuilds the `usage_daily` table (one row per day, client and route) from `request_logs`. It replaces the day's rows each time, so running it repeatedly never double counts. It refreshes today and yesterday every `USAGE_ROLLUP_INTERVAL` seconds (default 300), and only one gateway worker runs it per interval (Redis lock). `GET /admin/analytics/daily` reads from this table, and `POST /admin/analytics/rollup?day=YYYY-MM-DD` runs it by hand.

Set `LOG_RETENTION_DAYS` (default 0 = keep everything) to delete old `request_logs` rows. Before deleting, the job rolls up those days first, so the daily totals stay even after the raw rows are gone.

### Scheduled health checks
Every `HEALTH_CHECK_INTERVAL` seconds (default 15) the gateway calls each active service's health URL (`base_url` + `health_path`, default `/health`). A 5xx, a timeout or a refused connection marks the service **down**. The results show in `GET /health`, in `/admin/health/backends`, and on the dashboard. They also feed the circuit breakers: failed checks count towards opening a circuit, and a good check closes an open one straight away, so a backend that comes back is used again without waiting for a real request to test it.

### Dashboard
`/dashboard` is one HTML file with no build step and no external libraries. It shows total and blocked requests, average response time, backend error rate, requests per minute, the last 7 days (from `usage_daily`), backend health and circuit state, block reasons, per-route stats and recent admin changes. It refreshes by itself. The page is public but contains no data; everything it shows comes from `/admin` endpoints that need the admin key, which you type into the page (kept in `sessionStorage` for the tab only).

## Design decisions worth knowing

**Rate limiting is atomic.** Each check is one Lua script that runs inside Redis. If 50 requests arrive at the same time, Redis runs them one after another, so two requests can never spend the same token. The script also reads the time from Redis, so gateway workers with different clocks still agree. `tests/test_algorithms.py` fires 50 concurrent checks at a bucket of 10 and checks that exactly 10 pass. The quota script works the same way.

**Token Bucket vs Sliding Window.** Token Bucket allows a burst (up to `burst`) and then refills at a steady rate, and it only stores two values per client. Sliding Window is exact (no more than N requests in any window of that length) but stores one entry per request. Choose per policy with the `algorithm` field.

**API keys are stored as SHA-256 hashes.** The raw key is shown once, when it is created. Keys are long random strings, so a fast hash is fine here (slow hashes like bcrypt are for passwords people choose themselves).

**Retries are limited on purpose.** The gateway retries once, and only when the method is idempotent (GET, HEAD, OPTIONS) and the connection was refused, meaning the request never reached the backend. POST is never retried automatically, because a retry after a timeout could create a duplicate order or payment. The payment service shows the proper fix: clients send an `Idempotency-Key`, and the service returns the same payment instead of charging twice. The gateway passes that header through.

**Circuit breaker.** After 5 failures in a row (connection errors, timeouts, 5xx) a backend is marked open and requests get an immediate 503 instead of waiting for timeouts. After 30 seconds one trial request is let through. Both numbers are configurable.

**Gateway credentials are not forwarded.** `X-API-Key`, `Authorization` and `Cookie` are removed before the request goes to the backend. The backend gets `X-Client-Id` and `X-Request-Id` instead.

**Cache runs after auth, rate limiting and quota.** A cache hit still counts against the client's limits, and an unauthenticated request can never read from the cache. Cached responses are shared across clients, so only turn on caching for routes whose responses are the same for everyone.

**Redis down?** By default the gateway lets requests through (fail open) and logs the error. Set `RATE_LIMIT_FAIL_OPEN=false` to reject with 503 instead. This covers the IP limit, rate limits and quotas.

**Logging does not slow requests down.** Usage records are written in a background task after the response is sent. A failed log write is logged and ignored.

## Configuration

Environment variables (defaults are for local development, so change the keys in real use):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./gateway.db` | `mysql+pymysql://user:pass@host:3306/db` for MySQL |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `ADMIN_KEY` | `dev-admin-key` | protects `/admin/*` |
| `JWT_SECRET` | dev value | signs and verifies JWTs (HS256) |
| `DEFAULT_RATE_LIMIT` / `DEFAULT_RATE_WINDOW` | `100` / `60` | used when no policy matches |
| `IP_RATE_LIMIT` / `IP_RATE_WINDOW` | `300` / `60` | per-IP limit (0 = off) |
| `TRUST_PROXY_HEADERS` | `false` | believe `X-Forwarded-For` (only behind your own proxy) |
| `UPSTREAM_TIMEOUT` | `5` | seconds to wait for a backend |
| `CB_FAILURE_THRESHOLD` / `CB_RESET_SECONDS` | `5` / `30` | circuit breaker |
| `HEALTH_CHECK_INTERVAL` / `HEALTH_CHECK_TIMEOUT` | `15` / `2` | seconds (interval 0 = off) |
| `USAGE_ROLLUP_INTERVAL` | `300` | seconds between rollups (0 = off) |
| `LOG_RETENTION_DAYS` | `0` | delete older request logs after rollup (0 = keep) |
| `RATE_LIMIT_FAIL_OPEN` | `true` | behaviour when Redis is unavailable |

## Tests

```bash
python -m pytest
```

100 tests. They start a temporary `redis-server` automatically (or use `TEST_REDIS_URL` if set, which is what CI does). Backend services are replaced by a fake transport, so tests can simulate a backend being down, slow or returning 500.

- `test_algorithms.py`: both algorithms, refill, bursts, concurrency
- `test_circuit_breaker.py`: open / half-open / close behaviour
- `test_gateway.py`: the 7 original demo scenarios, plus JWT, permissions, caching, retries, admin validation and analytics
- `test_features.py`: IP limit (including fake `X-Forwarded-For`), quotas (including under concurrency), versioning and sunset, audit log (including "no secrets stored"), rollup and pruning, health checks and circuit integration, dashboard
- `test_backend_services.py`: payment idempotency, product v2, header pass-through

GitHub Actions (`.github/workflows/ci.yml`) runs the tests with a Redis service and checks that both Docker images build.

## Project structure

```
gateway/
  main.py                 app setup, background jobs, /health, /dashboard, the catch-all /api/* handler
  config.py               settings from environment variables
  errors.py               GatewayError (status, message, reason for the log)
  utils.py                client_ip()
  algorithms/             token_bucket.py, sliding_window.py (Redis Lua)
  middleware/             authentication.py, rate_limiter.py (incl. IP limit), quota.py, request_logger.py
  routing/                proxy.py (forwarding, cache, retries), circuit_breaker.py
  services/               decision.py (auth + route + permission + policy + quota in one DB pass),
                          routing_service.py (matching, version headers), api_key_service.py,
                          analytics_service.py, usage_service.py (rollup, prune),
                          audit_service.py, health_service.py
  api/                    admin endpoints: clients, routes, rate_limits (+quotas), analytics, audit
  database/               models.py, connection.py
  dashboard/index.html    the dashboard page
backend-services/         four small demo FastAPI services (user, product, order, payment)
scripts/                  seed.py, load_test.py
tests/
```

## Known limits

- Circuit breaker state and backend health are kept in memory per gateway process. With several workers, each one has its own view. Moving them to Redis would fix that.
- Request and response bodies are buffered in memory, not streamed, so it is not meant for very large uploads or downloads.
- Schema is created with `create_all` on startup, which does not change existing tables. A real deployment would use Alembic migrations.
- The admin API uses one shared key, so the audit log shows the IP but not which person made a change.
- Quota periods are UTC days and months, and the quota script uses the gateway's clock (not Redis's) to pick the period.
- The usage rollup groups by client and route. Per-IP or per-status breakdowns would need extra columns.
- Not covered: per-route IP limits, header-based API versioning (`Accept-Version`), and alerting when a backend goes down.
