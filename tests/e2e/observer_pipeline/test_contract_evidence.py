"""Cross-module evidence for Observer digest and fail-closed dependencies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_connector.domain.canonical_json import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[3]
CONNECTOR_DIGEST_CONTRACT = (
    ROOT / "hermes-connector/src/hermes_connector/contracts/generated/"
    "canonical-payload-digest-v1.json"
)
CLOUD_DIGEST_CONTRACT = (
    ROOT / "hermes-cloud/src/hermes_cloud/contracts/generated/"
    "canonical-payload-digest-v1.json"
)
WEB_GENERATED_CONTRACTS = ROOT / "hermes-web/src/shared/contracts/generated"
ANDROID_GENERATED_CONTRACTS = (
    ROOT / "hermes-android/core/protocol/src/test/resources/contracts"
)
V2_CONSUMER_RESOURCES = (
    Path("cloud-realtime-v2.json"),
    Path("observer-output-parity-v2.json"),
    Path("schemas/cloud/payloads/session-event-v2.schema.json"),
)


def test_chinese_utf8_digest_is_identical_at_connector_and_cloud_boundaries() -> None:
    connector_contract = json.loads(CONNECTOR_DIGEST_CONTRACT.read_bytes())
    cloud_contract = json.loads(CLOUD_DIGEST_CONTRACT.read_bytes())
    assert connector_contract == cloud_contract
    vector = next(
        item
        for item in connector_contract["vectors"]
        if item["name"] == "utf8-chinese-v1"
    )
    expected = "fc8c9ae630b9a4baba1252ee82bc62c50bd4d2000e16fb5673af92325313703d"
    assert vector["sha256"] == expected
    assert (
        canonical_json_bytes(vector["payload"]).decode("utf-8")
        == vector["canonical_utf8"]
    )
    assert hashlib.sha256(canonical_json_bytes(vector["payload"])).hexdigest() == (
        expected
    )
    assert canonical_payload_digest(vector["payload"]) == expected


def test_web_and_android_v2_generated_resources_match_contract_authority() -> None:
    for relative_path in V2_CONSUMER_RESOURCES:
        authoritative = json.loads((ROOT / "contracts" / relative_path).read_bytes())
        assert (
            json.loads((WEB_GENERATED_CONTRACTS / relative_path).read_bytes())
            == authoritative
        )
        assert (
            json.loads((ANDROID_GENERATED_CONTRACTS / relative_path).read_bytes())
            == authoritative
        )

    realtime_fixture = json.loads(
        (ROOT / "contracts/fixtures/valid/cloud-realtime-v2-event.json").read_bytes()
    )
    assert realtime_fixture["method"] == "event"
    assert realtime_fixture["params"]["observer_contract"] == 2
    assert realtime_fixture["params"]["type"] in {
        "todo.update",
        "subagent.update",
        "tool.update",
        "terminal.update",
    }
