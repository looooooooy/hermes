from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent
STABLE_SESSION_ID = "88888888-8888-4888-8888-888888888888"
JS_SAFE_INTEGER_MAXIMUM = 9_007_199_254_740_991
UUID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _load(relative_path: str) -> dict[str, object]:
    value = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _registry() -> Registry:
    resources = []
    for path in (ROOT / "schemas").rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _validator(relative_path: str) -> Draft202012Validator:
    return Draft202012Validator(
        _load(relative_path),
        format_checker=FormatChecker(),
        registry=_registry(),
    )


def _openapi_validator(
    api: dict[str, object],
    component: str,
) -> Draft202012Validator:
    return Draft202012Validator(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$ref": f"#/components/schemas/{component}",
            "components": api["components"],
        },
        format_checker=FormatChecker(),
        registry=_registry(),
    )


class SessionCatalogSpecGateTests(unittest.TestCase):
    def test_stable_public_uuid_is_the_only_cross_lane_session_identity(self) -> None:
        catalog = _load("fixtures/valid/cloud-api-session-catalog-only-page.json")
        projection = catalog["sessions"][0]
        self.assertEqual(projection["id"], STABLE_SESSION_ID)
        self.assertEqual(projection["_lineage_root_id"], "durable-session-real")

        api = _load("openapi/cloud-api-v1.json")
        self.assertIn("/api/sessions/{session_id}", api["paths"])
        self.assertIn("/api/sessions/{session_id}/messages", api["paths"])
        self.assertNotIn("/api/sessions/{session_key}", api["paths"])
        self.assertEqual(
            api["components"]["parameters"]["SessionId"]["schema"],
            {
                "type": "string",
                "format": "uuid",
                "pattern": UUID_PATTERN,
            },
        )
        ticket_request = _load("fixtures/valid/cloud-api-control-ticket-request.json")
        self.assertEqual(ticket_request["session_id"], STABLE_SESSION_ID)
        self.assertNotIn("session_key", ticket_request)
        _openapi_validator(api, "WebSocketTicketRequest").validate(ticket_request)

        transcript = _load("fixtures/valid/cloud-api-session-transcript.json")
        self.assertEqual(transcript["session_id"], STABLE_SESSION_ID)

        realtime = _load("cloud-realtime-v2.json")
        realtime_cases = (
            ("observe_subscribe_request", "cloud-realtime-v2-subscribe.json", "params"),
            (
                "observe_subscribe_result",
                "cloud-realtime-v2-subscribe-result.json",
                "result",
            ),
            ("session_event", "cloud-realtime-v2-event.json", "params"),
        )
        for schema_name, fixture_name, payload_key in realtime_cases:
            with self.subTest(schema=schema_name):
                fixture = _load(f"fixtures/valid/{fixture_name}")
                Draft202012Validator(
                    realtime["schemas"][schema_name],
                    format_checker=FormatChecker(),
                    registry=_registry(),
                ).validate(fixture)
                payload = fixture[payload_key]
                self.assertEqual(payload["session_id"], STABLE_SESSION_ID)
                self.assertNotIn("session_key", payload)
                self.assertNotIn("runtime_session_id", payload)

        control = _load("fixtures/valid/cloud-realtime-control-method.json")
        self.assertEqual(control["params"]["session_id"], STABLE_SESSION_ID)
        self.assertNotIn("session_key", control["params"])

    def test_catalog_only_projection_cannot_fabricate_transcript_facts(self) -> None:
        api = _load("openapi/cloud-api-v1.json")
        validator = _openapi_validator(api, "SessionPage")
        nullable_facts = (
            "title",
            "started_at",
            "last_active",
            "ended_at",
            "preview",
            "source",
            "model",
            "cwd",
            "git_branch",
        )
        zero_facts = (
            "message_count",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
        )
        for field in nullable_facts:
            with self.subTest(field=field):
                page = _load("fixtures/valid/cloud-api-session-catalog-only-page.json")
                page["sessions"][0][field] = 1 if field.endswith("_at") else "forged"
                self.assertTrue(list(validator.iter_errors(page)))
        for field in zero_facts:
            with self.subTest(field=field):
                page = _load("fixtures/valid/cloud-api-session-catalog-only-page.json")
                page["sessions"][0][field] = 1
                self.assertTrue(list(validator.iter_errors(page)))
        page = _load("fixtures/valid/cloud-api-session-catalog-only-page.json")
        page["sessions"][0]["transcript_available"] = True
        self.assertTrue(list(validator.iter_errors(page)))

        invalid_path = (
            "fixtures/invalid/cloud-api-session-catalog-only-fabricated-transcript.json"
        )
        self.assertTrue((ROOT / invalid_path).is_file())
        self.assertTrue(list(validator.iter_errors(_load(invalid_path))))
        self.assertIn(
            invalid_path,
            _load("fixtures/manifest.json")["external_profiles"]["cloud_api_v1"][
                "invalid"
            ],
        )

    def test_catalog_semantic_vectors_are_registered_and_match_validator(self) -> None:
        valid_vectors = {
            "fixtures/conformance/session-catalog-current-writer-snapshot.json",
            "fixtures/conformance/session-catalog-new-generation-rollover.json",
            "fixtures/conformance/session-catalog-stable-session-id-reuse.json",
            "fixtures/conformance/session-catalog-event-revisions.json",
        }
        invalid_vectors = {
            "fixtures/invalid/session-catalog-semantic-atomic-repeat-page-gap.json",
            "fixtures/invalid/session-catalog-semantic-duplicate-same-page.json",
            "fixtures/invalid/session-catalog-semantic-duplicate-cross-page.json",
            "fixtures/invalid/session-catalog-semantic-page-gap.json",
            "fixtures/invalid/session-catalog-semantic-scope-change.json",
            "fixtures/invalid/session-catalog-semantic-revision-change.json",
            "fixtures/invalid/session-catalog-semantic-event-gap.json",
            "fixtures/invalid/session-catalog-semantic-late-old-generation.json",
            "fixtures/invalid/session-catalog-semantic-late-old-generation-snapshot.json",
            "fixtures/invalid/session-catalog-semantic-stale-writer.json",
            "fixtures/invalid/session-catalog-semantic-payload-identity.json",
            "fixtures/invalid/session-catalog-semantic-stable-session-id-changed.json",
            "fixtures/invalid/session-catalog-semantic-stable-session-id-invalid.json",
            "fixtures/invalid/session-catalog-semantic-stable-session-scope-missing.json",
            "fixtures/invalid/session-catalog-semantic-event-first-sequence.json",
            "fixtures/invalid/session-catalog-semantic-event-unknown-action.json",
            "fixtures/invalid/session-catalog-semantic-event-stale-upsert.json",
            "fixtures/invalid/session-catalog-semantic-event-remove-missing.json",
            "fixtures/invalid/session-catalog-semantic-event-remove-revision.json",
            "fixtures/invalid/session-catalog-semantic-input-empty.json",
            "fixtures/invalid/session-catalog-semantic-input-scalar-operation.json",
            "fixtures/invalid/session-catalog-semantic-input-operations-not-array.json",
            "fixtures/invalid/session-catalog-semantic-input-state-wrong-type.json",
            "fixtures/invalid/session-catalog-semantic-input-writer-missing.json",
            "fixtures/invalid/session-catalog-semantic-input-unknown-op.json",
            "fixtures/invalid/session-catalog-semantic-input-catalog-index-mismatch.json",
        }
        profile = _load("fixtures/manifest.json")["external_profiles"][
            "connector_session_catalog_v1"
        ]
        self.assertEqual(set(profile["semantic_valid"]), valid_vectors)
        self.assertEqual(set(profile["semantic_invalid"]), invalid_vectors)

        module_path = ROOT / "tools/session_catalog_conformance.py"
        self.assertTrue(module_path.is_file())
        spec = importlib.util.spec_from_file_location(
            "session_catalog_conformance",
            module_path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for relative_path in sorted(valid_vectors | invalid_vectors):
            with self.subTest(vector=relative_path):
                vector = _load(relative_path)
                self.assertEqual(
                    module.validate_vector(vector),
                    vector.get("expected_errors", ["invalid_vector"]),
                )

    def test_semantic_vector_input_schema_fails_closed_without_exceptions(self) -> None:
        schema_path = "schemas/conformance/session-catalog-semantic-vector-v1.schema.json"
        self.assertTrue((ROOT / schema_path).is_file())
        schema = _load(schema_path)
        self.assertEqual(
            schema["$id"],
            "https://contracts.hermes.local/conformance/session-catalog-semantic-vector-v1.schema.json",
        )
        module_path = ROOT / "tools/session_catalog_conformance.py"
        spec = importlib.util.spec_from_file_location("catalog_input_gate", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = (
            "session-catalog-semantic-input-empty.json",
            "session-catalog-semantic-input-scalar-operation.json",
            "session-catalog-semantic-input-operations-not-array.json",
            "session-catalog-semantic-input-state-wrong-type.json",
            "session-catalog-semantic-input-writer-missing.json",
            "session-catalog-semantic-input-unknown-op.json",
        )
        for name in cases:
            with self.subTest(fixture=name):
                self.assertEqual(
                    module.validate_vector(_load(f"fixtures/invalid/{name}")),
                    ["invalid_vector"],
                )

    def test_local_rpc_covers_every_exact_response_branch(self) -> None:
        schema = _load("schemas/local/session-catalog-rpc-v1.schema.json")
        validator = _validator("schemas/local/session-catalog-rpc-v1.schema.json")
        branch_fixtures = {
            "subscribeRequest": "session-catalog-local-subscribe.json",
            "pageRequest": "session-catalog-local-page.json",
            "unsubscribeRequest": "session-catalog-local-unsubscribe.json",
            "pageResponse": "session-catalog-local-subscribe-result.json",
            "unsubscribeResponse": "session-catalog-local-unsubscribe-result.json",
            "eventNotification": "session-catalog-local-event.json",
            "resetNotification": "session-catalog-local-reset-required.json",
            "errorResponse": "session-catalog-local-error-response.json",
        }
        for branch, name in branch_fixtures.items():
            with self.subTest(branch=branch):
                fixture = _load(f"fixtures/valid/{name}")
                validator.validate(fixture)
                matches = sum(
                    not list(
                        Draft202012Validator(
                            {
                                "$schema": "https://json-schema.org/draft/2020-12/schema",
                                "$ref": f"#/$defs/{candidate}",
                                "$defs": schema["$defs"],
                            },
                            format_checker=FormatChecker(),
                            registry=_registry(),
                        ).iter_errors(fixture)
                    )
                    for candidate in branch_fixtures
                )
                self.assertEqual(matches, 1)

        invalid = (
            "session-catalog-local-unsubscribe-closed-false.json",
            "session-catalog-local-error-code.json",
            "session-catalog-local-error-message.json",
            "session-catalog-local-error-reason.json",
            "session-catalog-local-error-extra-field.json",
        )
        for name in invalid:
            with self.subTest(invalid=name):
                self.assertTrue(
                    list(validator.iter_errors(_load(f"fixtures/invalid/{name}")))
                )

        profile = _load("fixtures/manifest.json")["external_profiles"][
            "connector_session_catalog_v1"
        ]
        self.assertTrue(
            {f"fixtures/valid/{name}" for name in branch_fixtures.values()}.issubset(
                set(profile["valid"])
            )
        )
        self.assertTrue(
            {f"fixtures/invalid/{name}" for name in invalid}.issubset(
                set(profile["invalid"])
            )
        )

    def test_profile_grammar_is_identical_across_catalog_boundaries(self) -> None:
        expected = {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^(?![\\s\\S]*[^A-Za-z0-9_.-])[A-Za-z0-9_.-]+$",
        }
        schema_profiles = (
            _load("schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json")["properties"]["profile"],
            _load("schemas/cloud/payloads/session-catalog-event-v1.schema.json")["properties"]["profile"],
            _load("schemas/cloud/payloads/session-catalog-ack-v1.schema.json")["properties"]["profile"],
            _load("schemas/cloud/payloads/session-catalog-nack-v1.schema.json")["properties"]["profile"],
            _load("schemas/local/session-catalog-rpc-v1.schema.json")["$defs"]["page"]["properties"]["profile"],
            _load("schemas/local/session-catalog-rpc-v1.schema.json")["$defs"]["subscribeRequest"]["properties"]["params"]["properties"]["profile"],
            _load("schemas/local/session-catalog-rpc-v1.schema.json")["$defs"]["eventNotification"]["properties"]["params"]["properties"]["profile"],
            _load("schemas/public/session-event-v2.schema.json")["properties"]["profile"],
        )
        for profile_schema in schema_profiles:
            self.assertEqual(profile_schema, expected)

        api = _load("openapi/cloud-api-v1.json")
        projection = api["components"]["schemas"]["SessionProjection"]
        self.assertEqual(projection["properties"]["profile"], {
            "type": ["string", "null"],
            "maxLength": 128,
            "pattern": "^(?![\\s\\S]*[^A-Za-z0-9_.-])[A-Za-z0-9_.-]+$",
        })
        self.assertEqual(
            projection["allOf"][0]["then"]["properties"]["profile"], expected
        )
        for route in (
            "/api/v1/agents/{agent_id}/sessions",
            "/api/sessions",
            "/api/sessions/{session_id}",
            "/api/sessions/{session_id}/messages",
        ):
            profile_parameter = next(
                item for item in api["paths"][route]["get"]["parameters"]
                if item.get("name") == "profile"
            )
            self.assertEqual(profile_parameter["schema"], expected)

        fixture_cases = (
            ("schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json", "session-catalog-snapshot-invalid-profile.json"),
            ("schemas/cloud/payloads/session-catalog-event-v1.schema.json", "session-catalog-event-invalid-profile.json"),
            ("schemas/cloud/payloads/session-catalog-ack-v1.schema.json", "session-catalog-ack-invalid-profile.json"),
            ("schemas/cloud/payloads/session-catalog-nack-v1.schema.json", "session-catalog-nack-invalid-profile.json"),
            ("schemas/local/session-catalog-rpc-v1.schema.json", "session-catalog-local-invalid-profile.json"),
        )
        for schema_name, fixture_name in fixture_cases:
            self.assertTrue(list(_validator(schema_name).iter_errors(
                _load(f"fixtures/invalid/{fixture_name}")
            )))
        self.assertTrue(list(_openapi_validator(api, "SessionPage").iter_errors(
            _load("fixtures/invalid/cloud-api-session-invalid-profile.json")
        )))
        realtime = _load("cloud-realtime-v2.json")
        self.assertTrue(list(Draft202012Validator(
            realtime["schemas"]["session_event"], registry=_registry(),
            format_checker=FormatChecker(),
        ).iter_errors(_load("fixtures/invalid/cloud-realtime-v2-event-invalid-profile.json"))))

        dynamic_cases = (
            (
                _validator("schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"),
                _load("fixtures/valid/session-catalog-snapshot-page.json"),
                ("profile",),
            ),
            (
                _validator("schemas/cloud/payloads/session-catalog-event-v1.schema.json"),
                _load("fixtures/valid/session-catalog-event-upsert.json"),
                ("profile",),
            ),
            (
                _validator("schemas/cloud/payloads/session-catalog-ack-v1.schema.json"),
                _load("fixtures/valid/session-catalog-ack-event.json"),
                ("profile",),
            ),
            (
                _validator("schemas/cloud/payloads/session-catalog-nack-v1.schema.json"),
                _load("fixtures/valid/session-catalog-nack-event-gap.json"),
                ("profile",),
            ),
            (
                _validator("schemas/local/session-catalog-rpc-v1.schema.json"),
                _load("fixtures/valid/session-catalog-local-subscribe.json"),
                ("params", "profile"),
            ),
            (
                Draft202012Validator(
                    realtime["schemas"]["session_event"],
                    registry=_registry(),
                    format_checker=FormatChecker(),
                ),
                _load("fixtures/valid/cloud-realtime-v2-event.json"),
                ("params", "profile"),
            ),
            (
                _openapi_validator(api, "SessionPage"),
                _load("fixtures/valid/cloud-api-session-catalog-only-page.json"),
                ("sessions", 0, "profile"),
            ),
        )
        values = (
            ("a", True),
            ("a" * 128, True),
            ("", False),
            ("a" * 129, False),
            ("bad/profile", False),
            ("a\n", False),
        )
        for case_index, (validator, base, path) in enumerate(dynamic_cases):
            for value, accepted in values:
                with self.subTest(case=case_index, value=repr(value)):
                    instance = copy.deepcopy(base)
                    target = instance
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = value
                    self.assertEqual(not list(validator.iter_errors(instance)), accepted)

    def test_reset_reasons_include_page_revision_change(self) -> None:
        local = _load("schemas/local/session-catalog-rpc-v1.schema.json")
        expected = {
            "cursor_stale",
            "event_gap",
            "buffer_overflow",
            "page_revision_changed",
            "runtime_generation_changed",
            "transport_replaced",
        }
        self.assertEqual(
            set(
                local["$defs"]["resetNotification"]["properties"]["params"][
                    "properties"
                ]["reason"]["enum"]
            ),
            expected,
        )
        self.assertEqual(
            set(
                local["$defs"]["errorResponse"]["properties"]["error"][
                    "properties"
                ]["reason"]["enum"]
            ),
            expected,
        )

    def test_snapshot_ack_binds_terminal_page_position(self) -> None:
        validator = _validator(
            "schemas/cloud/payloads/session-catalog-ack-v1.schema.json"
        )
        ack = _load("fixtures/valid/session-catalog-ack-snapshot.json")
        validator.validate(ack)
        self.assertEqual(ack["snapshot_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(ack["page_index"], 0)
        self.assertIs(ack["is_last"], True)
        for field in ("page_index", "is_last"):
            with self.subTest(missing=field):
                mutated = dict(ack)
                mutated.pop(field)
                self.assertTrue(list(validator.iter_errors(mutated)))
        nonterminal = dict(ack)
        nonterminal["is_last"] = False
        self.assertTrue(list(validator.iter_errors(nonterminal)))

    def test_each_nack_reason_has_one_exact_position_tuple(self) -> None:
        validator = _validator(
            "schemas/cloud/payloads/session-catalog-nack-v1.schema.json"
        )
        valid = {
            "page_gap": "session-catalog-nack-page-gap.json",
            "event_gap": "session-catalog-nack-event-gap.json",
            "runtime_mismatch": "session-catalog-nack-runtime-mismatch.json",
            "stale_writer": "session-catalog-nack-stale-writer.json",
            "contract_mismatch": "session-catalog-nack-contract-mismatch.json",
            "revision_conflict": "session-catalog-nack-revision-conflict.json",
        }
        invalid = {
            reason: f"session-catalog-nack-{reason.replace('_', '-')}-conflicting.json"
            for reason in valid
        }
        manifest = _load("fixtures/manifest.json")
        registered = {
            item["fixture"]
            for section in ("valid", "invalid")
            for item in manifest[section]
            if item["schema"].endswith("session-catalog-nack-v1.schema.json")
        }
        for reason, filename in valid.items():
            relative_path = f"fixtures/valid/{filename}"
            with self.subTest(reason=reason, valid=True):
                fixture = _load(relative_path)
                self.assertEqual(fixture["reason"], reason)
                validator.validate(fixture)
                self.assertIn(relative_path, registered)
        for reason, filename in invalid.items():
            relative_path = f"fixtures/invalid/{filename}"
            with self.subTest(reason=reason, valid=False):
                fixture = _load(relative_path)
                self.assertEqual(fixture["reason"], reason)
                self.assertTrue(list(validator.iter_errors(fixture)))
                self.assertIn(relative_path, registered)

    def test_catalog_path_integer_fields_are_javascript_safe(self) -> None:
        envelope = _load("schemas/cloud/connector-envelope-v1.schema.json")
        self.assertEqual(
            envelope["properties"]["sequence"]["maximum"],
            JS_SAFE_INTEGER_MAXIMUM,
        )
        api = _load("openapi/cloud-api-v1.json")
        projection = api["components"]["schemas"]["SessionProjection"]["properties"]
        for field in (
            "message_count",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
        ):
            self.assertEqual(projection[field]["maximum"], JS_SAFE_INTEGER_MAXIMUM)
        page = api["components"]["schemas"]["SessionPage"]["properties"]
        for field in ("total", "offset"):
            self.assertEqual(page[field]["maximum"], JS_SAFE_INTEGER_MAXIMUM)

        envelope_invalid = "fixtures/invalid/cloud-envelope-sequence-over-js-safe.json"
        page_invalid = "fixtures/invalid/cloud-api-session-page-count-over-js-safe.json"
        manifest = _load("fixtures/manifest.json")
        envelope_registered = {item["fixture"] for item in manifest["invalid"]}
        page_registered = set(
            manifest["external_profiles"]["cloud_api_v1"]["invalid"]
        )
        self.assertTrue((ROOT / envelope_invalid).is_file())
        self.assertIn(envelope_invalid, envelope_registered)
        self.assertTrue((ROOT / page_invalid).is_file())
        self.assertIn(page_invalid, page_registered)

    def test_all_catalog_entries_reference_one_canonical_schema(self) -> None:
        reference = (
            "https://contracts.hermes.local/session-catalog-entry-v1.schema.json"
        )
        entry = _load("schemas/session-catalog-entry-v1.schema.json")
        self.assertEqual(entry["$id"], reference)
        self.assertNotIn("profile", entry["properties"])
        self.assertNotIn("runtime_generation", entry["properties"])

        local = _load("schemas/local/session-catalog-rpc-v1.schema.json")
        snapshot = _load(
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        )
        event = _load("schemas/cloud/payloads/session-catalog-event-v1.schema.json")
        self.assertEqual(
            local["$defs"]["page"]["properties"]["sessions"]["items"],
            {"$ref": reference},
        )
        self.assertEqual(snapshot["properties"]["sessions"]["items"], {"$ref": reference})
        self.assertEqual(event["properties"]["entry"], {"$ref": reference})
        self.assertNotIn("entry", local["$defs"])
        self.assertNotIn("entry", snapshot.get("$defs", {}))
        self.assertNotIn("entry", event.get("$defs", {}))

        _validator("schemas/local/session-catalog-rpc-v1.schema.json").validate(
            _load("fixtures/valid/session-catalog-local-subscribe-result.json")
        )
        _validator(
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        ).validate(_load("fixtures/valid/session-catalog-snapshot-page.json"))
        _validator(
            "schemas/cloud/payloads/session-catalog-event-v1.schema.json"
        ).validate(_load("fixtures/valid/session-catalog-event-upsert.json"))

    def test_generated_catalog_contract_ownership_is_layered(self) -> None:
        plugin = (
            REPOSITORY_ROOT
            / "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated"
        )
        connector = (
            REPOSITORY_ROOT
            / "hermes-connector/src/hermes_connector/contracts/generated"
        )
        cloud = REPOSITORY_ROOT / "hermes-cloud/src/hermes_cloud/contracts/generated"
        local = Path("schemas/local/session-catalog-rpc-v1.schema.json")
        entry = Path("schemas/session-catalog-entry-v1.schema.json")
        snapshot = Path(
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        )
        event = Path("schemas/cloud/payloads/session-catalog-event-v1.schema.json")
        ack = Path("schemas/cloud/payloads/session-catalog-ack-v1.schema.json")
        nack = Path("schemas/cloud/payloads/session-catalog-nack-v1.schema.json")

        self.assertTrue((plugin / local).is_file())
        self.assertTrue((plugin / entry).is_file())
        self.assertFalse((plugin / snapshot).exists())
        self.assertFalse((plugin / event).exists())
        for path in (local, entry, snapshot, event, ack, nack):
            self.assertTrue((connector / path).is_file(), str(path))
        for path in (entry, snapshot, event, ack, nack):
            self.assertTrue((cloud / path).is_file(), str(path))

    def test_event_entries_inherit_outer_scope(self) -> None:
        policy = _load("session-catalog-v1.json")
        self.assertEqual(
            policy["event_rules"]["entry_scope"],
            "entry_inherits_outer_profile_and_runtime_generation_no_duplicate_fields",
        )


if __name__ == "__main__":
    unittest.main()
