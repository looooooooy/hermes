from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]

EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "agent.terminal.output",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}
MERGEABLE_EVENT_TYPES = {
    "message.delta",
    "agent.terminal.output",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}
RUNNING_STATUSES = {"running", "working", "streaming"}


def _load(relative_path: str) -> dict[str, object]:
    value = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _event_errors(
    event: object,
    *,
    session_key: str,
    runtime_session_id: str,
    previous_sequence: int,
) -> tuple[list[str], int]:
    errors: list[str] = []
    if not isinstance(event, dict):
        return ["event is not an object"], previous_sequence
    event_type = event.get("type")
    sequence = event.get("event_sequence")
    sequence_start = event.get("event_sequence_start", sequence)
    if event_type not in EVENT_TYPES:
        errors.append("event type is not allowed")
    if event.get("session_key") != session_key:
        errors.append("event session_key does not match")
    if event.get("session_id") != runtime_session_id:
        errors.append("event runtime session does not match")
    if type(sequence) is not int or sequence < 1:
        errors.append("event sequence is invalid")
        return errors, previous_sequence
    if type(sequence_start) is not int or sequence_start < 1:
        errors.append("event range start is invalid")
        return errors, previous_sequence
    if sequence_start != previous_sequence + 1:
        errors.append("event range is not contiguous")
    if sequence_start > sequence:
        errors.append("event range is reversed")
    if sequence_start < sequence and event_type not in MERGEABLE_EVENT_TYPES:
        errors.append("event range is not mergeable")
    payload = event.get("payload")
    if event_type == "status.update" and isinstance(payload, dict):
        status = payload.get("status")
        running = payload.get("running")
        if isinstance(status, str) and isinstance(running, bool):
            if running != (status in RUNNING_STATUSES):
                errors.append("status and running disagree")
    return errors, sequence


def _snapshot_errors(snapshot: dict[str, object]) -> list[str]:
    errors: list[str] = []
    running = snapshot.get("running")
    status = snapshot.get("status")
    if isinstance(running, bool) and isinstance(status, str):
        if running != (status in RUNNING_STATUSES):
            errors.append("snapshot status and running disagree")
    snapshot_sequence = snapshot.get("snapshot_event_sequence")
    event_sequence = snapshot.get("event_sequence")
    if type(snapshot_sequence) is not int or type(event_sequence) is not int:
        return [*errors, "snapshot cursors are invalid"]
    if snapshot_sequence > event_sequence:
        errors.append("snapshot cursor exceeds head")
    previous = snapshot_sequence
    replay = snapshot.get("replay_events")
    if not isinstance(replay, list):
        return [*errors, "replay is invalid"]
    for event in replay:
        event_errors, previous = _event_errors(
            event,
            session_key=str(snapshot.get("session_key")),
            runtime_session_id=str(snapshot.get("runtime_session_id")),
            previous_sequence=previous,
        )
        errors.extend(event_errors)
    if previous != event_sequence:
        errors.append("replay does not reach head")
    return errors


