from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cloud.adapters.connector_contract_v1 import ContractConformanceError
from hermes_cloud.entrypoints.connector_gateway import decode_connector_frame

CONTRACTS_ROOT = Path(__file__).parent / "fixtures/repository_contracts"


def _fixture(relative_path: str) -> bytes:
    return (CONTRACTS_ROOT / "fixtures" / relative_path).read_bytes()


def _assert_invalid_envelope(raw: bytes) -> None:
    with pytest.raises(ContractConformanceError) as captured:
        decode_connector_frame(raw)

    assert captured.value.category == "invalid_envelope"
    assert captured.value.code == 4301
    assert captured.value.retryable is False


def test_rejects_authoritative_duplicate_key_fixture() -> None:
    _assert_invalid_envelope(_fixture("invalid/cloud-envelope-duplicate-key.json"))


def test_rejects_duplicate_keys_at_arbitrary_nesting_depth() -> None:
    envelope = json.loads(_fixture("valid/cloud-connector-envelope.json"))
    envelope["payload"] = {"duplicate_test": "__NESTED_DUPLICATE__"}
    raw = json.dumps(envelope, separators=(",", ":")).replace(
        '"__NESTED_DUPLICATE__"',
        '{"outer":{"value":1,"value":2}}',
    )

    _assert_invalid_envelope(raw.encode("utf-8"))


def test_rejects_authoritative_non_utc_sent_at_fixture() -> None:
    _assert_invalid_envelope(_fixture("invalid/cloud-envelope-non-utc.json"))


def test_rejects_authoritative_noncanonical_uuid_fixture() -> None:
    _assert_invalid_envelope(_fixture("invalid/cloud-envelope-noncanonical-uuid.json"))
