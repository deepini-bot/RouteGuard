from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Product Service")

PRODUCTS = {
    101: {"id": 101, "name": "Mechanical Keyboard", "price": 3499.0},
    102: {"id": 102, "name": "USB-C Hub", "price": 1299.0},
    103: {"id": 103, "name": "Laptop Stand", "price": 899.0},
}


class ProductIn(BaseModel):
    name: str
    price: float


@app.get("/health")
def health():
    return {"status": "ok", "service": "product-service"}


@app.get("/api/products")
def list_products():
    return {"service": "product-service", "items": list(PRODUCTS.values())}


@app.get("/api/products/{product_id}")
def get_product(product_id: int):
    if product_id not in PRODUCTS:
        raise HTTPException(404, "Product not found")
    return {"service": "product-service", **PRODUCTS[product_id]}


@app.post("/api/products", status_code=201)
def create_product(body: ProductIn):
    new_id = max(PRODUCTS) + 1
    PRODUCTS[new_id] = {"id": new_id, **body.model_dump()}
    return {"service": "product-service", **PRODUCTS[new_id]}


# --- v2: a different response shape, so the gateway's versioning can be demonstrated ---

def _v2(product: dict) -> dict:
    return {
        "service": "product-service",
        "version": "v2",
        "id": product["id"],
        "name": product["name"],
        "price": {"amount": product["price"], "currency": "INR"},
    }


@app.get("/api/v2/products")
def list_products_v2():
    return {"service": "product-service", "version": "v2", "items": [_v2(p) for p in PRODUCTS.values()]}


@app.get("/api/v2/products/{product_id}")
def get_product_v2(product_id: int):
    if product_id not in PRODUCTS:
        raise HTTPException(404, "Product not found")
    return _v2(PRODUCTS[product_id])
