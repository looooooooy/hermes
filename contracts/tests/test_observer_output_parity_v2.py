from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]

BASE_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "agent.terminal.output",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}
LIFECYCLE_EVENT_TYPES = {
    "todo.update",
    "subagent.update",
    "tool.update",
    "terminal.update",
}
MERGEABLE_EVENT_TYPES = {
    "message.delta",
    "agent.terminal.output",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}
V2_MESSAGE_TYPES = {
    "session.observe.open.v2",
    "session.observe.close.v2",
    "session.snapshot.v2",
    "session.event.v2",
    "stream.ack.v2",
    "stream.nack.v2",
}


def _load(relative_path: str) -> dict[str, object]:
    value = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validator(relative_path: str) -> Draft202012Validator:
    resources = []
    for path in (ROOT / "schemas").rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Draft202012Validator(
        _load(relative_path),
        format_checker=FormatChecker(),
        registry=Registry().with_resources(resources),
    )


def _inline_validator(schema: dict[str, object]) -> Draft202012Validator:
    resources = []
    for path in (ROOT / "schemas").rglob("*.schema.json"):
        resource = json.loads(path.read_text(encoding="utf-8"))
        resources.append((resource["$id"], Resource.from_contents(resource)))
    return Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
        registry=Registry().with_resources(resources),
    )


def _projection_errors(snapshot: dict[str, object]) -> list[str]:
    errors: list[str] = []
    running = snapshot.get("running")
    status = snapshot.get("status")
    if isinstance(running, bool) and isinstance(status, str):
        if running != (status in {"running", "working", "streaming"}):
            errors.append("snapshot running and status disagree")
    baseline = snapshot.get("snapshot_event_sequence")
    head = snapshot.get("event_sequence")
    if isinstance(baseline, int) and isinstance(head, int) and baseline > head:
        errors.append("snapshot cursor exceeds head")
    for collection, id_field in (
        ("todo_sections", "section_id"),
        ("subagents", "subagent_id"),
        ("tools", "tool_call_id"),
        ("terminals", "process_id"),
    ):
        values = snapshot.get(collection)
        if not isinstance(values, list):
            continue
        identities = [
            (value.get("turn_id"), value.get(id_field))
            for value in values
            if isinstance(value, dict)
        ]
        if len(identities) != len(set(identities)):
            errors.append(f"{collection} contains duplicate identities")
        for value in values:
            if (
                isinstance(value, dict)
                and isinstance(value.get("first_event_sequence"), int)
                and isinstance(baseline, int)
                and value["first_event_sequence"] > baseline
            ):
                errors.append(f"{collection} first occurrence exceeds snapshot cursor")
    todo_sections = snapshot.get("todo_sections")
    if isinstance(todo_sections, list):
        for section in todo_sections:
            if not isinstance(section, dict) or not isinstance(section.get("items"), list):
                continue
            item_ids = [
                item.get("id") for item in section["items"] if isinstance(item, dict)
            ]
            if len(item_ids) != len(set(item_ids)):
                errors.append("todo item ids are not unique")

    subagents = snapshot.get("subagents")
    if not isinstance(subagents, list):
        return errors
    by_id = {
        (node["turn_id"], node["subagent_id"]): node
        for node in subagents
        if isinstance(node, dict)
    }
    for key, node in by_id.items():
        parent = node.get("parent_subagent_id")
        if parent is not None and (key[0], parent) not in by_id:
            errors.append("subagent parent is missing")
            continue
        seen: set[tuple[object, object]] = set()
        cursor = key
        depth = 0
        while cursor in by_id:
            if cursor in seen:
                errors.append("subagent parent cycle")
                break
            seen.add(cursor)
            depth += 1
            if depth > 8:
                errors.append("subagent depth exceeds 8")
                break
            next_parent = by_id[cursor].get("parent_subagent_id")
            if next_parent is None:
                break
            cursor = (cursor[0], next_parent)
    return errors


