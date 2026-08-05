from __future__ import annotations

import base64
import json
import os
import stat
from types import SimpleNamespace
from uuid import UUID

import pytest

from hermes_cloud.platform.sqlalchemy import observer_encryption
from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    AesGcmTenantEnvelopeCipher,
    MappingTenantKekResolver,
    ObserverEncryptionContext,
    ObserverEncryptionError,
    read_tenant_kek_registry,
)

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("50000000-0000-4000-8000-000000000001")
OTHER_TENANT_ID = UUID("10000000-0000-4000-8000-000000000002")


def _resolver(
    *,
    tenant_a_current: str = "v1",
    include_v1: bool = True,
) -> MappingTenantKekResolver:
    tenant_a_keys = {"v2": b"2" * 32}
    if include_v1:
        tenant_a_keys["v1"] = b"1" * 32
    return MappingTenantKekResolver(
        keys={
            TENANT_ID: tenant_a_keys,
            OTHER_TENANT_ID: {"v1": b"b" * 32},
        },
        current_versions={
            TENANT_ID: tenant_a_current,
            OTHER_TENANT_ID: "v1",
        },
    )


def _context(*, field: str = "messages") -> ObserverEncryptionContext:
    return ObserverEncryptionContext(
        tenant_id=TENANT_ID,
        agent_id=AGENT_ID,
        profile="default",
        session_key="session-root-1",
        field=field,
        schema_version=1,
    )


def test_tenant_envelope_cipher_round_trips_and_binds_full_aad() -> None:
    cipher = AesGcmTenantEnvelopeCipher(_resolver())
    plaintext = [{"role": "assistant", "content": "不可明文存储"}]

    encrypted = cipher.encrypt_json(plaintext, context=_context())

    assert encrypted["version"] == 1
    assert encrypted["algorithm"] == "A256GCM"
    assert encrypted["key_version"] == "v1"
    assert set(encrypted) == {
        "version",
        "algorithm",
        "key_version",
        "kek_fingerprint",
        "wrap_nonce",
        "wrapped_dek",
        "wrap_tag",
        "payload_nonce",
        "ciphertext",
        "payload_tag",
    }
    assert "不可明文存储" not in str(encrypted)
    assert cipher.decrypt_json(encrypted, context=_context()) == plaintext
    with pytest.raises(ObserverEncryptionError):
        cipher.decrypt_json(encrypted, context=_context(field="inflight"))
    with pytest.raises(ObserverEncryptionError):
        AesGcmTenantEnvelopeCipher(
            MappingTenantKekResolver(
                keys={TENANT_ID: {"v1": b"x" * 32}},
                current_versions={TENANT_ID: "v1"},
            )
        ).decrypt_json(encrypted, context=_context())


@pytest.mark.parametrize("kek", (b"", b"short", b"x" * 31, b"x" * 33))
def test_tenant_envelope_cipher_rejects_invalid_key_material(kek: bytes) -> None:
    with pytest.raises(ObserverEncryptionError):
        MappingTenantKekResolver(
            keys={TENANT_ID: {"v1": kek}},
            current_versions={TENANT_ID: "v1"},
        )


def test_tenant_keys_are_isolated_and_missing_tenant_fails_closed() -> None:
    cipher = AesGcmTenantEnvelopeCipher(_resolver())
    encrypted = cipher.encrypt_json({"value": "tenant-a"}, context=_context())
    other_context = ObserverEncryptionContext(
        tenant_id=OTHER_TENANT_ID,
        agent_id=AGENT_ID,
        profile="default",
        session_key="session-root-1",
        field="messages",
        schema_version=1,
    )

    with pytest.raises(ObserverEncryptionError):
        cipher.decrypt_json(encrypted, context=other_context)
    with pytest.raises(ObserverEncryptionError):
        AesGcmTenantEnvelopeCipher(
            MappingTenantKekResolver(keys={}, current_versions={})
        ).encrypt_json({"value": "missing"}, context=_context())


def test_key_rotation_reads_old_and_writes_current_until_old_key_is_retired() -> None:
    v1_cipher = AesGcmTenantEnvelopeCipher(_resolver(tenant_a_current="v1"))
    old = v1_cipher.encrypt_json({"generation": 1}, context=_context())
    rotated = AesGcmTenantEnvelopeCipher(_resolver(tenant_a_current="v2"))

    current = rotated.encrypt_json({"generation": 2}, context=_context())

    assert old["key_version"] == "v1"
    assert current["key_version"] == "v2"
    assert rotated.decrypt_json(old, context=_context()) == {"generation": 1}
    with pytest.raises(ObserverEncryptionError):
        AesGcmTenantEnvelopeCipher(
            _resolver(tenant_a_current="v2", include_v1=False)
        ).decrypt_json(old, context=_context())


