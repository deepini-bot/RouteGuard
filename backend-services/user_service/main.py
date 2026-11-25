from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(title="User Service")

USERS = {
    1: {"id": 1, "name": "Asha", "email": "asha@example.com"},
    2: {"id": 2, "name": "Ravi", "email": "ravi@example.com"},
}


class UserIn(BaseModel):
    name: str
    email: str


@app.get("/health")
def health():
    return {"status": "ok", "service": "user-service"}


@app.get("/api/users/{user_id}")
def get_user(user_id: int, x_client_id: str | None = Header(default=None)):
    if user_id not in USERS:
        raise HTTPException(404, "User not found")
    # x_client_id is added by the gateway, which shows what backends can rely on
    return {"service": "user-service", "called_by_client": x_client_id, **USERS[user_id]}


@app.post("/api/users", status_code=201)
def create_user(body: UserIn):
    new_id = max(USERS) + 1
    USERS[new_id] = {"id": new_id, **body.model_dump()}
    return {"service": "user-service", **USERS[new_id]}
