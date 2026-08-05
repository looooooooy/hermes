from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_connector.adapters.cloud.codec import (
    ConnectorProtocolCodec,
    InvalidCloudFrame,
)

CONTRACTS = Path(__file__).parents[4] / "contracts"
VALID = CONTRACTS / "fixtures" / "valid"


def _fixture(name: str) -> bytes:
    return (VALID / name).read_bytes()


def test_codec_consumes_the_frozen_session_catalog_payload_family() -> None:
    codec = ConnectorProtocolCodec()

    page = codec.decode_session_catalog_snapshot_page(
        _fixture("session-catalog-snapshot-page.json")
    )
    event = codec.decode_session_catalog_event(
        _fixture("session-catalog-event-upsert.json")
    )
    snapshot_ack = codec.decode_session_catalog_ack(
        _fixture("session-catalog-ack-snapshot.json")
    )
    event_ack = codec.decode_session_catalog_ack(
        _fixture("session-catalog-ack-event.json")
    )
    event_nack = codec.decode_session_catalog_nack(
        _fixture("session-catalog-nack-event-gap.json")
    )

    assert page.profile == "default"
    assert page.sessions[0].session_key == "durable-session-real"
    assert event.catalog_sequence == 8
    assert snapshot_ack.ack_kind == "snapshot_committed"
    assert event_ack.ack_kind == "event_applied"
    assert event_nack.reason == "event_gap"
    assert codec.decode_session_catalog_snapshot_page(
        codec.encode_session_catalog_snapshot_page(page)
    ) == page
    assert codec.decode_session_catalog_event(
        codec.encode_session_catalog_event(event)
    ) == event


@pytest.mark.parametrize(
    ("decoder_name", "fixture_name"),
    (
        (
            "decode_session_catalog_snapshot_page",
            "session-catalog-snapshot-page.json",
        ),
        ("decode_session_catalog_event", "session-catalog-event-upsert.json"),
        ("decode_session_catalog_ack", "session-catalog-ack-event.json"),
        ("decode_session_catalog_nack", "session-catalog-nack-event-gap.json"),
    ),
)
def test_connector_fabricated_cloud_session_id_is_rejected(
    decoder_name: str,
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(fixture_name))
    payload["session_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    with pytest.raises(InvalidCloudFrame):
        getattr(codec, decoder_name)(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )


@pytest.mark.parametrize(
    ("fixture_name", "reason"),
    (
        ("session-catalog-nack-page-gap.json", "page_gap"),
        ("session-catalog-nack-event-gap.json", "event_gap"),
        ("session-catalog-nack-runtime-mismatch.json", "runtime_mismatch"),
        ("session-catalog-nack-stale-writer.json", "stale_writer"),
        ("session-catalog-nack-contract-mismatch.json", "contract_mismatch"),
        ("session-catalog-nack-revision-conflict.json", "revision_conflict"),
    ),
)
def test_codec_accepts_only_the_root_catalog_nack_reason_family(
    fixture_name: str,
    reason: str,
) -> None:
    decoded = ConnectorProtocolCodec().decode_session_catalog_nack(
        _fixture(fixture_name)
    )
    assert decoded.reason == reason


def test_codec_rejects_non_contract_snapshot_mismatch_reason() -> None:
    payload = json.loads(_fixture("session-catalog-nack-page-gap.json"))
    payload["reason"] = "snapshot_mismatch"

    with pytest.raises(InvalidCloudFrame, match="reason"):
        ConnectorProtocolCodec().decode_session_catalog_nack(
            json.dumps(payload, separators=(",", ":")).encode()
        )
