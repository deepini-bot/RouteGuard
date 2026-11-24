class GatewayError(Exception):
    """An error the gateway returns to the client on purpose (401, 403, 429, 502...).

    `reason` is a short code stored in request_logs.block_reason.
    The optional keyword args carry whatever we already know about the request,
    so the log row is as complete as possible even when we reject early.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        reason: str,
        headers: dict | None = None,
        client_id: int | None = None,
        route_id: int | None = None,
        service_name: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.reason = reason
        self.headers = headers or {}
        self.client_id = client_id
        self.route_id = route_id
        self.service_name = service_name
