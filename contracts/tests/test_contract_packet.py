from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent


class DuplicateObjectKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateObjectKey(key)
        value[key] = item
    return value


def _load(relative_path: str) -> object:
    return json.loads(
        (ROOT / relative_path).read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicates,
    )


def _registry() -> Registry:
    resources = []
    for path in (ROOT / "schemas").rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _has_semantic_error(instance: object) -> bool:
    if not isinstance(instance, dict):
        return False
    message_type = instance.get("message_type")
    if message_type == "local.hello":
        required = instance.get("required_capabilities")
        optional = instance.get("optional_capabilities")
        if isinstance(required, list) and isinstance(optional, list):
            return not set(required).isdisjoint(optional)
    if message_type == "local.welcome":
        accepted = instance.get("accepted_capabilities")
        unavailable = instance.get("unavailable_optional_capabilities")
        if isinstance(accepted, list) and isinstance(unavailable, list):
            return not set(accepted).isdisjoint(unavailable)
    if "connector_instance_id" in instance:
        required = instance.get("required_capabilities")
        optional = instance.get("optional_capabilities")
        if isinstance(required, list) and isinstance(optional, list):
            return not set(required).isdisjoint(optional)
    if "connection_id" in instance and "resume_decision" in instance:
        accepted = instance.get("accepted_capabilities")
        unavailable = instance.get("unavailable_optional_capabilities")
        if isinstance(accepted, list) and isinstance(unavailable, list):
            return not set(accepted).isdisjoint(unavailable)
    return False


