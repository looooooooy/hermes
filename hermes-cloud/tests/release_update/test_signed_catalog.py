from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_cloud.modules.release_update.catalog import SignedReleaseCatalog
from hermes_cloud.modules.release_update.domain import DeviceUpdateContextV1
from hermes_cloud.modules.release_update.service import UpdateCheckUnavailable

NOW = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
RELEASE_ID = "1.0.5+20260808.5.g55555555"


class MemoryReader:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def read_control_object(self, object_key: str) -> bytes:
        self.reads.append(object_key)
        try:
            return self.objects[object_key]
        except KeyError as exc:
            raise RuntimeError("control object missing") from exc


def test_catalog_verifies_three_signed_controls_before_mapping_candidate() -> None:
    reader, trust, _key = fixture()
    catalog = SignedReleaseCatalog(reader=reader, trust_store=trust, now=lambda: NOW)

    candidate = catalog.select_candidate(context())

    assert candidate is not None
    assert candidate.release_id == RELEASE_ID
    assert candidate.release_generation == 105
    assert candidate.channel_generation == 82
    assert candidate.minimum_safe_release_generation == 100
    assert candidate.blocked is False
    assert candidate.rollback_authorized is False
    assert [item.kind for item in candidate.artifacts] == [
        "installer",
        "bootstrap_payload",
        "managed_release_payload",
    ]
    assert reader.reads == [
        "channels/v1/stable/current.json",
        f"releases/v1/{RELEASE_ID}/release-envelope.json",
        "blocks/v1/current.json",
    ]


def test_tampered_release_is_rejected_before_candidate_exists() -> None:
    reader, trust, _key = fixture()
    release_key = f"releases/v1/{RELEASE_ID}/release-envelope.json"
    envelope = json.loads(reader.objects[release_key])
    envelope["payload"]["product_version"] = "9.9.9"
    reader.objects[release_key] = encoded(envelope)
    catalog = SignedReleaseCatalog(reader=reader, trust_store=trust, now=lambda: NOW)

    with pytest.raises(UpdateCheckUnavailable, match="signature verification failed"):
        catalog.select_candidate(context())


def test_signed_blocklist_marks_candidate_blocked() -> None:
    reader, trust, key = fixture()
    release, channel, block = payloads()
    block["blocked_releases"] = [
        {
            "release_id": RELEASE_ID,
            "release_generation": 105,
            "reason_code": "regression",
            "blocked_at": "2026-08-07T23:30:00Z",
        }
    ]
    reader.objects["blocks/v1/current.json"] = encoded(sign(block, key))
    catalog = SignedReleaseCatalog(reader=reader, trust_store=trust, now=lambda: NOW)

    candidate = catalog.select_candidate(context())

    assert candidate is not None
    assert candidate.blocked is True
    assert release["release_id"] == channel["release_id"]


def test_rollback_authorization_must_bind_current_and_target_release() -> None:
    reader, trust, key = fixture()
    _release, channel, _block = payloads()
    channel["rollback_authorization"] = {
        "from_release_id": "1.0.8+20260807.8.g88888888",
        "from_release_generation": 108,
        "to_release_id": RELEASE_ID,
        "to_release_generation": 105,
        "reason_code": "business_rollback",
        "expires_at": "2026-08-08T00:30:00Z",
    }
    reader.objects["channels/v1/stable/current.json"] = encoded(sign(channel, key))
    catalog = SignedReleaseCatalog(reader=reader, trust_store=trust, now=lambda: NOW)

    matching = catalog.select_candidate(context(active_id="1.0.8+20260807.8.g88888888", active=108, highest=108))
    mismatched = catalog.select_candidate(context(active=104, highest=108))

    assert matching is not None and matching.rollback_authorized is True
    assert mismatched is not None and mismatched.rollback_authorized is False


