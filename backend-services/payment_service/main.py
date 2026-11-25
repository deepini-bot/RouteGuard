from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Payment Service")

PAYMENTS: dict[int, dict] = {}
BY_IDEMPOTENCY_KEY: dict[str, int] = {}


class PaymentIn(BaseModel):
    order_id: int
    amount: float = Field(gt=0)
    currency: str = "INR"


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-service"}


@app.get("/api/payments/{payment_id}")
def get_payment(payment_id: int):
    if payment_id not in PAYMENTS:
        raise HTTPException(404, "Payment not found")
    return {"service": "payment-service", **PAYMENTS[payment_id]}


@app.post("/api/payments", status_code=201)
def create_payment(body: PaymentIn, idempotency_key: str | None = Header(default=None)):
    """Charging a card must not happen twice. If the same Idempotency-Key is sent again,
    we return the payment we already made instead of creating a new one.
    This is why the gateway never retries POST requests on its own.
    """
    if idempotency_key and idempotency_key in BY_IDEMPOTENCY_KEY:
        existing = PAYMENTS[BY_IDEMPOTENCY_KEY[idempotency_key]]
        return JSONResponse(
            {"service": "payment-service", **existing}, status_code=200, headers={"Idempotent-Replay": "true"}
        )

    payment_id = len(PAYMENTS) + 1
    PAYMENTS[payment_id] = {"id": payment_id, **body.model_dump(), "status": "succeeded"}
    if idempotency_key:
        BY_IDEMPOTENCY_KEY[idempotency_key] = payment_id
    return {"service": "payment-service", **PAYMENTS[payment_id]}