def _event_policy_errors(event: dict[str, object]) -> list[str]:
    errors: list[str] = []
    event_type = event.get("type")
    sequence = event.get("event_sequence")
    sequence_start = event.get("event_sequence_start")
    if sequence_start is not None:
        if event_type not in MERGEABLE_EVENT_TYPES:
            errors.append("event type is not mergeable")
        if isinstance(sequence, int) and sequence_start > sequence:
            errors.append("event range is reversed")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return errors
    first = payload.get("first_event_sequence")
    revision = payload.get("revision")
    if isinstance(first, int) and isinstance(sequence, int):
        if first > sequence:
            errors.append("first_event_sequence exceeds event_sequence")
        if first == sequence and revision != 1:
            errors.append("initial revision must equal 1")
    if event_type == "todo.update" and payload.get("operation") == "upsert":
        items = payload.get("items")
        if isinstance(items, list):
            ids = [item.get("id") for item in items if isinstance(item, dict)]
            if len(ids) != len(set(ids)):
                errors.append("todo item ids are not unique")
    progress = payload.get("progress")
    if isinstance(progress, dict):
        current = progress.get("current")
        total = progress.get("total")
        if isinstance(current, int) and isinstance(total, int) and current > total:
            errors.append("progress current exceeds total")
    if event_type == "terminal.update" and payload.get("operation") == "upsert":
        status = payload.get("status")
        exit_code = payload.get("exit_code")
        if status == "completed" and exit_code != 0:
            errors.append("completed terminal requires zero exit_code")
        if status == "failed" and (not isinstance(exit_code, int) or exit_code == 0):
            errors.append("failed terminal requires nonzero exit_code")
        if status in {"running", "interrupted", "unknown"} and exit_code is not None:
            errors.append("non-exited terminal must omit exit_code")
    return errors


