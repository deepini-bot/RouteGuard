import os


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


class Settings:
    """All config comes from environment variables so Docker and CI can override it."""

    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./gateway.db")
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.admin_key = os.getenv("ADMIN_KEY", "dev-admin-key")
        self.jwt_secret = os.getenv("JWT_SECRET", "dev-jwt-secret-change-me-please-0123456789")

        # Used when a client has no rate limit policy configured
        self.default_limit = int(os.getenv("DEFAULT_RATE_LIMIT", "100"))
        self.default_window = int(os.getenv("DEFAULT_RATE_WINDOW", "60"))
        self.default_algorithm = os.getenv("DEFAULT_RATE_ALGORITHM", "token_bucket")

        # Limit per IP address, checked before authentication (0 = off).
        # Protects against floods and key guessing from one address.
        self.ip_rate_limit = int(os.getenv("IP_RATE_LIMIT", "300"))
        self.ip_rate_window = int(os.getenv("IP_RATE_WINDOW", "60"))
        # Only turn this on when the gateway is behind a proxy you control (it trusts X-Forwarded-For)
        self.trust_proxy_headers = _bool("TRUST_PROXY_HEADERS", "false")

        # If Redis is down: True = let requests through, False = reject with 503
        self.rate_limit_fail_open = _bool("RATE_LIMIT_FAIL_OPEN", "true")

        self.upstream_timeout = float(os.getenv("UPSTREAM_TIMEOUT", "5"))
        self.cb_failure_threshold = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
        self.cb_reset_seconds = float(os.getenv("CB_RESET_SECONDS", "30"))

        # Background jobs (interval in seconds, 0 = off)
        self.health_check_interval = float(os.getenv("HEALTH_CHECK_INTERVAL", "15"))
        self.health_check_timeout = float(os.getenv("HEALTH_CHECK_TIMEOUT", "2"))
        self.usage_rollup_interval = float(os.getenv("USAGE_ROLLUP_INTERVAL", "300"))
        # Delete request_logs older than this many days, after they are rolled up (0 = keep forever)
        self.log_retention_days = int(os.getenv("LOG_RETENTION_DAYS", "0"))


settings = Settings()
