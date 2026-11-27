from datetime import timedelta

from sqlalchemy.orm import Session

from gateway.database.models import ApiKey, utcnow
from gateway.middleware.authentication import generate_key


def create_api_key(db: Session, client_id: int, expires_in_days: int | None = None) -> tuple[ApiKey, str]:
    """Creates a key and returns (db row, raw key). Only the hash is stored.
    The caller commits, so the audit entry can be saved in the same transaction."""
    raw, key_hash = generate_key()
    key = ApiKey(
        client_id=client_id,
        key_prefix=raw[:8],
        key_hash=key_hash,
        expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    db.add(key)
    db.flush()  # assigns key.id
    return key, raw


def key_out(key: ApiKey) -> dict:
    return {
        "id": key.id,
        "client_id": key.client_id,
        "key_prefix": key.key_prefix,
        "status": key.status,
        "expires_at": key.expires_at,
        "last_used_at": key.last_used_at,
        "created_at": key.created_at,
    }
