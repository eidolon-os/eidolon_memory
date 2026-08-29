"""Short-lived, stateless confirmation proofs for exact privacy mutations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Literal

PrivacyMutationAction = Literal["archive", "delete"]


@dataclass(frozen=True, slots=True)
class ConfirmedPrivacyMutation:
    preview_id: str
    memory_space_id: str
    action: PrivacyMutationAction
    target: str
    drawer_ids: list[str]
    commitment_ids: list[str]
    expires_at: int


class PrivacyConfirmationSigner:
    """Issue tamper-evident previews without retaining a growing server ledger.

    Proofs deliberately expire and are process-local. A runner restart only
    invalidates an outstanding preview; it never changes memory state.
    """

    def __init__(self, secret: bytes | None = None, *, ttl_seconds: int = 600) -> None:
        if ttl_seconds < 1:
            raise ValueError("privacy confirmation TTL must be positive")
        self._secret = secret or secrets.token_bytes(32)
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        memory_space_id: str,
        action: PrivacyMutationAction,
        target: str,
        drawer_ids: list[str],
        commitment_ids: list[str] | None = None,
        now: int | None = None,
    ) -> tuple[str, ConfirmedPrivacyMutation]:
        issued_at = int(time.time() if now is None else now)
        unique_ids = list(dict.fromkeys(value.strip() for value in drawer_ids if value.strip()))
        unique_commitments = list(
            dict.fromkeys(
                value.strip() for value in (commitment_ids or []) if value.strip()
            )
        )
        if not unique_ids and not unique_commitments:
            raise ValueError("confirmation requires at least one exact memory ID")
        if len(unique_ids) > 100 or len(unique_commitments) > 100:
            raise ValueError("confirmation accepts at most 100 IDs per memory kind")
        if any(not value.startswith("drawer_") for value in unique_ids):
            raise ValueError("confirmation contains an invalid drawer ID")
        if any(not value.startswith("commitment:") for value in unique_commitments):
            raise ValueError("confirmation contains an invalid commitment ID")
        payload = {
            "v": 2,
            "preview_id": secrets.token_hex(12),
            "memory_space_id": memory_space_id,
            "action": action,
            "target": target,
            "drawer_ids": unique_ids,
            "commitment_ids": unique_commitments,
            "expires_at": issued_at + self.ttl_seconds,
        }
        serialized = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        encoded = self._encode(serialized)
        signature = self._encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        proof = self._mutation_from_payload(payload)
        return f"{encoded}.{signature}", proof

    def verify(
        self,
        token: str,
        *,
        expected_memory_space_id: str,
        now: int | None = None,
    ) -> ConfirmedPrivacyMutation:
        try:
            parts = token.split(".")
            if len(parts) != 2:
                raise ValueError("confirmation token is malformed")
            encoded, supplied_signature = parts
            expected_signature = self._encode(
                hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("confirmation token signature is invalid")
            payload = json.loads(self._decode(encoded))
            proof = self._mutation_from_payload(payload)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("confirmation token is malformed") from exc
        if proof.memory_space_id != expected_memory_space_id:
            raise ValueError("confirmation token belongs to another memory space")
        current = int(time.time() if now is None else now)
        if current >= proof.expires_at:
            raise ValueError("confirmation token has expired; run preview again")
        return proof

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @staticmethod
    def _mutation_from_payload(payload: dict[str, object]) -> ConfirmedPrivacyMutation:
        if payload.get("v") not in {1, 2} or payload.get("action") not in {"archive", "delete"}:
            raise ValueError("confirmation token schema is invalid")
        drawer_ids = payload.get("drawer_ids")
        commitment_ids = payload.get("commitment_ids", [])
        if not isinstance(drawer_ids, list) or not isinstance(commitment_ids, list):
            raise ValueError("confirmation token IDs are invalid")
        if not drawer_ids and not commitment_ids:
            raise ValueError("confirmation token has no memory IDs")
        values = [str(value) for value in drawer_ids]
        commitment_values = [str(value) for value in commitment_ids]
        if len(values) > 100 or any(not value.startswith("drawer_") for value in values):
            raise ValueError("confirmation token has invalid drawer IDs")
        if len(commitment_values) > 100 or any(
            not value.startswith("commitment:") for value in commitment_values
        ):
            raise ValueError("confirmation token has invalid commitment IDs")
        return ConfirmedPrivacyMutation(
            preview_id=str(payload.get("preview_id") or ""),
            memory_space_id=str(payload.get("memory_space_id") or ""),
            action=str(payload["action"]),  # type: ignore[arg-type]
            target=str(payload.get("target") or ""),
            drawer_ids=values,
            commitment_ids=commitment_values,
            expires_at=int(payload.get("expires_at") or 0),
        )
