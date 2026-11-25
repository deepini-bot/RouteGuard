# One image for all three demo services. Pick which one with SERVICE and PORT.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30"
COPY . .

ARG SERVICE=product_service
ENV SERVICE=${SERVICE}
ENV PORT=8000

CMD ["sh", "-c", "uvicorn ${SERVICE}.main:app --host 0.0.0.0 --port ${PORT}"]
