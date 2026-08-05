from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest

from hermes_connector.adapters.cloud.codec import (
    ConnectorProtocolCodec,
    InvalidCloudFrame,
)
from hermes_connector.domain.cloud_protocol import (
    CommandDelivery,
    CommandReceipt,
    CommandResult,
    ConnectorHeartbeat,
    ConnectorHello,
    ConnectorWelcome,
)
from hermes_connector.domain.contract_messages import CloudEnvelope

CONTRACTS = Path(__file__).parents[4] / "contracts"
VALID = CONTRACTS / "fixtures" / "valid"
INVALID = CONTRACTS / "fixtures" / "invalid"


def _fixture(directory: Path, name: str) -> bytes:
    return (directory / name).read_bytes()


def test_codec_decodes_frozen_connector_payload_fixtures() -> None:
    codec = ConnectorProtocolCodec()

    hello = codec.decode_hello(_fixture(VALID, "connector-hello-payload.json"))
    welcome = codec.decode_welcome(_fixture(VALID, "connector-welcome-payload.json"))
    heartbeat = codec.decode_heartbeat(
        _fixture(VALID, "connector-heartbeat-payload.json")
    )
    command = codec.decode_command_delivery(
        _fixture(VALID, "command-deliver-payload.json")
    )
    receipt = codec.decode_command_receipt(
        _fixture(VALID, "command-receipt-payload.json")
    )
    result = codec.decode_command_result(_fixture(VALID, "command-result-payload.json"))

    assert isinstance(hello, ConnectorHello)
    assert hello.resume.next_outbound_sequence == 0
    assert isinstance(welcome, ConnectorWelcome)
    assert welcome.max_in_flight == 64
    assert isinstance(heartbeat, ConnectorHeartbeat)
    assert heartbeat.sender_role == "connector"
    assert isinstance(command, CommandDelivery)
    assert command.method == "prompt.submit"
    assert command.params["text"] == "Continue the current task."
    assert isinstance(receipt, CommandReceipt)
    assert receipt.state == "delivered"
    assert isinstance(result, CommandResult)
    assert result.state == "succeeded"


def test_codec_exposes_owner_control_contract_surface() -> None:
    codec = ConnectorProtocolCodec()

    for method in (
        "decode_control_request",
        "encode_control_request",
        "decode_control_request_payload",
        "decode_control_response",
        "encode_control_response",
        "decode_control_response_payload",
        "control_response_payload",
    ):
        assert hasattr(codec, method), method