class ContractPacketTest(unittest.TestCase):
    def test_schema_identifiers_are_unique_and_draft_2020_12(self) -> None:
        paths = sorted((ROOT / "schemas").rglob("*.schema.json"))
        self.assertGreaterEqual(len(paths), 3)

        identifiers: list[str] = []
        for path in paths:
            schema = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self.assertEqual(
                schema["$schema"],
                "https://json-schema.org/draft/2020-12/schema",
            )
            identifiers.append(schema["$id"])

        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_valid_fixtures_match_their_schema(self) -> None:
        manifest = _load("fixtures/manifest.json")
        for item in manifest["valid"]:
            schema = _load(item["schema"])
            instance = _load(item["fixture"])
            Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
                registry=_registry(),
            ).validate(instance)

    def test_invalid_fixtures_are_rejected(self) -> None:
        manifest = _load("fixtures/manifest.json")
        for item in manifest["invalid"]:
            schema = _load(item["schema"])
            try:
                instance = _load(item["fixture"])
            except DuplicateObjectKey:
                continue
            errors = list(
                Draft202012Validator(
                    schema,
                    format_checker=FormatChecker(),
                    registry=_registry(),
                ).iter_errors(instance)
            )
            self.assertTrue(
                errors or _has_semantic_error(instance),
                item["fixture"],
            )

    def test_error_codes_are_unique_and_do_not_overlap_json_rpc_reserved(self) -> None:
        catalog = _load("error-codes-v1.json")
        codes = [item["code"] for item in catalog["errors"]]
        existing_plugin_codes = {4001, 4003, *range(4200, 4220)}

        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(code >= 4000 for code in codes))
        self.assertTrue(set(codes).isdisjoint(existing_plugin_codes))

    def test_capability_manifest_reserves_enterprise_and_ui_namespaces(self) -> None:
        schema = _load("schemas/capability-manifest-v1.schema.json")
        namespaces = schema["properties"]["capabilities"]["items"]["enum"]

        self.assertTrue(
            {
                "view.card",
                "view.interaction",
                "enterprise.data",
                "mcp.app",
            }.issubset(namespaces)
        )

    def test_mobile_control_source_is_authoritative_for_consumer_copies(self) -> None:
        source = _load("sources/mobile-control-v1.json")
        consumer_paths = [
            REPOSITORY_ROOT
            / "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/mobile-control-v1.json",
            REPOSITORY_ROOT
            / "hermes-android/core/protocol/src/test/resources/contracts/mobile-control-v1.json",
            REPOSITORY_ROOT
            / (
                "hermes-connector/src/hermes_connector/contracts/generated/"
                "sources/mobile-control-v1.json"
            ),
        ]

        for path in consumer_paths:
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                source,
                str(path),
            )

    def test_connector_cloud_contracts_are_authoritative_for_connector_copies(
        self,
    ) -> None:
        relative_paths = (
            "canonical-payload-digest-v1.json",
            "message-types-v1.json",
            "schemas/session-catalog-entry-v1.schema.json",
            "schemas/cloud/connector-envelope-v1.schema.json",
            "schemas/cloud/payloads/command-deliver-v1.schema.json",
            "schemas/cloud/payloads/command-receipt-v1.schema.json",
            "schemas/cloud/payloads/command-result-v1.schema.json",
            "schemas/cloud/payloads/control-request-v1.schema.json",
            "schemas/cloud/payloads/control-response-v1.schema.json",
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json",
            "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
            "schemas/cloud/payloads/session-catalog-ack-v1.schema.json",
            "schemas/cloud/payloads/session-catalog-nack-v1.schema.json",
            "schemas/cloud/payloads/session-snapshot-v1.schema.json",
            "schemas/cloud/payloads/session-snapshot-v2.schema.json",
            "schemas/cloud/payloads/session-event-v1.schema.json",
            "schemas/cloud/payloads/session-event-v2.schema.json",
            "schemas/cloud/payloads/session-observe-open-v1.schema.json",
            "schemas/cloud/payloads/session-observe-open-v2.schema.json",
            "schemas/cloud/payloads/session-observe-close-v1.schema.json",
            "schemas/cloud/payloads/session-observe-close-v2.schema.json",
            "schemas/cloud/payloads/stream-ack-v1.schema.json",
            "schemas/cloud/payloads/stream-ack-v2.schema.json",
            "schemas/cloud/payloads/stream-nack-v1.schema.json",
            "schemas/cloud/payloads/stream-nack-v2.schema.json",
        )

        for relative_path in relative_paths:
            source = ROOT / relative_path
            consumer = (
                REPOSITORY_ROOT
                / "hermes-connector/src/hermes_connector/contracts/generated"
                / relative_path
            )
            self.assertTrue(consumer.is_file(), str(consumer))
            self.assertEqual(
                json.loads(consumer.read_text(encoding="utf-8")),
                json.loads(source.read_text(encoding="utf-8")),
                str(consumer),
            )

    def test_catalog_transport_contracts_are_synchronized_to_their_consumers(
        self,
    ) -> None:
        consumers = {
            "schemas/session-catalog-entry-v1.schema.json": (
                "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated",
                "hermes-connector/src/hermes_connector/contracts/generated",
                "hermes-cloud/src/hermes_cloud/contracts/generated",
            ),
            "schemas/local/session-catalog-rpc-v1.schema.json": (
                "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated",
                "hermes-connector/src/hermes_connector/contracts/generated",
            ),
            "schemas/cloud/payloads/session-catalog-ack-v1.schema.json": (
                "hermes-connector/src/hermes_connector/contracts/generated",
                "hermes-cloud/src/hermes_cloud/contracts/generated",
            ),
            "schemas/cloud/payloads/session-catalog-nack-v1.schema.json": (
                "hermes-connector/src/hermes_connector/contracts/generated",
                "hermes-cloud/src/hermes_cloud/contracts/generated",
            ),
        }
        for relative_path, consumer_roots in consumers.items():
            source = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
            for consumer_root in consumer_roots:
                consumer = REPOSITORY_ROOT / consumer_root / relative_path
                with self.subTest(contract=relative_path, consumer=str(consumer)):
                    self.assertTrue(consumer.is_file())
                    self.assertEqual(
                        json.loads(consumer.read_text(encoding="utf-8")),
                        source,
                    )

    def test_core_schema_never_contains_platform_specific_fields(self) -> None:
        forbidden = {"android", "ios", "web", "desktop"}

        for path in sorted((ROOT / "schemas").rglob("*.schema.json")):
            schema_text = path.read_text(encoding="utf-8").lower()
            for platform in forbidden:
                if (
                    platform == "desktop"
                    and path.name == "control-response-v1.schema.json"
                ):
                    # Public controller ownership uses the frozen desktop/mobile/none
                    # vocabulary; this is a display-safe role, not a platform field.
                    continue
                self.assertNotIn(f'"{platform}"', schema_text, str(path))

    def test_transport_schemas_allow_only_namespaced_extensions(self) -> None:
        for relative_path in (
            "schemas/local/gateway-handshake-v1.schema.json",
            "schemas/cloud/connector-envelope-v1.schema.json",
        ):
            schema = _load(relative_path)
            extension = schema["properties"]["extensions"]
            pattern = extension["propertyNames"]["pattern"]

            self.assertEqual(extension["type"], "object")
            self.assertEqual(extension["maxProperties"], 16)
            self.assertTrue(pattern.startswith("^"))
            self.assertTrue(pattern.endswith("$"))

    def test_cloud_message_catalog_covers_envelope_and_freezes_effects(self) -> None:
        envelope = _load("schemas/cloud/connector-envelope-v1.schema.json")
        catalog = _load("message-types-v1.json")
        entries = catalog["message_types"]
        by_name = {entry["name"]: entry for entry in entries}
        message_types = set(envelope["properties"]["message_type"]["enum"])

        self.assertEqual(set(by_name), message_types)
        self.assertEqual(len(entries), len(by_name))
        for entry in entries:
            if entry["status"] == "frozen":
                schema_path = entry["payload_schema"]
                self.assertIsInstance(schema_path, str)
                self.assertTrue((ROOT / schema_path).is_file(), entry["name"])
                self.assertNotEqual(entry["effect"], "none")
            else:
                self.assertEqual(entry["status"], "reserved")
                self.assertIsNone(entry["payload_schema"])
                self.assertEqual(entry["effect"], "none")

    def test_connector_session_payload_schemas_are_exact_and_platform_neutral(
        self,
    ) -> None:
        payload_dir = ROOT / "schemas/cloud/payloads"
        expected = {
            "command-deliver-v1.schema.json",
            "command-receipt-v1.schema.json",
            "command-result-v1.schema.json",
            "connector-hello-v1.schema.json",
            "connector-welcome-v1.schema.json",
            "connector-heartbeat-v1.schema.json",
            "control-request-v1.schema.json",
            "control-response-v1.schema.json",
            "session-catalog-snapshot-page-v1.schema.json",
            "session-catalog-event-v1.schema.json",
            "session-catalog-ack-v1.schema.json",
            "session-catalog-nack-v1.schema.json",
            "session-snapshot-v1.schema.json",
            "session-snapshot-v2.schema.json",
            "session-event-v1.schema.json",
            "session-event-v2.schema.json",
            "session-observe-open-v1.schema.json",
            "session-observe-open-v2.schema.json",
            "session-observe-close-v1.schema.json",
            "session-observe-close-v2.schema.json",
            "stream-ack-v1.schema.json",
            "stream-ack-v2.schema.json",
            "stream-nack-v1.schema.json",
            "stream-nack-v2.schema.json",
        }

        self.assertEqual(
            {path.name for path in payload_dir.glob("*.schema.json")},
            expected,
        )
        for path in sorted(payload_dir.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(schema["additionalProperties"], path.name)
            self.assertIn("extensions", schema["properties"])
            lowered = path.read_text(encoding="utf-8").lower()
            for platform in ("android", "ios", "web", "desktop"):
                if (
                    platform == "desktop"
                    and path.name == "control-response-v1.schema.json"
                ):
                    continue
                self.assertNotIn(f'"{platform}"', lowered, path.name)

    def test_command_lane_messages_are_frozen_with_exact_payload_authorities(
        self,
    ) -> None:
        catalog = _load("message-types-v1.json")
        by_name = {item["name"]: item for item in catalog["message_types"]}

        self.assertEqual(
            by_name["command.deliver"],
            {
                "name": "command.deliver",
                "direction": "cloud_to_connector",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/command-deliver-v1.schema.json"
                ),
                "effect": "command_persist_and_dispatch",
            },
        )
        self.assertEqual(
            by_name["command.receipt"],
            {
                "name": "command.receipt",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/command-receipt-v1.schema.json"
                ),
                "effect": "command_delivery_receipt",
            },
        )
        self.assertEqual(
            by_name["command.result"],
            {
                "name": "command.result",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/command-result-v1.schema.json"
                ),
                "effect": "command_terminal_result",
            },
        )

    def test_owner_control_lane_messages_are_frozen_with_exact_authorities(
        self,
    ) -> None:
        catalog = _load("message-types-v1.json")
        by_name = {item["name"]: item for item in catalog["message_types"]}

        self.assertIn("control.request", by_name)
        self.assertIn("control.response", by_name)
        self.assertEqual(
            by_name["control.request"],
            {
                "name": "control.request",
                "direction": "cloud_to_connector",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/control-request-v1.schema.json"
                ),
                "effect": "owner_control_request_route",
            },
        )
        self.assertEqual(
            by_name["control.response"],
            {
                "name": "control.response",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "payload_schema": (
                    "schemas/cloud/payloads/control-response-v1.schema.json"
                ),
                "effect": "owner_control_response_route",
            },
        )

    def test_owner_control_request_fixtures_cover_every_exact_operation_body(
        self,
    ) -> None:
        schema_path = "schemas/cloud/payloads/control-request-v1.schema.json"
        self.assertTrue((ROOT / schema_path).is_file())
        schema = _load(schema_path)
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        lifecycle_operations = {
            "control.transport.open",
            "session.control.acquire",
            "session.control.renew",
            "session.control.release",
            "session.control.status",
            "control.transport.close",
        }
        self.assertEqual(
            set(schema["properties"]["operation"]["enum"]),
            lifecycle_operations
            | {
                "session.command.status",
                "prompt.submit",
                "session.interrupt",
                "session.steer",
                "approval.respond",
                "clarify.respond",
            },
        )

        fixtures = [
            _load(f"fixtures/valid/control-request-{suffix}.json")
            for suffix in ("open", "acquire", "renew", "release", "status", "close")
        ]

        self.assertEqual(
            {fixture["operation"] for fixture in fixtures},
            lifecycle_operations,
        )
        for fixture in fixtures:
            validator.validate(fixture)

    def test_owner_control_request_schema_maps_every_mobile_action_exactly(
        self,
    ) -> None:
        schema = _load("schemas/cloud/payloads/control-request-v1.schema.json")
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        bodies = {
            "session.command.status": {
                "method": "prompt.submit",
                "client_request_id": "request-status",
            },
            "prompt.submit": {
                "lease_id": "opaque-lease",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "text": "Run focused tests",
            },
            "session.interrupt": {
                "lease_id": "opaque-lease",
                "client_request_id": "request-interrupt",
            },
            "session.steer": {
                "lease_id": "opaque-lease",
                "client_request_id": "request-steer",
                "text": "Focus on the first failure",
            },
            "approval.respond": {
                "lease_id": "opaque-lease",
                "client_request_id": "request-approval",
                "request_id": "pending-approval",
                "choice": "allow_once",
            },
            "clarify.respond": {
                "lease_id": "opaque-lease",
                "client_request_id": "request-clarify",
                "request_id": "pending-clarify",
                "choice_id": "choice-1",
            },
        }
        for operation, body in bodies.items():
            request = {
                "request_id": "11111111-1111-4111-8111-111111111111",
                "control_transport_id": (
                    "22222222-2222-4222-8222-222222222222"
                ),
                "operation": operation,
                "issued_at": "2026-08-01T00:00:00Z",
                "expires_at": "2026-08-01T00:00:03Z",
                "body": body,
            }
            self.assertEqual(list(validator.iter_errors(request)), [], operation)
            request["body"]["unexpected"] = True
            self.assertTrue(list(validator.iter_errors(request)), operation)

        status_request = {
            "request_id": "11111111-1111-4111-8111-111111111111",
            "control_transport_id": "22222222-2222-4222-8222-222222222222",
            "operation": "session.command.status",
            "issued_at": "2026-08-01T00:00:00Z",
            "expires_at": "2026-08-01T00:00:03Z",
            "body": {"client_request_id": "request-status"},
        }
        self.assertTrue(list(validator.iter_errors(status_request)))
        status_request["body"]["method"] = "session.command.status"
        self.assertTrue(list(validator.iter_errors(status_request)))

    def test_mobile_control_source_freezes_exact_command_status_owner_methods(
        self,
    ) -> None:
        source = _load("sources/mobile-control-v1.json")
        self.assertEqual(
            source["command_status_methods"],
            [
                "prompt.submit",
                "session.interrupt",
                "session.steer",
                "approval.respond",
                "clarify.respond",
            ],
        )

    def test_cloud_realtime_command_status_request_is_exact_and_method_scoped(
        self,
    ) -> None:
        realtime = _load("cloud-realtime-v1.json")
        validator = Draft202012Validator(realtime["schemas"]["control_request"])
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.command.status",
            "params": {
                "session_id": "88888888-8888-4888-8888-888888888888",
                "profile": "default",
                "method": "approval.respond",
                "client_request_id": "request-status",
            },
        }
        self.assertEqual(list(validator.iter_errors(request)), [])
        del request["params"]["method"]
        self.assertTrue(list(validator.iter_errors(request)))
        request["params"]["method"] = "session.command.status"
        self.assertTrue(list(validator.iter_errors(request)))
        request["params"]["method"] = "approval.respond"
        request["params"]["unexpected"] = True
        self.assertTrue(list(validator.iter_errors(request)))

    def test_mobile_control_error_catalog_projects_exactly_to_every_wire_authority(
        self,
    ) -> None:
        catalog = _load("sources/mobile-control-v1.json")["error_codes"]
        self.assertEqual(
            catalog,
            {
                "control_role_required": 4200,
                "control_contract_unsupported": 4201,
                "live_runtime_unavailable": 4202,
                "controller_conflict": 4203,
                "lease_required": 4204,
                "lease_expired": 4205,
                "lease_mismatch": 4206,
                "request_id_payload_conflict": 4207,
                "pending_request_conflict": 4208,
                "method_not_allowed": 4209,
                "command_unknown": 4210,
                "revision_conflict": 4211,
                "session_binding_mismatch": 4212,
                "invalid_pending_response": 4213,
                "owner_adapter_unavailable": 4214,
                "relay_overloaded": 4215,
                "deadline_exceeded_before_effect": 4306,
                "effect_unknown": 4307,
            },
        )

        realtime_error = _load("cloud-realtime-v1.json")["schemas"][
            "gateway_ready"
        ]["oneOf"][1]["properties"]["params"]["properties"]["payload"][
            "properties"
        ]["control_error_codes"]
        self.assertEqual(set(realtime_error["required"]), set(catalog))
        self.assertEqual(
            {
                name: definition["const"]
                for name, definition in realtime_error["properties"].items()
            },
            catalog,
        )
        self.assertEqual(
            _load("fixtures/valid/cloud-realtime-control-ready.json")["params"][
                "payload"
            ]["control_error_codes"],
            catalog,
        )

        response_error = _load(
            "schemas/cloud/payloads/control-response-v1.schema.json"
        )["$defs"]["error"]
        self.assertEqual(
            {
                variant["properties"]["reason"]["const"]:
                    variant["properties"]["code"]["const"]
                for variant in response_error["oneOf"]
            },
            catalog,
        )

    def test_mobile_control_source_declares_scoped_observer_without_session_target(
        self,
    ) -> None:
        source = _load("sources/mobile-control-v1.json")
        self.assertEqual(
            source["scoped_observer_ticket_request"],
            {
                "connection_role": "observer",
                "client_instance_id": "11111111-1111-4111-8111-111111111111",
            },
        )

    def test_controller_status_kind_and_label_are_canonical_and_conditional(
        self,
    ) -> None:
        schema = _load("schemas/cloud/payloads/control-response-v1.schema.json")
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        base = _load("fixtures/valid/control-response-status.json")

        for kind, label in (
            ("desktop", "Hermes Desktop"),
            ("mobile", "Hermes Mobile"),
            ("none", None),
        ):
            response = json.loads(json.dumps(base))
            response["result"]["controller_kind"] = kind
            response["result"]["controller_label"] = label
            self.assertEqual(list(validator.iter_errors(response)), [])

        for kind, label in (
            ("local", "Hermes Desktop"),
            ("none", "No controller"),
            ("desktop", None),
            ("mobile", None),
        ):
            response = json.loads(json.dumps(base))
            response["result"]["controller_kind"] = kind
            response["result"]["controller_label"] = label
            self.assertTrue(list(validator.iter_errors(response)), (kind, label))

    def test_owner_control_response_lease_is_only_in_successful_acquire_or_renew(
        self,
    ) -> None:
        schema_path = "schemas/cloud/payloads/control-response-v1.schema.json"
        self.assertTrue((ROOT / schema_path).is_file())
        schema = _load(schema_path)
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        success_paths = (
            "fixtures/valid/control-response-open.json",
            "fixtures/valid/control-response-acquire.json",
            "fixtures/valid/control-response-renew.json",
            "fixtures/valid/control-response-release.json",
            "fixtures/valid/control-response-status.json",
            "fixtures/valid/control-response-close.json",
        )

        for path in success_paths:
            fixture = _load(path)
            validator.validate(fixture)
            result = fixture["result"]
            self.assertEqual(
                "lease_id" in result,
                fixture["operation"]
                in {"session.control.acquire", "session.control.renew"},
                path,
            )

        for path in (
            "fixtures/invalid/control-response-status-lease-leak.json",
            "fixtures/invalid/control-response-failed-lease-leak.json",
            "fixtures/invalid/control-response-acquire-missing-lease.json",
        ):
            fixture = _load(path)
            self.assertTrue(list(validator.iter_errors(fixture)), path)

    def test_connector_session_state_rules_are_documented(self) -> None:
        protocol = (ROOT / "SESSION_PROTOCOL.md").read_text(encoding="utf-8")

        self.assertIn("DISCONNECTED --> CONNECTING", protocol)
        self.assertIn("CONNECTING --> NEGOTIATING", protocol)
        self.assertIn("NEGOTIATING --> ACTIVE", protocol)
        self.assertIn("ACTIVE --> RECONCILING", protocol)
        self.assertIn("reset_required", protocol)
        self.assertIn("must not trigger", protocol)
        self.assertIn("same epoch", protocol)
        self.assertIn("including sequence 0", protocol)
        self.assertIn("new epoch", protocol)
        self.assertIn("replay settled frames from the prior epoch", protocol)


if __name__ == "__main__":
    unittest.main()
