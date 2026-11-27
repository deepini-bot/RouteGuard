from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.api.deps import require_admin
from gateway.database.connection import get_db
from gateway.database.models import Client, Quota, RateLimitConfig, Route
from gateway.services import audit_service as audit

router = APIRouter(prefix="/admin", tags=["rate limits & quotas"], dependencies=[Depends(require_admin)])

Algorithm = Literal["token_bucket", "sliding_window"]


class RateLimitIn(BaseModel):
    client_id: int | None = None
    route_id: int | None = None
    algorithm: Algorithm = "token_bucket"
    limit: int = Field(gt=0, le=1_000_000)
    window_seconds: int = Field(default=60, gt=0, le=86400)
    burst: int | None = Field(default=None, gt=0, le=1_000_000)  # token bucket only
    enabled: bool = True


class RateLimitPatch(BaseModel):
    algorithm: Algorithm | None = None
    limit: int | None = Field(default=None, gt=0, le=1_000_000)
    window_seconds: int | None = Field(default=None, gt=0, le=86400)
    burst: int | None = Field(default=None, gt=0, le=1_000_000)
    enabled: bool | None = None


class QuotaIn(BaseModel):
    client_id: int | None = None
    route_id: int | None = None
    period: Literal["day", "month"]
    limit: int = Field(gt=0, le=1_000_000_000)
    enabled: bool = True


class QuotaPatch(BaseModel):
    limit: int | None = Field(default=None, gt=0, le=1_000_000_000)
    period: Literal["day", "month"] | None = None
    enabled: bool | None = None


def _out(c: RateLimitConfig) -> dict:
    return {
        "id": c.id, "client_id": c.client_id, "route_id": c.route_id, "algorithm": c.algorithm,
        "limit": c.limit, "window_seconds": c.window_seconds, "burst": c.burst, "enabled": c.enabled,
    }


def _quota_out(q: Quota) -> dict:
    return {
        "id": q.id, "client_id": q.client_id, "route_id": q.route_id,
        "period": q.period, "limit": q.limit, "enabled": q.enabled,
    }


def _check_targets(db: Session, client_id: int | None, route_id: int | None) -> None:
    if client_id is None and route_id is None:
        raise HTTPException(422, "Set client_id, route_id, or both.")
    if client_id is not None and db.get(Client, client_id) is None:
        raise HTTPException(404, "Client not found")
    if route_id is not None and db.get(Route, route_id) is None:
        raise HTTPException(404, "Route not found")


@router.post("/rate-limits", status_code=201)
def create_rate_limit(request: Request, body: RateLimitIn, db: Session = Depends(get_db)):
    _check_targets(db, body.client_id, body.route_id)
    cfg = RateLimitConfig(**body.model_dump())
    db.add(cfg)
    db.flush()
    audit.record(db, request, "rate_limit.create", "rate_limit", cfg.id, body.model_dump())
    db.commit()
    return _out(cfg)


@router.get("/rate-limits")
def list_rate_limits(db: Session = Depends(get_db)):
    return [_out(c) for c in db.scalars(select(RateLimitConfig).order_by(RateLimitConfig.id))]


@router.patch("/rate-limits/{config_id}")
def update_rate_limit(request: Request, config_id: int, body: RateLimitPatch, db: Session = Depends(get_db)):
    cfg = db.get(RateLimitConfig, config_id)
    if cfg is None:
        raise HTTPException(404, "Rate limit policy not found")
    diff = audit.apply_changes(cfg, body.model_dump(exclude_unset=True))
    if diff:
        audit.record(db, request, "rate_limit.update", "rate_limit", cfg.id, diff)
    db.commit()
    return _out(cfg)


@router.post("/quotas", status_code=201)
def create_quota(request: Request, body: QuotaIn, db: Session = Depends(get_db)):
    """A daily or monthly cap. Set client_id, route_id, or both (the more specific quota wins)."""
    _check_targets(db, body.client_id, body.route_id)
    quota = Quota(**body.model_dump())
    db.add(quota)
    db.flush()
    audit.record(db, request, "quota.create", "quota", quota.id, body.model_dump())
    db.commit()
    return _quota_out(quota)


@router.get("/quotas")
def list_quotas(db: Session = Depends(get_db)):
    return [_quota_out(q) for q in db.scalars(select(Quota).order_by(Quota.id))]


@router.patch("/quotas/{quota_id}")
def update_quota(request: Request, quota_id: int, body: QuotaPatch, db: Session = Depends(get_db)):
    quota = db.get(Quota, quota_id)
    if quota is None:
        raise HTTPException(404, "Quota not found")
    diff = audit.apply_changes(quota, body.model_dump(exclude_unset=True))
    if diff:
        audit.record(db, request, "quota.update", "quota", quota.id, diff)
    db.commit()
    return _quota_out(quota)
