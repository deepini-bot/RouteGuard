from dataclasses import dataclass

from gateway.database.connection import SessionLocal
from gateway.database.models import utcnow
from gateway.errors import GatewayError
from gateway.middleware.authentication import authenticate, is_allowed
from gateway.middleware.quota import QuotaPolicy, resolve_quota
from gateway.middleware.rate_limiter import Policy, resolve_policy
from gateway.services.routing_service import RouteInfo, match_route


@dataclass
class Decision:
    client_id: int
    auth_method: str
    route: RouteInfo
    policy: Policy
    quota: QuotaPolicy | None


def decide(api_key: str | None, authorization: str | None, path: str, method: str) -> Decision:
    """Everything that needs the database, in one go (one session, one thread hop).

    Order matters: authenticate first, so unauthenticated callers can't probe which routes exist.
    """
    with SessionLocal() as db:
        auth = authenticate(db, api_key, authorization)

        route = match_route(db, path, method)
        if route is None:
            raise GatewayError(404, "No route matches this request", "no_route", client_id=auth.client_id)

        if not is_allowed(db, auth.client_id, route.id):
            raise GatewayError(
                403, "This client is not allowed to access this route", "forbidden",
                client_id=auth.client_id, route_id=route.id, service_name=route.service_name,
            )

        # A retired API version: the route still exists so clients get a clear message, not a 404
        if route.sunset_at is not None and utcnow().date() >= route.sunset_at:
            raise GatewayError(
                410, f"This API version was retired on {route.sunset_at.isoformat()}", "sunset",
                client_id=auth.client_id, route_id=route.id, service_name=route.service_name,
            )

        policy = resolve_policy(db, auth.client_id, route.id)
        quota = resolve_quota(db, auth.client_id, route.id)
        return Decision(auth.client_id, auth.method, route, policy, quota)
