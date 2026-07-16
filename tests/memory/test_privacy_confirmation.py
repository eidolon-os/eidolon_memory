from __future__ import annotations

import pytest

from eidolon.memory.application.privacy_confirmation import PrivacyConfirmationSigner


def test_confirmation_proof_binds_realm_action_and_exact_ids() -> None:
    signer = PrivacyConfirmationSigner(b"test-secret", ttl_seconds=60)
    token, issued = signer.issue(
        memory_space_id="default.alice.default",
        action="delete",
        target="绿茶",
        drawer_ids=["drawer_a", "drawer_b", "drawer_a"],
        now=100,
    )

    verified = signer.verify(
        token,
        expected_memory_space_id="default.alice.default",
        now=120,
    )

    assert verified == issued
    assert verified.drawer_ids == ["drawer_a", "drawer_b"]


def test_confirmation_proof_rejects_tampering_expiry_and_cross_realm() -> None:
    signer = PrivacyConfirmationSigner(b"test-secret", ttl_seconds=10)
    token, _ = signer.issue(
        memory_space_id="default.alice.default",
        action="delete",
        target="绿茶",
        drawer_ids=["drawer_a"],
        now=100,
    )

    with pytest.raises(ValueError, match="signature"):
        signer.verify(
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            expected_memory_space_id="default.alice.default",
            now=105,
        )
    with pytest.raises(ValueError, match="another memory space"):
        signer.verify(token, expected_memory_space_id="default.bob.default", now=105)
    with pytest.raises(ValueError, match="expired"):
        signer.verify(token, expected_memory_space_id="default.alice.default", now=110)
