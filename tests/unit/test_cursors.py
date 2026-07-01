from __future__ import annotations

import pytest

from qbsvc.api.cursors import decode_cursor, encode_cursor
from qbsvc.exceptions import PaginationError


def test_encode_decode_round_trip():
    for position in (1, 2, 100, 1_000_000):
        assert decode_cursor(encode_cursor(position)) == position


def test_encoded_cursor_is_not_a_bare_integer():
    """Cursors must be opaque so clients can't infer or hand-craft them.
    Recommendation §16 #2 — keeping the wire shape free to change later.
    """
    encoded = encode_cursor(42)
    assert not encoded.isdigit()


def test_encode_rejects_non_positive_positions():
    with pytest.raises(ValueError):
        encode_cursor(0)
    with pytest.raises(ValueError):
        encode_cursor(-5)


def test_decode_rejects_garbage():
    with pytest.raises(PaginationError):
        decode_cursor("not-valid-base64!!!")


def test_decode_rejects_non_integer_payload():
    import base64

    encoded = base64.urlsafe_b64encode(b"hello").decode().rstrip("=")
    with pytest.raises(PaginationError):
        decode_cursor(encoded)


def test_decode_rejects_non_positive_position():
    import base64

    encoded = base64.urlsafe_b64encode(b"0").decode().rstrip("=")
    with pytest.raises(PaginationError):
        decode_cursor(encoded)
