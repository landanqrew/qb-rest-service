from __future__ import annotations

import base64
import binascii

from qbsvc.exceptions import PaginationError


def encode_cursor(position: int) -> str:
    """Encode a 1-based QBO STARTPOSITION offset as an opaque cursor.

    Opaque on purpose: scope §16 #2 — clients shouldn't depend on the
    underlying offset shape so we can swap pagination strategies later
    without a breaking change.
    """
    if position < 1:
        raise ValueError(f"position must be >= 1, got {position}")
    return (
        base64.urlsafe_b64encode(str(position).encode("ascii"))
        .decode("ascii")
        .rstrip("=")
    )


def decode_cursor(cursor: str) -> int:
    """Decode an opaque cursor back to a STARTPOSITION offset."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        position = int(raw.decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise PaginationError(f"Invalid cursor: {cursor!r}") from exc
    if position < 1:
        raise PaginationError(f"Invalid cursor position: {position}")
    return position
