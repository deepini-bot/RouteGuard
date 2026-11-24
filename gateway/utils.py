from fastapi import Request

from gateway.config import settings


def client_ip(request: Request) -> str:
    """The caller's IP address.

    X-Forwarded-For is only believed when TRUST_PROXY_HEADERS is on. Otherwise anyone could
    send a fake header and dodge the per-IP limit.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