@pytest.mark.parametrize(
    ("decoder_name", "fixture_name"),
    (
        ("decode_hello", "connector-hello-capability-overlap.json"),
        ("decode_hello", "connector-hello-platform-field.json"),
        ("decode_welcome", "connector-welcome-capability-overlap.json"),
        ("decode_heartbeat", "connector-heartbeat-bad-role.json"),
    ),
)
def test_codec_rejects_frozen_invalid_payload_fixtures(
    decoder_name: str,
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()

    with pytest.raises(InvalidCloudFrame):
        getattr(codec, decoder_name)(_fixture(INVALID, fixture_name))


@pytest.mark.parametrize(
    "fixture_name",
    (
        "command-deliver-method-not-allowed.json",
        "command-deliver-lease-leak.json",
    ),
)
def test_codec_rejects_invalid_command_delivery_fixtures(
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()

    with pytest.raises(InvalidCloudFrame):
        codec.decode_command_delivery(_fixture(INVALID, fixture_name))


def test_codec_rejects_command_delivery_with_reversed_time_window() -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(VALID, "command-deliver-payload.json"))
    payload["expires_at"] = payload["issued_at"]

    with pytest.raises(InvalidCloudFrame, match="expires_at"):
        codec.decode_command_delivery(json.dumps(payload).encode())


def test_codec_rejects_command_result_without_exact_terminal_outcome() -> None:
    codec = ConnectorProtocolCodec()

    with pytest.raises(InvalidCloudFrame):
        codec.decode_command_result(
            _fixture(INVALID, "command-result-missing-error.json")
        )


def test_codec_round_trips_command_receipt_and_result() -> None:
    codec = ConnectorProtocolCodec()
    receipt = codec.decode_command_receipt(
        _fixture(VALID, "command-receipt-payload.json")
    )
    result = codec.decode_command_result(_fixture(VALID, "command-result-payload.json"))

    assert (
        codec.decode_command_receipt(codec.encode_command_receipt(receipt)) == receipt
    )
    assert codec.decode_command_result(codec.encode_command_result(result)) == result


def test_codec_rejects_duplicate_keys_and_noncanonical_envelopes() -> None:
    codec = ConnectorProtocolCodec()

    for fixture_name in (
        "cloud-envelope-duplicate-key.json",
        "cloud-envelope-negative-sequence.json",
        "cloud-envelope-non-utc.json",
        "cloud-envelope-noncanonical-uuid.json",
    ):
        with pytest.raises(InvalidCloudFrame):
            codec.decode_envelope(_fixture(INVALID, fixture_name))


def test_codec_enforces_trace_context_and_payload_property_limits() -> None:
    codec = ConnectorProtocolCodec()
    envelope = json.loads(_fixture(VALID, "cloud-connector-envelope.json"))
    envelope["traceparent"] = "invalid-trace-context"

    with pytest.raises(InvalidCloudFrame):
        codec.decode_envelope(json.dumps(envelope).encode())

    envelope.pop("traceparent")
    envelope["payload"] = {f"field_{index}": index for index in range(1025)}

    with pytest.raises(InvalidCloudFrame):
        codec.decode_envelope(json.dumps(envelope).encode())


@pytest.mark.parametrize(
    "mutation",
    (
        {"oversized_utf8": "界" * 43_691},
        {"nul": "\u0000"},
        {"lone_surrogate": "\ud800"},
        {"oversized_array": list(range(1_025))},
        {"oversized_object": {f"field_{index}": index for index in range(1_025)}},
    ),
)
def test_codec_rejects_global_json_tree_limit_mutations(
    mutation: dict[str, object],
) -> None:
    codec = ConnectorProtocolCodec()
    envelope = json.loads(_fixture(VALID, "cloud-connector-envelope.json"))
    envelope["message_type"] = "command.deliver"
    envelope["payload"] = {"nested": mutation}
    frame = json.dumps(envelope, ensure_ascii=True).encode("utf-8")

    with pytest.raises(InvalidCloudFrame):
        codec.decode_envelope(frame)


def test_codec_rejects_json_nesting_deeper_than_32() -> None:
    codec = ConnectorProtocolCodec()
    envelope = json.loads(_fixture(VALID, "cloud-connector-envelope.json"))
    envelope["message_type"] = "command.deliver"
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(33):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    envelope["payload"] = nested

    with pytest.raises(InvalidCloudFrame):
        codec.decode_envelope(json.dumps(envelope).encode("utf-8"))


def test_codec_emits_deterministic_canonical_json() -> None:
    codec = ConnectorProtocolCodec()
    hello = codec.decode_hello(_fixture(VALID, "connector-hello-payload.json"))

    first = codec.encode_hello(hello)
    second = codec.encode_hello(hello)

    assert first == second
    assert first == json.dumps(
        json.loads(first),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def test_codec_round_trips_envelopes_welcome_and_heartbeat() -> None:
    codec = ConnectorProtocolCodec()
    welcome = codec.decode_welcome(_fixture(VALID, "connector-welcome-payload.json"))
    heartbeat = codec.decode_heartbeat(
        _fixture(VALID, "connector-heartbeat-payload.json")
    )
    envelope = CloudEnvelope(
        contract_version=1,
        message_id=UUID("33333333-3333-4333-8333-333333333333"),
        message_type="connector.heartbeat",
        tenant_id="tenant-test",
        device_id="device-test",
        sequence=1,
        sent_at=datetime(2026, 7, 30, 12, 0, 20, tzinfo=UTC),
        payload=MappingProxyType(json.loads(codec.encode_heartbeat(heartbeat))),
    )

    assert codec.decode_welcome(codec.encode_welcome(welcome)) == welcome
    assert codec.decode_heartbeat(codec.encode_heartbeat(heartbeat)) == heartbeat
    assert codec.decode_envelope(codec.encode_envelope(envelope)) == envelope


@pytest.mark.parametrize(
    "fixture_name",
    (
        "control-request-open.json",
        "control-request-acquire.json",
        "control-request-renew.json",
        "control-request-release.json",
        "control-request-status.json",
        "control-request-close.json",
    ),
)
def test_codec_decodes_and_round_trips_owner_control_requests(
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()

    request = codec.decode_control_request(_fixture(VALID, fixture_name))

    assert type(request).__name__ == "OwnerControlRequest"
    assert (
        codec.decode_control_request(codec.encode_control_request(request)) == request
    )
    assert (
        codec.decode_control_request_payload(json.loads(_fixture(VALID, fixture_name)))
        == request
    )


@pytest.mark.parametrize(
    "fixture_name",
    (
        "control-response-open.json",
        "control-response-acquire.json",
        "control-response-renew.json",
        "control-response-release.json",
        "control-response-status.json",
        "control-response-close.json",
        "control-response-failed.json",
        "control-response-unknown.json",
    ),
)
def test_codec_decodes_and_round_trips_owner_control_responses(
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()

    response = codec.decode_control_response(_fixture(VALID, fixture_name))

    assert type(response).__name__ == "OwnerControlResponse"
    assert codec.decode_control_response(codec.encode_control_response(response)) == (
        response
    )
    assert (
        codec.decode_control_response_payload(json.loads(_fixture(VALID, fixture_name)))
        == response
    )
    assert dict(codec.control_response_payload(response)) == json.loads(
        _fixture(VALID, fixture_name)
    )


@pytest.mark.parametrize(
    "fixture_name",
    (
        "control-request-acquire-partial-runtime.json",
        "control-request-status-lease-leak.json",
        "control-request-open-lease-leak.json",
    ),
)
def test_codec_rejects_invalid_owner_control_requests(fixture_name: str) -> None:
    codec = ConnectorProtocolCodec()

    with pytest.raises(InvalidCloudFrame):
        codec.decode_control_request(_fixture(INVALID, fixture_name))


@pytest.mark.parametrize(
    "fixture_name",
    (
        "control-response-status-lease-leak.json",
        "control-response-failed-lease-leak.json",
        "control-response-acquire-missing-lease.json",
    ),
)
def test_codec_rejects_owner_control_response_lease_leaks(
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()

    with pytest.raises(InvalidCloudFrame):
        codec.decode_control_response(_fixture(INVALID, fixture_name))


def test_codec_rejects_unhashable_pending_approval_choice_as_invalid_frame() -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(VALID, "control-response-acquire.json"))
    payload["result"]["pending_input"] = {
        "request_id": "pending-approval",
        "kind": "approval",
        "title": "Approve command",
        "description": "Review the requested operation.",
        "command": "./gradlew test",
        "choices": [{"not": "a string"}],
        "expires_at_epoch_ms": 4_102_444_800_000,
    }

    with pytest.raises(InvalidCloudFrame, match="pending approval choices"):
        codec.decode_control_response(json.dumps(payload).encode())


def test_codec_accepts_uncontrolled_status_with_null_controller_label() -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(VALID, "control-response-status.json"))
    payload["result"].update(
        {
            "controller_kind": "none",
            "controller_label": None,
            "lease_expires_at_epoch_ms": 0,
            "pending_input": None,
        }
    )

    response = codec.decode_control_response(json.dumps(payload).encode())

    assert response.result["controller_label"] is None


def test_codec_rejects_owner_control_request_with_expired_deadline() -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(VALID, "control-request-status.json"))
    payload["expires_at"] = payload["issued_at"]

    with pytest.raises(InvalidCloudFrame, match="expires_at"):
        codec.decode_control_request(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("operation", "body", "result"),
    [
        (
            "session.command.status",
            {
                "runtime_session_id": "runtime-7",
                "method": "approval.respond",
                "client_request_id": "request-status",
            },
            {
                "status": "accepted",
                "client_request_id": "request-status",
                "client_turn_id": "turn-status",
                "server_turn_id": "server-turn-status",
            },
        ),
        (
            "prompt.submit",
            {
                "runtime_session_id": "runtime-7",
                "lease_id": "opaque-lease",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "text": "Queue this turn",
            },
            {
                "status": "queued",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "server_turn_id": "server-turn-prompt",
            },
        ),
        (
            "session.interrupt",
            {"lease_id": "opaque-lease", "client_request_id": "request-stop"},
            {"status": "accepted", "client_request_id": "request-stop"},
        ),
        (
            "session.steer",
            {
                "lease_id": "opaque-lease",
                "client_request_id": "request-steer",
                "text": "Focus on the first failure",
            },
            {"status": "accepted", "client_request_id": "request-steer"},
        ),
        (
            "approval.respond",
            {
                "lease_id": "opaque-lease",
                "client_request_id": "request-approval",
                "request_id": "pending-approval",
                "choice": "allow_once",
            },
            {
                "status": "accepted",
                "kind": "approval",
                "request_id": "pending-approval",
                "client_request_id": "request-approval",
                "control_revision": 8,
            },
        ),
        (
            "clarify.respond",
            {
                "lease_id": "opaque-lease",
                "client_request_id": "request-clarify",
                "request_id": "pending-clarify",
                "choice_id": "choice-1",
            },
            {
                "status": "accepted",
                "kind": "clarify",
                "request_id": "pending-clarify",
                "client_request_id": "request-clarify",
                "control_revision": 9,
            },
        ),
    ],
)
def test_codec_round_trips_safe_mobile_owner_actions(
    operation: str,
    body: dict[str, object],
    result: dict[str, object],
) -> None:
    codec = ConnectorProtocolCodec()
    request_payload = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "control_transport_id": "22222222-2222-4222-8222-222222222222",
        "operation": operation,
        "issued_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-08-01T00:00:03Z",
        "body": body,
    }
    response_payload = {
        "request_id": request_payload["request_id"],
        "control_transport_id": request_payload["control_transport_id"],
        "operation": operation,
        "state": "succeeded",
        "completed_at": "2026-08-01T00:00:01Z",
        "result": result,
    }

    request = codec.decode_control_request(json.dumps(request_payload).encode())
    response = codec.decode_control_response(json.dumps(response_payload).encode())

    assert (
        codec.decode_control_request(codec.encode_control_request(request)) == request
    )
    assert (
        codec.decode_control_response(codec.encode_control_response(response))
        == response
    )


@pytest.mark.parametrize(
    ("kind", "label"),
    [("none", "No controller"), ("desktop", None), ("mobile", None)],
)
def test_codec_rejects_noncanonical_controller_kind_label_pairs(
    kind: str,
    label: object,
) -> None:
    payload = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "control_transport_id": "22222222-2222-4222-8222-222222222222",
        "operation": "session.control.status",
        "state": "succeeded",
        "completed_at": "2026-08-01T00:00:01Z",
        "result": {
            "controller_kind": kind,
            "controller_label": label,
            "control_revision": 1,
            "lease_expires_at_epoch_ms": 0,
            "pending_input": None,
        },
    }

    with pytest.raises(InvalidCloudFrame):
        ConnectorProtocolCodec().decode_control_response(json.dumps(payload).encode())


