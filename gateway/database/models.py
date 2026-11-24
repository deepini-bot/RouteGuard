from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Naive UTC datetime (works the same on SQLite and MySQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))  # first few chars, so keys are recognisable in lists
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex, raw key is never stored
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | inactive | revoked
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | disabled
    health_path: Mapped[str] = mapped_column(String(255), default="/health")  # polled by the health checker


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path_prefix: Mapped[str] = mapped_column(String(255), unique=True)  # public path, e.g. /api/v2/products
    methods: Mapped[str] = mapped_column(String(100), default="*")  # "*" or "GET,POST"
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cache_ttl: Mapped[int] = mapped_column(Integer, default=0)  # seconds, 0 = no caching (GET only)

    # API versioning
    version: Mapped[str | None] = mapped_column(String(20), nullable=True)  # label such as "v1"
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    sunset_at: Mapped[date | None] = mapped_column(Date, nullable=True)  # from this day the route returns 410 Gone
    # If set, the matched path_prefix is replaced by this when calling the backend
    # (so /api/v1/products/5 can reach a backend that only knows /api/products/5)
    upstream_prefix: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ClientPermission(Base):
    """Which routes a client may call. No rows = the client can't call anything."""

    __tablename__ = "client_permissions"

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), primary_key=True, autoincrement=False)
    route_id: Mapped[int] = mapped_column(ForeignKey("routes.id"), primary_key=True, autoincrement=False)


class RateLimitConfig(Base):
    __tablename__ = "rate_limit_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    route_id: Mapped[int | None] = mapped_column(ForeignKey("routes.id"), nullable=True)
    algorithm: Mapped[str] = mapped_column(String(30), default="token_bucket")
    limit: Mapped[int] = mapped_column(Integer)
    window_seconds: Mapped[int] = mapped_column(Integer, default=60)
    burst: Mapped[int | None] = mapped_column(Integer, nullable=True)  # token bucket capacity
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Quota(Base):
    """A long-term cap (per day or per month) on top of the short rate limit."""

    __tablename__ = "quotas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    route_id: Mapped[int | None] = mapped_column(ForeignKey("routes.id"), nullable=True)
    period: Mapped[str] = mapped_column(String(10))  # day | month (UTC)
    limit: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RequestLog(Base):
    __tablename__ = "request_logs"
    __table_args__ = (Index("ix_request_logs_client_created", "client_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    service_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(500))
    status_code: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class UsageDaily(Base):
    """One row per day / client / route, built from request_logs by the rollup job.

    client_id and route_id use 0 for "none" (e.g. a request with a bad API key),
    so they can be part of the primary key.
    """

    __tablename__ = "usage_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=0)
    route_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=0)
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    blocked_requests: Mapped[int] = mapped_column(Integer, default=0)
    error_requests: Mapped[int] = mapped_column(Integer, default=0)  # status >= 500
    forwarded_requests: Mapped[int] = mapped_column(Integer, default=0)  # reached a backend
    total_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)  # sum over forwarded requests


class AuditLog(Base):
    """Who changed what in the admin API. Raw API keys and secrets are never stored here."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(50), index=True)  # e.g. key.revoke
    resource_type: Mapped[str] = mapped_column(String(30), index=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
