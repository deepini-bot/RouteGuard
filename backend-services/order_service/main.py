from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Order Service")

ORDERS: dict[int, dict] = {
    1: {"id": 1, "user_id": 1, "product_id": 101, "quantity": 1, "status": "placed"},
}


class OrderIn(BaseModel):
    user_id: int
    product_id: int
    quantity: int = 1


@app.get("/health")
def health():
    return {"status": "ok", "service": "order-service"}


@app.get("/api/orders/{order_id}")
def get_order(order_id: int):
    if order_id not in ORDERS:
        raise HTTPException(404, "Order not found")
    return {"service": "order-service", **ORDERS[order_id]}


@app.post("/api/orders", status_code=201)
def create_order(body: OrderIn):
    # Creating an order is NOT idempotent, which is why the gateway never retries POST requests
    new_id = max(ORDERS) + 1
    ORDERS[new_id] = {"id": new_id, **body.model_dump(), "status": "placed"}
    return {"service": "order-service", **ORDERS[new_id]}