class ConnectorObserverV1ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot_schema = _load(
            "schemas/cloud/payloads/session-snapshot-v1.schema.json"
        )
        self.event_schema = _load(
            "schemas/cloud/payloads/session-event-v1.schema.json"
        )

    def test_catalog_freezes_observer_ingress_messages(self) -> None:
        catalog = _load("message-types-v1.json")
        by_name = {item["name"]: item for item in catalog["message_types"]}
        self.assertEqual(
            by_name["session.snapshot"],
            {
                "name": "session.snapshot",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/session-snapshot-v1.schema.json"
                ),
                "effect": "authoritative_projection_replace",
            },
        )
        self.assertEqual(
            by_name["session.event"],
            {
                "name": "session.event",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/session-event-v1.schema.json"
                ),
                "effect": "authoritative_projection_append",
            },
        )

    def test_canonical_payload_digest_v1_freezes_utf8_and_strict_json(self) -> None:
        contract = _load("canonical-payload-digest-v1.json")
        self.assertEqual(contract["contract"], "canonical-payload-digest")
        self.assertEqual(contract["version"], 1)
        self.assertEqual(
            contract["serialization"],
            {
                "encoding": "UTF-8",
                "ensure_ascii": False,
                "allow_nan": False,
                "sort_keys": True,
                "separators": [",", ":"],
            },
        )
        self.assertEqual(
            contract["decoder_requirements"],
            {
                "duplicate_object_members": "reject",
                "non_finite_numbers": "reject",
            },
        )
        vector = contract["vectors"][0]
        canonical = json.dumps(
            vector["payload"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(canonical, vector["canonical_utf8"])
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            vector["sha256"],
        )
        self.assertIn("你好", canonical)

    def test_catalog_freezes_observer_subscription_and_business_ack_messages(
        self,
    ) -> None:
        catalog = _load("message-types-v1.json")
        by_name = {item["name"]: item for item in catalog["message_types"]}
        expected = {
            "session.observe.open": (
                "schemas/cloud/payloads/session-observe-open-v1.schema.json",
                "observer_subscription_open",
            ),
            "session.observe.close": (
                "schemas/cloud/payloads/session-observe-close-v1.schema.json",
                "observer_subscription_close",
            ),
            "stream.ack": (
                "schemas/cloud/payloads/stream-ack-v1.schema.json",
                "observer_commit_acknowledge",
            ),
            "stream.nack": (
                "schemas/cloud/payloads/stream-nack-v1.schema.json",
                "observer_recovery_request",
            ),
        }
        for message_type, (schema, effect) in expected.items():
            self.assertEqual(
                by_name[message_type],
                {
                    "name": message_type,
                    "direction": "cloud_to_connector",
                    "status": "frozen",
                    "payload_schema": schema,
                    "effect": effect,
                },
            )

    def test_subscription_and_ack_payloads_have_exact_authority_bindings(self) -> None:
        open_schema = _load(
            "schemas/cloud/payloads/session-observe-open-v1.schema.json"
        )
        close_schema = _load(
            "schemas/cloud/payloads/session-observe-close-v1.schema.json"
        )
        ack_schema = _load("schemas/cloud/payloads/stream-ack-v1.schema.json")
        nack_schema = _load("schemas/cloud/payloads/stream-nack-v1.schema.json")
        subscription_binding = {
            "request_id",
            "subscription_id",
            "profile",
            "session_key",
            "target_source",
        }
        self.assertTrue(subscription_binding <= set(open_schema["required"]))
        self.assertTrue(subscription_binding <= set(close_schema["required"]))
        observer_commit_binding = {
            "observer_message_id",
            "payload_digest",
            "connector_sequence",
            "observer_message_type",
            "profile",
            "session_key",
            "runtime_generation",
            "runtime_session_id",
            "event_sequence",
        }
        self.assertTrue(observer_commit_binding <= set(ack_schema["required"]))
        self.assertTrue(
            observer_commit_binding
            | {"reason", "expected_event_sequence", "recovery"}
            <= set(nack_schema["required"])
        )
        self.assertNotIn("next_inbound_sequence", ack_schema["properties"])
        self.assertNotIn("next_outbound_sequence", ack_schema["properties"])

    def test_observer_receipt_identity_limits_match_ingress_source(self) -> None:
        event_schema = _load(
            "schemas/cloud/payloads/session-event-v1.schema.json"
        )
        schemas = (
            _load("schemas/cloud/payloads/session-observe-open-v1.schema.json"),
            _load("schemas/cloud/payloads/session-observe-close-v1.schema.json"),
            _load("schemas/cloud/payloads/stream-ack-v1.schema.json"),
            _load("schemas/cloud/payloads/stream-nack-v1.schema.json"),
        )
        source_limits = {
            "profile": event_schema["properties"]["profile"]["maxLength"],
            "session_key": event_schema["properties"]["session_key"]["maxLength"],
            "runtime_generation": event_schema["properties"][
                "runtime_generation"
            ]["maxLength"],
        }
        self.assertEqual(
            event_schema["properties"]["session_id"]["maxLength"],
            256,
        )
        for schema in schemas:
            for field, maximum in source_limits.items():
                if field in schema["properties"]:
                    self.assertEqual(
                        schema["properties"][field]["maxLength"],
                        maximum,
                        (schema["$id"], field),
                    )
            if "runtime_session_id" in schema["properties"]:
                self.assertEqual(
                    schema["properties"]["runtime_session_id"]["maxLength"],
                    256,
                    schema["$id"],
                )

    def test_subscription_and_ack_valid_invalid_fixtures(self) -> None:
        cases = (
            (
                "session-observe-open-v1.schema.json",
                "session-observe-open-payload.json",
                "session-observe-open-missing-source.json",
            ),
            (
                "session-observe-close-v1.schema.json",
                "session-observe-close-payload.json",
                "session-observe-close-missing-subscription.json",
            ),
            (
                "stream-ack-v1.schema.json",
                "stream-ack-payload.json",
                "stream-ack-heartbeat-cursor.json",
            ),
            (
                "stream-nack-v1.schema.json",
                "stream-nack-payload.json",
                "stream-nack-missing-expected-sequence.json",
            ),
        )
        for schema_name, valid_name, invalid_name in cases:
            schema = _load(f"schemas/cloud/payloads/{schema_name}")
            validator = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )
            validator.validate(_load(f"fixtures/valid/{valid_name}"))
            self.assertTrue(
                list(
                    validator.iter_errors(
                        _load(f"fixtures/invalid/{invalid_name}")
                    )
                ),
                invalid_name,
            )

    def test_payload_schemas_freeze_profile_runtime_and_external_event_catalog(
        self,
    ) -> None:
        snapshot_required = set(self.snapshot_schema["required"])
        self.assertTrue(
            {
                "profile",
                "runtime_generation",
                "session_key",
                "runtime_session_id",
                "running",
                "status",
                "event_sequence",
                "snapshot_event_sequence",
                "messages",
                "inflight",
                "replay_events",
            }.issubset(snapshot_required)
        )
        event_required = set(self.event_schema["required"])
        self.assertTrue(
            {
                "profile",
                "runtime_generation",
                "session_key",
                "session_id",
                "type",
                "event_sequence",
                "payload",
            }.issubset(event_required)
        )
        self.assertEqual(
            set(self.event_schema["properties"]["type"]["enum"]),
            EVENT_TYPES,
        )

    def test_valid_and_n_1_fixtures_validate_with_semantics(self) -> None:
        for schema, paths in (
            (
                self.snapshot_schema,
                (
                    "fixtures/valid/session-snapshot-payload.json",
                    "fixtures/compatibility/session-snapshot-n1.json",
                ),
            ),
            (
                self.event_schema,
                (
                    "fixtures/valid/session-event-payload.json",
                    "fixtures/compatibility/session-event-n1.json",
                ),
            ),
        ):
            validator = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )
            for path in paths:
                fixture = _load(path)
                validator.validate(fixture)
                if "snapshot_event_sequence" in fixture:
                    self.assertEqual(_snapshot_errors(fixture), [], path)

    def test_invalid_fixtures_fail_schema_or_semantics(self) -> None:
        cases = (
            (
                self.snapshot_schema,
                "fixtures/invalid/session-snapshot-replay-gap.json",
            ),
            (
                self.snapshot_schema,
                "fixtures/invalid/session-snapshot-status-mismatch.json",
            ),
            (
                self.event_schema,
                "fixtures/invalid/session-event-missing-profile.json",
            ),
            (
                self.event_schema,
                "fixtures/invalid/session-event-local-type.json",
            ),
            (
                self.event_schema,
                "fixtures/invalid/session-event-nonmergeable-range.json",
            ),
        )
        for schema, path in cases:
            fixture = _load(path)
            schema_errors = list(
                Draft202012Validator(
                    schema,
                    format_checker=FormatChecker(),
                ).iter_errors(fixture)
            )
            semantic_errors: list[str] = []
            if not schema_errors and "snapshot_event_sequence" in fixture:
                semantic_errors = _snapshot_errors(fixture)
            elif not schema_errors:
                semantic_errors, _ = _event_errors(
                    fixture,
                    session_key=str(fixture.get("session_key")),
                    runtime_session_id=str(fixture.get("session_id")),
                    previous_sequence=int(fixture.get("event_sequence_start", 0)) - 1,
                )
            self.assertTrue(schema_errors or semantic_errors, path)

    def test_manifest_registers_observer_valid_invalid_and_n_1(self) -> None:
        manifest = _load("fixtures/manifest.json")
        profile = manifest["external_profiles"]["connector_observer_v1"]
        self.assertEqual(profile["authority"], "message-types-v1.json")
        self.assertEqual(
            set(profile["valid"]),
            {
                "fixtures/valid/session-snapshot-payload.json",
                "fixtures/valid/session-event-payload.json",
                "fixtures/valid/session-observe-open-payload.json",
                "fixtures/valid/session-observe-close-payload.json",
                "fixtures/valid/stream-ack-payload.json",
                "fixtures/valid/stream-nack-payload.json",
            },
        )
        self.assertGreaterEqual(len(profile["invalid"]), 9)
        self.assertEqual(
            set(profile["n_1"]),
            {
                "fixtures/compatibility/session-snapshot-n1.json",
                "fixtures/compatibility/session-event-n1.json",
            },
        )


if __name__ == "__main__":
    unittest.main()