def test_codec_rejects_internal_4308_on_the_cloud_control_boundary() -> None:
    payload = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "control_transport_id": "22222222-2222-4222-8222-222222222222",
        "operation": "session.control.status",
        "state": "failed",
        "completed_at": "2026-08-01T00:00:01Z",
        "error": {"code": 4308, "reason": "idempotency_conflict"},
    }

    with pytest.raises(InvalidCloudFrame):
        ConnectorProtocolCodec().decode_control_response(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("operation", "body"),
    [
        (
            "prompt.submit",
            {
                "lease_id": "lease",
                "client_request_id": "request",
                "client_turn_id": "turn",
            },
        ),
        (
            "session.interrupt",
            {"lease_id": "lease", "client_request_id": "request", "extra": True},
        ),
        (
            "approval.respond",
            {
                "lease_id": "lease",
                "client_request_id": "request",
                "request_id": "pending",
                "choice": "owner_internal_allow",
            },
        ),
        (
            "clarify.respond",
            {
                "lease_id": "lease",
                "client_request_id": "request",
                "request_id": "pending",
                "choice_id": "choice",
                "other_text": "ambiguous",
            },
        ),
    ],
)
def test_codec_rejects_non_exact_mobile_owner_action_body(
    operation: str,
    body: dict[str, object],
) -> None:
    codec = ConnectorProtocolCodec()
    payload = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "control_transport_id": "22222222-2222-4222-8222-222222222222",
        "operation": operation,
        "issued_at": "2026-08-01T00:00:00Z",
        "expires_at": "2026-08-01T00:00:03Z",
        "body": body,
    }

    with pytest.raises(InvalidCloudFrame):
        codec.decode_control_request(json.dumps(payload).encode())
