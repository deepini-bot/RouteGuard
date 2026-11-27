import secrets

from fastapi import Header, HTTPException

from gateway.config import settings


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Protects every /admin endpoint. Compared in constant time."""
    if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.admin_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")
