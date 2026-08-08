from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_release_publisher.signing import (
    ReleaseSigningError,
    build_release_trust_store,
    canonical_envelope_bytes,
    sign_control_payload,
)


def write_key(path: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path.resolve()


def test_signer_emits_compact_canonical_ed25519_envelope(tmp_path: Path) -> None:
    key_path = write_key(tmp_path / "release-key.pem")
    payload = {
        "schema_version": 1,
        "channel": "stable",
        "channel_generation": 82,
        "release_id": "1.4.2+20260807.3.g9839a049",
        "release_generation": 1042,
    }

    envelope = sign_control_payload(
        payload,
        private_key_path=key_path,
        key_id="release-key-1",
        signed_at="2026-08-07T14:00:00Z",
    )

    assert envelope["payload"] == payload
    assert envelope["signature_algorithm"] == "ed25519"
    assert len(base64.b64decode(envelope["signature"])) == 64
    unsigned = dict(envelope)
    unsigned.pop("signature")
    assert canonical_envelope_bytes(envelope) == json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_trust_store_is_derived_from_same_private_key(tmp_path: Path) -> None:
    key_path = write_key(tmp_path / "release-key.pem")
    trust = build_release_trust_store(
        private_key_path=key_path,
        key_id="release-key-1",
        not_before="2026-08-01T00:00:00Z",
        not_after="2027-08-01T00:00:00Z",
    )

    assert trust["schema_version"] == 1
    assert trust["keys"][0]["key_id"] == "release-key-1"
    assert len(base64.b64decode(trust["keys"][0]["public_key"])) == 32
    assert trust["keys"][0]["revoked"] is False


def test_floating_point_values_are_forbidden_in_signed_control(tmp_path: Path) -> None:
    key_path = write_key(tmp_path / "release-key.pem")
    with pytest.raises(ReleaseSigningError, match="floating-point"):
        sign_control_payload(
            {"schema_version": 1, "rollout": 0.5},
            private_key_path=key_path,
            key_id="release-key-1",
            signed_at="2026-08-07T14:00:00Z",
        )
