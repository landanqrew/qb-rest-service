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

    def __init__(
        self,
        status_code: int,
        detail: str,
        raw: dict | None = None,
        intuit_tid: str | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.raw = raw
        # Intuit's transaction id (`intuit_tid` header) from the failing
        # response, carried so the exception handler can echo it to callers.
        self.intuit_tid = intuit_tid
        super().__init__(f"HTTP {status_code}: {detail}")


class RateLimitError(QBError):
    """Rate limit exceeded — either by QBO (after a retry) or by the
    process-local token bucket in front of QBClient.

    `retry_after` is set (in seconds) when the bucket trips locally, so the
    HTTP layer can surface a Retry-After header to the caller. It stays
    None for the post-retry QBO 429 path, which has no useful retry hint
    to forward.
    """

    def __init__(self, message: str = "Rate limited", *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
