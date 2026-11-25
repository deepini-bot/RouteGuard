from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.api.deps import require_admin
from gateway.database.connection import get_db
from gateway.database.models import Route, Service
from gateway.services import audit_service as audit
from gateway.services.health_service import check_services

router = APIRouter(prefix="/admin", tags=["services & routes"], dependencies=[Depends(require_admin)])

HttpMethod = Literal["*", "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _check_url(v: str) -> str:
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    return v.rstrip("/")


def _check_health_path(v: str) -> str:
    if not v.startswith("/"):
        raise ValueError("health_path must start with /")
    return v


def _check_prefix(v: str | None) -> str | None:
    if v is None:
        return v
    v = v.rstrip("/")
    if not v.startswith("/api/"):
        raise ValueError("path_prefix must start with /api/ (the gateway only serves /api/*)")
    return v


def _check_upstream_prefix(v: str | None) -> str | None:
    if v is not None and not v.startswith("/"):
        raise ValueError("upstream_prefix must start with /")
    return v


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str
    health_path: str = "/health"

    _url = field_validator("base_url")(_check_url)
    _hp = field_validator("health_path")(_check_health_path)


class ServicePatch(BaseModel):
    base_url: str | None = None
    status: Literal["active", "disabled"] | None = None
    health_path: str | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, v):
        return _check_url(v) if v is not None else v

    @field_validator("health_path")
    @classmethod
    def _hp(cls, v):
        return _check_health_path(v) if v is not None else v


_Version = Field(default=None, max_length=20, pattern=r"^[A-Za-z0-9._-]+$")


class RouteIn(BaseModel):
    path_prefix: str
    service_id: int
    methods: list[HttpMethod] = ["*"]
    enabled: bool = True
    cache_ttl: int = Field(default=0, ge=0, le=86400)
    version: str | None = _Version
    deprecated: bool = False
    sunset_at: date | None = None
    upstream_prefix: str | None = None

    _prefix = field_validator("path_prefix")(_check_prefix)
    _up = field_validator("upstream_prefix")(_check_upstream_prefix)


class RoutePatch(BaseModel):
    """Send only the fields you want to change. For version, sunset_at and upstream_prefix,
    an explicit null clears the value."""

    path_prefix: str | None = None
    service_id: int | None = None
    methods: list[HttpMethod] | None = None
    enabled: bool | None = None
    cache_ttl: int | None = Field(default=None, ge=0, le=86400)
    version: str | None = _Version
    deprecated: bool | None = None
    sunset_at: date | None = None
    upstream_prefix: str | None = None

    _prefix = field_validator("path_prefix")(_check_prefix)
    _up = field_validator("upstream_prefix")(_check_upstream_prefix)


def _service_out(s: Service) -> dict:
    return {"id": s.id, "name": s.name, "base_url": s.base_url, "status": s.status, "health_path": s.health_path}


def _route_out(r: Route) -> dict:
    return {
        "id": r.id, "path_prefix": r.path_prefix, "service_id": r.service_id,
        "methods": r.methods.split(","), "enabled": r.enabled, "cache_ttl": r.cache_ttl,
        "version": r.version, "deprecated": r.deprecated,
        "sunset_at": r.sunset_at.isoformat() if r.sunset_at else None,
        "upstream_prefix": r.upstream_prefix,
    }


@router.post("/services", status_code=201)
def create_service(request: Request, body: ServiceIn, db: Session = Depends(get_db)):
    if db.scalar(select(Service).where(Service.name == body.name)):
        raise HTTPException(409, "A service with this name already exists")
    service = Service(name=body.name, base_url=body.base_url, health_path=body.health_path)
    db.add(service)
    db.flush()
    audit.record(db, request, "service.create", "service", service.id, {"name": service.name, "base_url": service.base_url})
    db.commit()
    return _service_out(service)


@router.get("/services")
def list_services(db: Session = Depends(get_db)):
    return [_service_out(s) for s in db.scalars(select(Service).order_by(Service.id))]


@router.patch("/services/{service_id}")
def update_service(request: Request, service_id: int, body: ServicePatch, db: Session = Depends(get_db)):
    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(404, "Service not found")
    diff = audit.apply_changes(service, body.model_dump(exclude_none=True))
    if diff:
        audit.record(db, request, "service.update", "service", service.id, diff)
    db.commit()
    return _service_out(service)


@router.post("/routes", status_code=201)
def create_route(request: Request, body: RouteIn, db: Session = Depends(get_db)):
    if db.get(Service, body.service_id) is None:
        raise HTTPException(404, "Service not found")
    if db.scalar(select(Route).where(Route.path_prefix == body.path_prefix)):
        raise HTTPException(409, "A route with this path_prefix already exists")
    route = Route(
        path_prefix=body.path_prefix, service_id=body.service_id, methods=",".join(body.methods),
        enabled=body.enabled, cache_ttl=body.cache_ttl, version=body.version, deprecated=body.deprecated,
        sunset_at=body.sunset_at, upstream_prefix=body.upstream_prefix,
    )
    db.add(route)
    db.flush()
    audit.record(
        db, request, "route.create", "route", route.id,
        {"path_prefix": route.path_prefix, "service_id": route.service_id, "version": route.version},
    )
    db.commit()
    return _route_out(route)


@router.get("/routes")
def list_routes(db: Session = Depends(get_db)):
    return [_route_out(r) for r in db.scalars(select(Route).order_by(Route.id))]


@router.patch("/routes/{route_id}")
def update_route(request: Request, route_id: int, body: RoutePatch, db: Session = Depends(get_db)):
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(404, "Route not found")

    sent = body.model_dump(exclude_unset=True)
    nullable = {"version", "sunset_at", "upstream_prefix"}  # explicit null clears these
    changes = {k: v for k, v in sent.items() if v is not None or k in nullable}

    if "service_id" in changes and db.get(Service, changes["service_id"]) is None:
        raise HTTPException(404, "Service not found")
    if "methods" in changes:
        changes["methods"] = ",".join(changes["methods"])

    diff = audit.apply_changes(route, changes)
    if diff:
        audit.record(db, request, "route.update", "route", route.id, diff)
    db.commit()
    return _route_out(route)


@router.get("/health/backends")
async def backend_health(request: Request):
    """Latest result of the scheduled health checks (empty until the first check has run)."""
    return request.app.state.backend_health


@router.post("/health/backends/check")
async def run_backend_health_check(request: Request):
    """Runs the health checks right now instead of waiting for the next scheduled run."""
    return await check_services(request.app)