def context(
    *,
    active_id: str = "1.0.4+20260801.4.g44444444",
    active: int = 104,
    highest: int = 104,
) -> DeviceUpdateContextV1:
    return DeviceUpdateContextV1(
        device_id="77777777-7777-4777-8777-777777777777",
        organization_id="33333333-3333-4333-8333-333333333333",
        target="linux-x86_64",
        os_version="Ubuntu 24.04.4 LTS",
        active_release_id=active_id,
        active_release_generation=active,
        highest_release_generation=highest,
        requested_channel="stable",
    )


def fixture() -> tuple[MemoryReader, dict[str, object], Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    release, channel, block = payloads()
    reader = MemoryReader(
        {
            "channels/v1/stable/current.json": encoded(sign(channel, key)),
            f"releases/v1/{RELEASE_ID}/release-envelope.json": encoded(sign(release, key)),
            "blocks/v1/current.json": encoded(sign(block, key)),
        }
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust: dict[str, object] = {
        "schema_version": 1,
        "keys": [
            {
                "key_id": "release-key-1",
                "signature_algorithm": "ed25519",
                "public_key": base64.b64encode(public).decode("ascii"),
                "not_before": "2026-08-01T00:00:00Z",
                "not_after": "2027-08-01T00:00:00Z",
                "revoked": False,
            }
        ],
    }
    return reader, trust, key


def payloads() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    digests = {"installer": "a" * 64, "bootstrap": "b" * 64, "runtime": "c" * 64}

    def artifact(name: str, digest: str, platform_signature: str | None) -> dict[str, object]:
        return {
            "object_key": f"artifacts/v1/sha256/{digest[:2]}/{digest}/{name}",
            "sha256": digest,
            "size_bytes": 4096,
            "platform_signature": platform_signature,
        }

    release: dict[str, object] = {
        "schema_version": 1,
        "product": "hermes-desktop",
        "product_version": "1.0.5",
        "release_id": RELEASE_ID,
        "release_generation": 105,
        "published_at": "2026-08-07T23:00:00Z",
        "source": {
            "repository": "looooooooy/hermes",
            "git_commit": "5" * 40,
            "workflow_run_id": "31200000000",
        },
        "components": {
            "desktop": "1.0.5",
            "runtime_manager": "1.0.5",
            "private_python": "3.13.14",
            "uv": "0.12.2",
            "core": "0.19.0",
            "plugin": "0.1.0",
            "connector": "0.1.0",
        },
        "contracts": {
            "runtime": 1,
            "host_spi": 1,
            "local_protocol": 1,
            "cloud_protocol": 1,
        },
        "targets": {
            "linux-x86_64": {
                "minimum_os": "Ubuntu 24.04",
                "installer": artifact("Hermes-1.0.5-amd64.deb", digests["installer"], "linux-package"),
                "bootstrap_payload": artifact("bootstrap.tar.zst", digests["bootstrap"], None),
                "managed_release_payload": artifact("runtime.tar.zst", digests["runtime"], None),
            }
        },
        "security": {
            "security_critical": False,
            "minimum_safe_release_generation": 100,
            "mandatory_after": None,
        },
    }
    channel: dict[str, object] = {
        "schema_version": 1,
        "channel": "stable",
        "channel_generation": 82,
        "release_id": RELEASE_ID,
        "release_generation": 105,
        "published_at": "2026-08-07T23:05:00Z",
        "minimum_safe_release_generation": 100,
        "mandatory_after": None,
        "rollback_authorization": None,
    }
    block: dict[str, object] = {
        "schema_version": 1,
        "block_generation": 5,
        "published_at": "2026-08-07T23:06:00Z",
        "minimum_safe_release_generation": 100,
        "blocked_releases": [],
    }
    return release, channel, block


def sign(payload: dict[str, object], key: Ed25519PrivateKey) -> dict[str, object]:
    envelope: dict[str, object] = {
        "schema_version": 1,
        "key_id": "release-key-1",
        "signature_algorithm": "ed25519",
        "signed_at": "2026-08-07T23:10:00Z",
        "payload": payload,
        "signature": "",
    }
    unsigned = dict(envelope)
    unsigned.pop("signature")
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["signature"] = base64.b64encode(key.sign(canonical)).decode("ascii")
    return envelope


def encoded(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
