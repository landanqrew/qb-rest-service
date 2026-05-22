class QBError(Exception):
    """Base exception for all quickbooks-cli errors."""


class ConfigError(QBError):
    """Missing or invalid configuration."""


class AuthError(QBError):
    """Authentication or token errors."""

    # Stable identifier surfaced in /readyz and error envelopes. Subclasses
    # override to give callers a fixed signal that doesn't drift with the
    # human-readable message.
    code: str = "AUTH_ERROR"


class NotAuthenticatedError(AuthError):
    """No tokens in the store — OAuth flow has never been completed."""

    code = "NOT_AUTHENTICATED"


class TokenRefreshError(AuthError):
    """Refresh exchange with Intuit failed (network, invalid_grant, etc.)."""

    code = "TOKEN_REFRESH_FAILED"


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