class ObserverOutputParityV2ContractTest(unittest.TestCase):
    def test_capability_and_machine_policy_freeze_v2_semantics(self) -> None:
        capability = _load("schemas/capability-manifest-v1.schema.json")[
            "properties"
        ]["capabilities"]["items"]["enum"]
        self.assertIn("session.observe.output-parity.v1", capability)
        self.assertIn(
            "session.observe.output-parity.v1",
            _load("fixtures/valid/capability-manifest.json")["capabilities"],
        )
        self.assertIn(
            "session.observe.output-parity.v1",
            _load("fixtures/valid/connector-hello-payload.json")[
                "optional_capabilities"
            ],
        )
        self.assertIn(
            "session.observe.output-parity.v1",
            _load("fixtures/valid/connector-welcome-payload.json")[
                "accepted_capabilities"
            ],
        )

        policy = _load("observer-output-parity-v2.json")
        self.assertEqual(policy["contract"], "observer-output-parity")
        self.assertEqual(policy["version"], 2)
        self.assertEqual(
            policy["capability"],
            "session.observe.output-parity.v1",
        )
        self.assertEqual(
            set(policy["event_types"]),
            BASE_EVENT_TYPES | LIFECYCLE_EVENT_TYPES,
        )
        self.assertEqual(
            set(policy["non_mergeable_lifecycle_event_types"]),
            LIFECYCLE_EVENT_TYPES,
        )
        self.assertEqual(policy["ordering"]["transport"], "event_sequence")
        self.assertEqual(
            policy["ordering"]["stable_projection"],
            ["first_event_sequence", "entity_id"],
        )
        self.assertEqual(policy["snapshot"]["install"], "atomic_replace")
        self.assertEqual(
            policy["snapshot"]["replay_starts_at"],
            "snapshot_event_sequence + 1",
        )
        self.assertEqual(policy["runtime_rollover"], "discard_then_resnapshot")
        self.assertEqual(policy["subagent_tree"]["max_depth"], 8)
        self.assertEqual(policy["subagent_tree"]["max_nodes"], 128)
        self.assertEqual(policy["subagent_tree"]["orphan"], "reject")
        self.assertEqual(policy["subagent_tree"]["cycle"], "reject")
        self.assertEqual(policy["delete"]["requires_terminal"], True)
        self.assertEqual(policy["delete"]["subagent_requires_leaf"], True)
        self.assertEqual(policy["security"]["raw_args"], "forbidden")
        self.assertEqual(policy["security"]["raw_tool_output"], "forbidden")
        self.assertEqual(policy["security"]["full_approval_payload"], "forbidden")
        self.assertEqual(policy["limits"]["max_frame_bytes"], 262_144)
        self.assertEqual(policy["limits"]["max_safe_integer"], 9_007_199_254_740_991)

    def test_v1_remains_exact_and_does_not_accept_v2_projection(self) -> None:
        event_v1 = _load("schemas/cloud/payloads/session-event-v1.schema.json")
        snapshot_v1 = _load("schemas/cloud/payloads/session-snapshot-v1.schema.json")
        realtime_v1 = _load("cloud-realtime-v1.json")
        self.assertEqual(set(event_v1["properties"]["type"]["enum"]), BASE_EVENT_TYPES)
        self.assertTrue(
            {"todo_sections", "subagents", "tools", "terminals"}.isdisjoint(
                snapshot_v1["properties"]
            )
        )
        ready = realtime_v1["schemas"]["gateway_ready"]["oneOf"][0]
        observer_contract = ready["properties"]["params"]["properties"][
            "payload"
        ]["properties"]["observer_contract"]
        self.assertEqual(observer_contract, {"const": 1})
        Draft202012Validator(event_v1).validate(
            _load("fixtures/compatibility/session-event-n1.json")
        )

        v2_event = _load("fixtures/valid/session-event-v2-todo-upsert.json")
        self.assertTrue(list(Draft202012Validator(event_v1).iter_errors(v2_event)))

    def test_internal_v2_message_catalog_and_envelope_are_versioned(self) -> None:
        catalog = _load("message-types-v1.json")
        by_name = {item["name"]: item for item in catalog["message_types"]}
        envelope_types = set(
            _load("schemas/cloud/connector-envelope-v1.schema.json")["properties"][
                "message_type"
            ]["enum"]
        )
        self.assertTrue(V2_MESSAGE_TYPES <= set(by_name))
        self.assertTrue(V2_MESSAGE_TYPES <= envelope_types)
        for message_type in V2_MESSAGE_TYPES:
            item = by_name[message_type]
            self.assertEqual(item["status"], "frozen")
            self.assertTrue((ROOT / item["payload_schema"]).is_file())

    def test_internal_v2_valid_fixtures_are_exact(self) -> None:
        cases = (
            ("session-event-v2.schema.json", "session-event-v2-todo-upsert.json"),
            ("session-event-v2.schema.json", "session-event-v2-subagent-upsert.json"),
            ("session-event-v2.schema.json", "session-event-v2-tool-upsert.json"),
            ("session-event-v2.schema.json", "session-event-v2-terminal-upsert.json"),
            ("session-snapshot-v2.schema.json", "session-snapshot-v2-payload.json"),
            ("session-observe-open-v2.schema.json", "session-observe-open-v2-payload.json"),
            ("session-observe-close-v2.schema.json", "session-observe-close-v2-payload.json"),
            ("stream-ack-v2.schema.json", "stream-ack-v2-payload.json"),
            ("stream-nack-v2.schema.json", "stream-nack-v2-payload.json"),
        )
        for schema_name, fixture_name in cases:
            with self.subTest(fixture=fixture_name):
                _validator(f"schemas/cloud/payloads/{schema_name}").validate(
                    _load(f"fixtures/valid/{fixture_name}")
                )

    def test_lifecycle_payloads_reject_raw_or_unknown_fields(self) -> None:
        validator = _validator("schemas/cloud/payloads/session-event-v2.schema.json")
        tool = _load("fixtures/valid/session-event-v2-tool-upsert.json")
        tool["payload"]["raw_args"] = {"command": "must-not-cross"}
        self.assertTrue(list(validator.iter_errors(tool)))

        terminal = _load("fixtures/valid/session-event-v2-terminal-upsert.json")
        terminal["payload"]["output"] = "must-not-cross"
        self.assertTrue(list(validator.iter_errors(terminal)))

        deletion = copy.deepcopy(tool)
        deletion["payload"] = {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": 2,
            "first_event_sequence": 3,
            "operation": "delete",
            "name": "Delete must not carry state",
        }
        self.assertTrue(list(validator.iter_errors(deletion)))

    def test_lifecycle_events_are_nonmergeable_and_semantically_consistent(self) -> None:
        validator = _validator("schemas/cloud/payloads/session-event-v2.schema.json")
        lifecycle = _load("fixtures/valid/session-event-v2-tool-upsert.json")
        lifecycle["event_sequence_start"] = lifecycle["event_sequence"]
        self.assertTrue(list(validator.iter_errors(lifecycle)))
        self.assertIn("event type is not mergeable", _event_policy_errors(lifecycle))

        initial = _load("fixtures/valid/session-event-v2-todo-upsert.json")
        initial["payload"]["revision"] = 2
        self.assertIn("initial revision must equal 1", _event_policy_errors(initial))
        initial["payload"]["revision"] = 1
        initial["payload"]["first_event_sequence"] = initial["event_sequence"] + 1
        self.assertIn(
            "first_event_sequence exceeds event_sequence",
            _event_policy_errors(initial),
        )

        duplicate_item = _load("fixtures/valid/session-event-v2-todo-upsert.json")
        duplicate_item["payload"]["items"].append(
            copy.deepcopy(duplicate_item["payload"]["items"][0])
        )
        self.assertIn("todo item ids are not unique", _event_policy_errors(duplicate_item))

        progress = _load("fixtures/valid/session-event-v2-subagent-upsert.json")
        progress["payload"]["progress"] = {"current": 4, "total": 3}
        self.assertIn("progress current exceeds total", _event_policy_errors(progress))

    def test_terminal_lifecycle_has_one_process_identity_and_exit_consistency(self) -> None:
        validator = _validator("schemas/cloud/payloads/session-event-v2.schema.json")
        terminal = _load("fixtures/valid/session-event-v2-terminal-upsert.json")
        terminal["payload"]["stream"] = "stdout"
        self.assertTrue(list(validator.iter_errors(terminal)))

        completed = _load("fixtures/valid/session-event-v2-terminal-upsert.json")
        completed["payload"]["status"] = "completed"
        self.assertIn(
            "completed terminal requires zero exit_code",
            _event_policy_errors(completed),
        )
        completed["payload"]["exit_code"] = 0
        self.assertEqual(_event_policy_errors(completed), [])

        failed = copy.deepcopy(completed)
        failed["payload"]["status"] = "failed"
        self.assertIn(
            "failed terminal requires nonzero exit_code",
            _event_policy_errors(failed),
        )

    def test_v2_output_deltas_require_turn_scoped_composite_identity(self) -> None:
        validator = _validator("schemas/cloud/payloads/session-event-v2.schema.json")
        terminal_output = {
            "observer_contract": 2,
            "profile": "default",
            "runtime_generation": "runtime-1",
            "session_key": "session-1",
            "session_id": "runtime-session-1",
            "type": "agent.terminal.output",
            "event_sequence": 5,
            "payload": {
                "turn_id": "turn-1",
                "process_id": "process-1",
                "stream": "stdout",
                "text": "display-safe output",
            },
        }
        validator.validate(terminal_output)
        del terminal_output["payload"]["turn_id"]
        self.assertTrue(list(validator.iter_errors(terminal_output)))

        tool_output = copy.deepcopy(terminal_output)
        tool_output["type"] = "tool.output.delta"
        tool_output["payload"] = {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "text": "display-safe output",
        }
        validator.validate(tool_output)
        del tool_output["payload"]["turn_id"]
        self.assertTrue(list(validator.iter_errors(tool_output)))

    def test_snapshot_semantics_reject_duplicates_or_invalid_subagent_graph(self) -> None:
        snapshot = _load("fixtures/valid/session-snapshot-v2-payload.json")
        self.assertEqual(_projection_errors(snapshot), [])

        duplicate = copy.deepcopy(snapshot)
        duplicate["tools"].append(copy.deepcopy(duplicate["tools"][0]))
        self.assertIn("tools contains duplicate identities", _projection_errors(duplicate))

        orphan = copy.deepcopy(snapshot)
        orphan["subagents"][0]["parent_subagent_id"] = "missing-parent"
        self.assertIn("subagent parent is missing", _projection_errors(orphan))

        cycle = copy.deepcopy(snapshot)
        cycle["subagents"][0]["parent_subagent_id"] = cycle["subagents"][1][
            "subagent_id"
        ]
        cycle["subagents"][1]["parent_subagent_id"] = cycle["subagents"][0][
            "subagent_id"
        ]
        self.assertIn("subagent parent cycle", _projection_errors(cycle))

        future = copy.deepcopy(snapshot)
        future["tools"][0]["first_event_sequence"] = (
            future["snapshot_event_sequence"] + 1
        )
        self.assertIn(
            "tools first occurrence exceeds snapshot cursor",
            _projection_errors(future),
        )

        bad_status = copy.deepcopy(snapshot)
        bad_status["running"] = False
        self.assertIn(
            "snapshot running and status disagree",
            _projection_errors(bad_status),
        )

        bad_cursor = copy.deepcopy(snapshot)
        bad_cursor["snapshot_event_sequence"] = bad_cursor["event_sequence"] + 1
        self.assertIn("snapshot cursor exceeds head", _projection_errors(bad_cursor))

    def test_snapshot_replay_uses_complete_v2_event_authority(self) -> None:
        validator = _validator("schemas/cloud/payloads/session-snapshot-v2.schema.json")
        snapshot = _load("fixtures/valid/session-snapshot-v2-payload.json")
        invalid_replay = _load("fixtures/valid/session-event-v2-tool-upsert.json")
        invalid_replay["event_sequence"] = 5
        invalid_replay["payload"]["raw_args"] = {"command": "must-not-cross"}
        snapshot["event_sequence"] = 5
        snapshot["replay_events"] = [invalid_replay]
        self.assertTrue(list(validator.iter_errors(snapshot)))

        invalid_replay["payload"].pop("raw_args")
        invalid_replay["event_sequence_start"] = 5
        self.assertTrue(list(validator.iter_errors(snapshot)))

    def test_external_v2_ticket_ready_and_subscribe_bind_one_exact_version(self) -> None:
        openapi = _load("openapi/cloud-api-v1.json")
        request_schema = openapi["components"]["schemas"]["WebSocketTicketRequest"]
        response_schema = openapi["components"]["schemas"]["WebSocketTicketResponse"]
        request = _load("fixtures/valid/cloud-api-observer-ticket-v2-request.json")
        response = _load("fixtures/valid/cloud-api-observer-ticket-v2.json")
        Draft202012Validator(request_schema, format_checker=FormatChecker()).validate(
            request
        )
        Draft202012Validator(response_schema).validate(response)
        self.assertEqual(request["observer_contract"], 2)
        self.assertEqual(response["observer_contract"], 2)

        realtime = _load("cloud-realtime-v2.json")
        for schema_name, fixture_name in (
            ("gateway_ready", "cloud-realtime-v2-ready.json"),
            ("observe_subscribe_request", "cloud-realtime-v2-subscribe.json"),
            ("observe_subscribe_result", "cloud-realtime-v2-subscribe-result.json"),
            ("session_event", "cloud-realtime-v2-event.json"),
        ):
            _inline_validator(realtime["schemas"][schema_name]).validate(
                _load(f"fixtures/valid/{fixture_name}")
            )

        self.assertEqual(realtime["observer_contract"], 2)
        self.assertEqual(realtime["selection"]["ticket_claim"], "observer_contract")
        self.assertEqual(realtime["selection"]["unsupported"], "reject_without_ticket")
        self.assertEqual(realtime["selection"]["fallback"], "forbidden")
        self.assertEqual(realtime["selection"]["mismatch"], "fail_closed")

    def test_v2_realtime_rejects_v1_subscribe_and_silent_downgrade(self) -> None:
        realtime = _load("cloud-realtime-v2.json")
        subscribe_validator = _inline_validator(
            realtime["schemas"]["observe_subscribe_request"]
        )
        self.assertTrue(
            list(
                subscribe_validator.iter_errors(
                    _load("fixtures/valid/cloud-realtime-subscribe.json")
                )
            )
        )

    def test_v2_subscribe_declares_and_validates_canonical_agent_scope(self) -> None:
        realtime = _load("cloud-realtime-v2.json")
        schema = realtime["schemas"]["observe_subscribe_request"]
        params = schema["properties"]["params"]
        self.assertNotIn("agent_id", params["required"])
        self.assertEqual(
            params["properties"]["agent_id"],
            {
                "type": "string",
                "format": "uuid",
                "pattern": (
                    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                    "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
                ),
            },
        )
        frame = _load("fixtures/valid/cloud-realtime-v2-subscribe.json")
        frame["params"]["agent_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        _inline_validator(schema).validate(frame)
        frame["params"]["agent_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        self.assertTrue(list(_inline_validator(schema).iter_errors(frame)))

    def test_cloud_realtime_v2_has_one_event_schema_authority(self) -> None:
        realtime = _load("cloud-realtime-v2.json")
        event_ref = {
            "$ref": (
                "https://contracts.hermes.local/public/"
                "session-event-v2.schema.json"
            )
        }
        self.assertEqual(
            realtime["schemas"]["session_event"]["properties"]["params"],
            event_ref,
        )
        self.assertNotIn("$defs", realtime["schemas"]["session_event"])
        subscribe = realtime["schemas"]["observe_subscribe_result"]
        self.assertNotIn("externalEventParams", subscribe["$defs"])
        replay_items = subscribe["properties"]["result"]["properties"][
            "replay_events"
        ]["items"]
        self.assertEqual(replay_items, event_ref)

    def test_manifest_registers_v2_invalid_and_n1_evidence(self) -> None:
        manifest = _load("fixtures/manifest.json")["external_profiles"]
        expected = {
            "connector_observer_v2": {
                "invalid": {
                    "fixtures/invalid/session-event-v2-lifecycle-range.json",
                    "fixtures/invalid/session-event-v2-tool-raw-args.json",
                    "fixtures/invalid/session-event-v2-terminal-stream.json",
                },
                "n_1": {
                    "fixtures/compatibility/session-snapshot-n1.json",
                    "fixtures/compatibility/session-event-n1.json",
                },
            },
            "cloud_api_observer_v2": {
                "invalid": {
                    "fixtures/invalid/cloud-api-observer-ticket-v2-request-downgrade.json",
                    "fixtures/invalid/cloud-api-observer-ticket-v2-response-downgrade.json",
                },
                "n_1": {
                    "fixtures/compatibility/cloud-api-observer-ticket-request-n1.json",
                    "fixtures/compatibility/cloud-api-observer-ticket-response-n1.json",
                },
            },
            "cloud_realtime_v2": {
                "invalid": {
                    "fixtures/invalid/cloud-realtime-v2-ready-downgrade.json",
                    "fixtures/invalid/cloud-realtime-v2-subscribe-missing-contract.json",
                    "fixtures/invalid/cloud-realtime-v2-event-invalid-profile.json",
                },
                "n_1": {"fixtures/compatibility/cloud-realtime-ready-n1.json"},
            },
        }
        for profile_name, categories in expected.items():
            profile = manifest[profile_name]
            for category, paths in categories.items():
                self.assertEqual(set(profile[category]), paths)
                self.assertTrue(all((ROOT / path).is_file() for path in paths))

        internal = _validator("schemas/cloud/payloads/session-event-v2.schema.json")
        for name in (
            "session-event-v2-lifecycle-range.json",
            "session-event-v2-tool-raw-args.json",
            "session-event-v2-terminal-stream.json",
        ):
            self.assertTrue(
                list(internal.iter_errors(_load(f"fixtures/invalid/{name}"))),
                name,
            )

        openapi = _load("openapi/cloud-api-v1.json")
        for schema_name, fixture_name in (
            (
                "WebSocketTicketRequest",
                "cloud-api-observer-ticket-v2-request-downgrade.json",
            ),
            (
                "WebSocketTicketResponse",
                "cloud-api-observer-ticket-v2-response-downgrade.json",
            ),
        ):
            self.assertTrue(
                list(
                    Draft202012Validator(
                        openapi["components"]["schemas"][schema_name]
                    ).iter_errors(_load(f"fixtures/invalid/{fixture_name}"))
                )
            )

        realtime = _load("cloud-realtime-v2.json")
        for schema_name, fixture_name in (
            ("gateway_ready", "cloud-realtime-v2-ready-downgrade.json"),
            (
                "observe_subscribe_request",
                "cloud-realtime-v2-subscribe-missing-contract.json",
            ),
        ):
            self.assertTrue(
                list(
                    _inline_validator(realtime["schemas"][schema_name]).iter_errors(
                        _load(f"fixtures/invalid/{fixture_name}")
                    )
                )
            )

        ready_v2 = _load("fixtures/valid/cloud-realtime-v2-ready.json")
        ready_v2["params"]["payload"]["observer_contract"] = 1
        self.assertTrue(
            list(
                _inline_validator(realtime["schemas"]["gateway_ready"]).iter_errors(
                    ready_v2
                )
            )
        )

    def test_sync_tool_declares_v2_contracts_for_later_consumer_adoption(self) -> None:
        sync_source = (ROOT / "tools/sync_consumers.py").read_text(encoding="utf-8")
        for relative_path in (
            "observer-output-parity-v2.json",
            "cloud-realtime-v2.json",
            "session-snapshot-v2.schema.json",
            "session-event-v2.schema.json",
            "session-observe-open-v2.schema.json",
            "session-observe-close-v2.schema.json",
            "stream-ack-v2.schema.json",
            "stream-nack-v2.schema.json",
        ):
            self.assertIn(relative_path, sync_source)
        self.assertIn("SYNCHRONIZED_CONTRACTS", sync_source)
        self.assertNotIn("PLANNED_SYNCHRONIZED_CONTRACTS", sync_source)
        self.assertNotIn("--include-planned", sync_source)
        for name in ("session-event-v2.schema.json", "session-snapshot-v2.schema.json"):
            plugin_copy = (
                ROOT.parent
                / "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated"
                / "schemas/cloud/payloads"
                / name
            )
            self.assertEqual(
                json.loads(plugin_copy.read_text(encoding="utf-8")),
                _load(f"schemas/cloud/payloads/{name}"),
            )

        realtime = _load("cloud-realtime-v2.json")
        for consumer_root in (
            ROOT.parent / "hermes-cloud/src/hermes_cloud/contracts/generated",
            ROOT.parent / "hermes-web/src/shared/contracts/generated",
            ROOT.parent / "hermes-android/core/protocol/src/test/resources/contracts",
        ):
            consumer_realtime = json.loads(
                (consumer_root / "cloud-realtime-v2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(consumer_realtime, realtime)
            for dependency in realtime["schema_dependencies"]:
                consumer_dependency = consumer_root / dependency
                self.assertTrue(consumer_dependency.is_file(), str(consumer_dependency))
                self.assertEqual(
                    json.loads(consumer_dependency.read_text(encoding="utf-8")),
                    _load(dependency),
                )


if __name__ == "__main__":
    unittest.main()
