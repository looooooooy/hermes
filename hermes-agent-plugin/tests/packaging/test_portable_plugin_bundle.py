from __future__ import annotations

import base64
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
COMMON = PLUGIN_ROOT / "packaging/common"
MODULE_PATH = COMMON / "portable_plugin_bundle.py"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))


def _module():
    spec = importlib.util.spec_from_file_location("hermes_portable_plugin_bundle", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _private_key(path: Path) -> None:
    contents = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(contents)
    path.chmod(0o600)


def test_portable_bundle_signs_only_relocatable_artifact_identity(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    module = _module()
    key = tmp_path / "vendor-key.pem"
    _private_key(key)
    now = datetime.now(timezone.utc)
    paths = module.assemble_portable_plugin_bundle_v2(
        wheel_path=canonical_wheel.resolve(),
        private_key_path=key.resolve(),
        output_root=(tmp_path / "portable-v2").resolve(),
        key_id="qualification-vendor-key",
        now=now,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        key_not_before=now - timedelta(days=1),
        key_not_after=now + timedelta(days=30),
    )

    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    trust = json.loads(paths.trust_store_path.read_text(encoding="utf-8"))
    assert set(manifest) == {
        "schema_version",
        "plugin_id",
        "version",
        "artifact_filename",
        "wheel_sha256",
        "entrypoint",
        "signature_algorithm",
        "key_id",
        "issued_at",
        "expires_at",
        "signature",
    }
    assert manifest["schema_version"] == 2
    assert manifest["artifact_filename"] == canonical_wheel.name
    assert "wheel_path" not in manifest
    assert "store_root" not in manifest
    encoded = json.dumps(manifest, sort_keys=True)
    assert str(tmp_path) not in encoded
    assert str(Path.home()) not in encoded
    assert paths.wheel_path.name == manifest["artifact_filename"]

    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(trust["keys"][0]["public_key"], validate=True)
    )
    public_key.verify(
        base64.b64decode(manifest["signature"], validate=True),
        canonical,
    )


def test_portable_bundle_is_still_bound_to_exact_wheel_bytes(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    module = _module()
    key = tmp_path / "vendor-key.pem"
    _private_key(key)
    now = datetime.now(timezone.utc)
    paths = module.assemble_portable_plugin_bundle_v2(
        wheel_path=canonical_wheel.resolve(),
        private_key_path=key.resolve(),
        output_root=(tmp_path / "portable-v2").resolve(),
        key_id="qualification-vendor-key",
        now=now,
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        key_not_before=now - timedelta(days=1),
        key_not_after=now + timedelta(days=30),
    )
    manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    import hashlib

    assert hashlib.sha256(paths.wheel_path.read_bytes()).hexdigest() == manifest["wheel_sha256"]
