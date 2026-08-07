from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_release_publisher.signing import (
    build_release_trust_store,
    sign_control_payload,
    write_json_new,
)

NOW = "2026-08-07T14:00:00Z"
RELEASE_ID = "1.4.2+20260807.3.g9839a049"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)

    private_key = Ed25519PrivateKey.generate()
    key_path = root / "release-key.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    digest = hashlib.sha256(b"fixture-artifact").hexdigest()

    def artifact(name: str, platform_signature: str | None) -> dict[str, object]:
        return {
            "object_key": f"artifacts/v1/sha256/{digest[:2]}/{digest}/{name}",
            "sha256": digest,
            "size_bytes": 16,
            "platform_signature": platform_signature,
        }

    release_payload = {
        "schema_version": 1,
        "product": "hermes-desktop",
        "product_version": "1.4.2",
        "release_id": RELEASE_ID,
        "release_generation": 1042,
        "published_at": NOW,
        "source": {
            "repository": "looooooooy/hermes",
            "git_commit": "9" * 40,
            "workflow_run_id": "31189012613",
        },
        "components": {
            "desktop": "1.4.2",
            "runtime_manager": "1.4.2",
            "private_python": "3.13.14",
            "uv": "0.12.2",
            "core": "0.19.0-hermes.7",
            "plugin": "0.1.3",
            "connector": "0.4.1",
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
                "installer": artifact("Hermes-1.4.2-amd64.deb", "linux-package-signature"),
                "bootstrap_payload": artifact("bootstrap-linux-x86_64.tar.zst", None),
                "managed_release_payload": artifact("managed-release-linux-x86_64.tar.zst", None),
            }
        },
        "security": {
            "security_critical": False,
            "minimum_safe_release_generation": 1000,
            "mandatory_after": None,
        },
    }
    channel_payload = {
        "schema_version": 1,
        "channel": "stable",
        "channel_generation": 82,
        "release_id": RELEASE_ID,
        "release_generation": 1042,
        "published_at": NOW,
        "minimum_safe_release_generation": 1000,
        "mandatory_after": None,
        "rollback_authorization": None,
    }
    block_payload = {
        "schema_version": 1,
        "block_generation": 5,
        "published_at": NOW,
        "minimum_safe_release_generation": 1000,
        "blocked_releases": [],
    }

    for name, payload in (
        ("release-envelope.json", release_payload),
        ("channel-envelope.json", channel_payload),
        ("block-envelope.json", block_payload),
    ):
        envelope = sign_control_payload(
            payload,
            private_key_path=key_path,
            key_id="release-key-1",
            signed_at=NOW,
        )
        write_json_new(root / name, envelope)

    trust = build_release_trust_store(
        private_key_path=key_path,
        key_id="release-key-1",
        not_before="2026-08-01T00:00:00Z",
        not_after="2027-08-01T00:00:00Z",
    )
    write_json_new(root / "release-trust-store.json", trust)
    write_json_new(
        root / "observed-state.json",
        {
            "schema_version": 1,
            "active_release_id": "1.4.1+20260801.1.g11111111",
            "active_release_generation": 1041,
            "highest_release_generation": 1041,
            "highest_channel_generation": 81,
            "highest_block_generation": 5,
        },
    )
    key_path.unlink()
    print(json.dumps({"fixture": str(root), "private_key_destroyed": not key_path.exists()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
