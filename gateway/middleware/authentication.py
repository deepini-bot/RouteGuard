import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.config import settings
from gateway.database.models import ApiKey, Client, ClientPermission, utcnow
from gateway.errors import GatewayError


def hash_key(raw_key: str) -> str:
    # API keys are long random strings, so a plain SHA-256 is enough here.
    # (Slow hashes like bcrypt are for human-chosen passwords.)
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_key() -> tuple[str, str]:
    """Returns (raw_key, key_hash). The raw key is shown to the client once and never stored."""
    raw = "gw_" + secrets.token_urlsafe(32)
    return raw, hash_key(raw)


def create_jwt(client_id: int, expires_minutes: int = 60) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    return jwt.encode({"sub": str(client_id), "exp": exp}, settings.jwt_secret, algorithm="HS256")


@dataclass
class AuthResult:
    client_id: int
    method: str  # "api_key" or "jwt"


def _load_active_client(db: Session, client_id: int) -> Client:
    client = db.get(Client, client_id)
    if client is None:
        raise GatewayError(401, "Unknown client", "auth")
    if client.status != "active":
        raise GatewayError(403, "Client is disabled", "forbidden", client_id=client.id)
    return client


def authenticate(db: Session, api_key: str | None, authorization: str | None) -> AuthResult:
    """Answers: who is calling? Raises GatewayError(401/403) if we can't tell or they are blocked."""
    if api_key:
        key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(api_key)))
        if key is None or key.status != "active":
            raise GatewayError(401, "Invalid or revoked API key", "auth")
        if key.expires_at is not None and key.expires_at < utcnow():
            raise GatewayError(401, "API key has expired", "auth")
        client = _load_active_client(db, key.client_id)

        # Update last_used_at at most once a minute, so we don't write to the DB on every request
        now = utcnow()
        if key.last_used_at is None or (now - key.last_used_at).total_seconds() > 60:
            key.last_used_at = now
            db.commit()
        return AuthResult(client.id, "api_key")

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        try:
            claims = jwt.decode(
                token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
            )
            client_id = int(claims["sub"])
        except (jwt.PyJWTError, ValueError):
            raise GatewayError(401, "Invalid or expired token", "auth")
        client = _load_active_client(db, client_id)
        return AuthResult(client.id, "jwt")

    raise GatewayError(
        401, "Missing credentials: send X-API-Key or Authorization: Bearer <token>", "auth",
        headers={"WWW-Authenticate": "ApiKey, Bearer"},
    )


def is_allowed(db: Session, client_id: int, route_id: int) -> bool:
    """Answers: is this client allowed to call this route?"""
    row = db.get(ClientPermission, (client_id, route_id))
    return row is not None
