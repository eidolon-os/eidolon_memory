"""Opaque keyset positions shared by Owner entry and graph reads."""

from __future__ import annotations

import base64
import json


def encode_position(**fields: str) -> str:
    raw = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_position(token: str, *, fields: tuple[str, ...]) -> dict[str, str]:
    try:
        if not token or len(token) > 1024:
            raise ValueError("invalid cursor length")
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(
            base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        )
        if not isinstance(payload, dict) or set(payload) != set(fields):
            raise ValueError("invalid cursor fields")
        if any(not isinstance(value, str) for value in payload.values()):
            raise ValueError("invalid cursor position")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("cursor was not issued by this memory") from exc
    return payload
