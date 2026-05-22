class QBError(Exception):
    """Base exception for all quickbooks-cli errors."""


class ConfigError(QBError):
    """Missing or invalid configuration."""


class AuthError(QBError):
    """Authentication or token errors."""


class TokenExpiredError(AuthError):
    """Token has expired and refresh failed."""


class APIError(QBError):
    """Error returned by the QuickBooks Online API."""

    def __init__(self, status_code: int, detail: str, raw: dict | None = None):
        self.status_code = status_code
        self.detail = detail
        self.raw = raw
        super().__init__(f"HTTP {status_code}: {detail}")


class RateLimitError(QBError):
    """API rate limit exceeded."""


class PaginationError(QBError):
    """Error during offset-based pagination."""


class TokenStoreError(QBError):
    """The underlying token-store backend failed (e.g., GCP API error).

    Distinct from `AuthError`: this signals an operational/config problem
    with the storage layer itself, not an authentication outcome. The most
    important case is `save()` failing after Intuit has already rotated the
    refresh token — the rotated value is then lost and must be loudly
    surfaced rather than silently retried.
    """