@pytest.mark.parametrize(
    "field",
    ("wrapped_dek", "wrap_tag", "ciphertext", "payload_tag"),
)
def test_ciphertext_tamper_is_rejected(field: str) -> None:
    cipher = AesGcmTenantEnvelopeCipher(_resolver())
    encrypted = cipher.encrypt_json({"value": "sealed"}, context=_context())
    encoded = str(encrypted[field])
    encrypted[field] = ("B" if encoded.startswith("A") else "A") + encoded[1:]

    with pytest.raises(ObserverEncryptionError):
        cipher.decrypt_json(encrypted, context=_context())


def test_tenant_key_registry_is_loaded_only_from_strict_credential_file(
    tmp_path,
) -> None:
    path = tmp_path / "observer-keyring.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    str(TENANT_ID): {
                        "current": "v2",
                        "keys": {
                            "v1": base64.b64encode(b"1" * 32).decode("ascii"),
                            "v2": base64.b64encode(b"2" * 32).decode("ascii"),
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    resolver = read_tenant_kek_registry(str(path))

    assert resolver.current(TENANT_ID) == ("v2", b"2" * 32)
    assert resolver.resolve(TENANT_ID, "v1") == b"1" * 32
    assert "111111" not in repr(resolver)


@pytest.mark.parametrize(
    "raw",
    (
        '{"version":1,"version":1,"tenants":{}}',
        '{"version":1,"tenants":{"not-a-uuid":{}}}',
        '{"version":1,"tenants":{},"unknown":true}',
    ),
)
def test_tenant_key_registry_rejects_noncanonical_or_duplicate_config(
    tmp_path,
    raw: str,
) -> None:
    path = tmp_path / "invalid-observer-keyring.json"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ObserverEncryptionError):
        read_tenant_kek_registry(str(path))


def test_tenant_key_registry_rejects_boolean_schema_version(tmp_path) -> None:
    path = tmp_path / "boolean-version-observer-keyring.json"
    path.write_text(
        json.dumps(
            {
                "version": True,
                "tenants": {
                    str(TENANT_ID): {
                        "current": "v1",
                        "keys": {"v1": base64.b64encode(b"1" * 32).decode("ascii")},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(ObserverEncryptionError):
        read_tenant_kek_registry(str(path))


def test_observer_keyring_reference_accepts_only_root_group_0440_shared_metadata() -> (
    None
):
    reference_type = getattr(
        observer_encryption,
        "_ObserverKeyringFileReference",
        None,
    )
    assert reference_type is not None
    shared = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o440,
        st_uid=0,
        st_gid=123,
        st_size=1,
    )

    reference_type._validate_metadata(
        shared,  # type: ignore[arg-type]
        expected_uid=999,
        effective_group_ids=frozenset({123}),
    )

    for invalid in (
        SimpleNamespace(**{**vars(shared), "st_uid": 456}),
        SimpleNamespace(**{**vars(shared), "st_gid": 456}),
        SimpleNamespace(**{**vars(shared), "st_mode": stat.S_IFREG | 0o400}),
        SimpleNamespace(**{**vars(shared), "st_mode": stat.S_IFREG | 0o640}),
    ):
        with pytest.raises(ObserverEncryptionError):
            reference_type._validate_metadata(
                invalid,  # type: ignore[arg-type]
                expected_uid=999,
                effective_group_ids=frozenset({123}),
            )


def test_observer_keyring_reference_rejects_relative_and_nonexact_private_mode(
    tmp_path,
) -> None:
    path = tmp_path / "observer-keyring.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    str(TENANT_ID): {
                        "current": "v1",
                        "keys": {"v1": base64.b64encode(b"1" * 32).decode("ascii")},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o400)
    with pytest.raises(ObserverEncryptionError):
        read_tenant_kek_registry(str(path))

    path.chmod(0o600)
    previous = os.getcwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(ObserverEncryptionError):
            read_tenant_kek_registry(path.name)
    finally:
        os.chdir(previous)
