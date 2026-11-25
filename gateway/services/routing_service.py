from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from email.utils import format_datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.database.models import Route, Service


@dataclass
class RouteInfo:
    """A plain snapshot of a route + its service (safe to use after the DB session closes)."""

    id: int
    path_prefix: str
    service_id: int
    service_name: str
    base_url: str
    cache_ttl: int
    version: str | None = None
    deprecated: bool = False
    sunset_at: date | None = None
    upstream_prefix: str | None = None

    def upstream_path(self, public_path: str) -> str:
        """The path to call on the backend. Swaps the public prefix for upstream_prefix if one is set."""
        if self.upstream_prefix is None:
            return public_path
        rest = public_path[len(self.path_prefix.rstrip("/")):]
        return (self.upstream_prefix.rstrip("/") + rest) or "/"


def version_headers(route: RouteInfo) -> dict[str, str]:
    """Headers that tell the client which API version it used and whether it is going away."""
    headers: dict[str, str] = {}
    if route.version:
        headers["X-API-Version"] = route.version
    if route.deprecated:
        headers["Deprecation"] = "true"
    if route.sunset_at:
        when = datetime.combine(route.sunset_at, time.min, tzinfo=timezone.utc)
        headers["Sunset"] = format_datetime(when, usegmt=True)
    return headers


def match_route(db: Session, path: str, method: str) -> RouteInfo | None:
    """Longest matching path prefix wins. /api/products/101 matches /api/products.

    Routes live in the database and are read on every request,
    so admin changes take effect immediately with no restart.
    """
    rows = db.execute(
        select(Route, Service)
        .join(Service, Route.service_id == Service.id)
        .where(Route.enabled.is_(True), Service.status == "active")
    ).all()

    best: tuple[Route, Service] | None = None
    for route, service in rows:
        prefix = route.path_prefix.rstrip("/")
        if not (path == prefix or path.startswith(prefix + "/")):
            continue
        allowed = {m.strip().upper() for m in route.methods.split(",")}
        if "*" not in allowed and method.upper() not in allowed:
            continue
        if best is None or len(prefix) > len(best[0].path_prefix.rstrip("/")):
            best = (route, service)

    if best is None:
        return None
    route, service = best
    return RouteInfo(
        route.id, route.path_prefix, service.id, service.name, service.base_url, route.cache_ttl,
        route.version, route.deprecated, route.sunset_at, route.upstream_prefix,
    )
