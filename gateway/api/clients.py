from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from gateway.api.deps import require_admin
from gateway.database.connection import get_db
from gateway.database.models import ApiKey, Client, ClientPermission, Route
from gateway.middleware.authentication import create_jwt
from gateway.services import audit_service as audit
from gateway.services.api_key_service import create_api_key, key_out

router = APIRouter(prefix="/admin", tags=["clients & keys"], dependencies=[Depends(require_admin)])


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ClientPatch(BaseModel):
    status: Literal["active", "disabled"]


class KeyIn(BaseModel):
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class KeyPatch(BaseModel):
    status: Literal["active", "inactive", "revoked"]


class PermissionsIn(BaseModel):
    route_ids: list[int]


class TokenIn(BaseModel):
    expires_minutes: int = Field(default=60, ge=1, le=1440)


def _client_out(c: Client) -> dict:
    return {"id": c.id, "name": c.name, "status": c.status, "created_at": c.created_at}


def _get_client(db: Session, client_id: int) -> Client:
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    return client


@router.post("/clients", status_code=201)
def create_client(request: Request, body: ClientIn, db: Session = Depends(get_db)):
    if db.scalar(select(Client).where(Client.name == body.name)):
        raise HTTPException(409, "A client with this name already exists")
    client = Client(name=body.name)
    db.add(client)
    db.flush()
    audit.record(db, request, "client.create", "client", client.id, {"name": client.name})
    db.commit()
    return _client_out(client)


@router.get("/clients")
def list_clients(db: Session = Depends(get_db)):
    return [_client_out(c) for c in db.scalars(select(Client).order_by(Client.id))]


@router.patch("/clients/{client_id}")
def update_client(request: Request, client_id: int, body: ClientPatch, db: Session = Depends(get_db)):
    client = _get_client(db, client_id)
    diff = audit.apply_changes(client, {"status": body.status})
    if diff:
        audit.record(db, request, "client.update", "client", client.id, diff)
    db.commit()
    return _client_out(client)


@router.post("/clients/{client_id}/keys", status_code=201)
def generate_key(request: Request, client_id: int, body: KeyIn | None = None, db: Session = Depends(get_db)):
    _get_client(db, client_id)
    key, raw = create_api_key(db, client_id, body.expires_in_days if body else None)
    # Only the prefix goes in the audit log, never the key itself
    audit.record(db, request, "key.create", "api_key", key.id, {"client_id": client_id, "key_prefix": key.key_prefix})
    db.commit()
    # This is the only time the raw key is ever visible
    return {**key_out(key), "api_key": raw, "note": "Store this key now. It cannot be shown again."}


@router.get("/clients/{client_id}/keys")
def list_keys(client_id: int, db: Session = Depends(get_db)):
    _get_client(db, client_id)
    keys = db.scalars(select(ApiKey).where(ApiKey.client_id == client_id).order_by(ApiKey.id))
    return [key_out(k) for k in keys]


@router.patch("/keys/{key_id}")
def update_key(request: Request, key_id: int, body: KeyPatch, db: Session = Depends(get_db)):
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "Key not found")
    if key.status == "revoked" and body.status != "revoked":
        raise HTTPException(409, "A revoked key cannot be re-activated. Generate a new key instead.")
    diff = audit.apply_changes(key, {"status": body.status})
    if diff:
        action = "key.revoke" if body.status == "revoked" else "key.update"
        audit.record(db, request, action, "api_key", key.id, {"client_id": key.client_id, **diff})
    db.commit()
    return key_out(key)


@router.get("/clients/{client_id}/permissions")
def get_permissions(client_id: int, db: Session = Depends(get_db)):
    _get_client(db, client_id)
    ids = db.scalars(select(ClientPermission.route_id).where(ClientPermission.client_id == client_id)).all()
    return {"client_id": client_id, "route_ids": sorted(ids)}


@router.put("/clients/{client_id}/permissions")
def set_permissions(request: Request, client_id: int, body: PermissionsIn, db: Session = Depends(get_db)):
    """Replaces the full list of routes this client may call."""
    _get_client(db, client_id)
    route_ids = set(body.route_ids)
    found = set(db.scalars(select(Route.id).where(Route.id.in_(route_ids))).all()) if route_ids else set()
    if found != route_ids:
        raise HTTPException(404, f"Unknown route ids: {sorted(route_ids - found)}")
    before = sorted(db.scalars(select(ClientPermission.route_id).where(ClientPermission.client_id == client_id)).all())
    db.execute(delete(ClientPermission).where(ClientPermission.client_id == client_id))
    db.add_all(ClientPermission(client_id=client_id, route_id=rid) for rid in route_ids)
    audit.record(
        db, request, "permissions.set", "client", client_id,
        {"route_ids": {"from": before, "to": sorted(route_ids)}},
    )
    db.commit()
    return {"client_id": client_id, "route_ids": sorted(route_ids)}


@router.post("/clients/{client_id}/token")
def issue_token(request: Request, client_id: int, body: TokenIn | None = None, db: Session = Depends(get_db)):
    """Issues a JWT for a client, for callers that use Bearer auth instead of an API key."""
    _get_client(db, client_id)
    minutes = body.expires_minutes if body else 60
    audit.record(db, request, "token.issue", "client", client_id, {"expires_minutes": minutes})
    db.commit()
    return {"access_token": create_jwt(client_id, minutes), "token_type": "bearer", "expires_in": minutes * 60}
