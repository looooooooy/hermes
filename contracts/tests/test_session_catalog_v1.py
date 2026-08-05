from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]


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


def _openapi_component_validator(
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
    )


class SessionCatalogV1ContractTests(unittest.TestCase):
    def test_cloud_catalog_messages_have_distinct_frozen_authorities(self) -> None:
        catalog = _load("message-types-v1.json")
        by_name = {item["name"]: item for item in catalog["message_types"]}

        self.assertEqual(
            by_name["session.catalog.snapshot.page"],
            {
                "name": "session.catalog.snapshot.page",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "required_capability": "session.catalog.v1",
                "payload_schema": (
                    "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
                ),
                "effect": "authoritative_session_directory_stage_or_replace",
            },
        )
        self.assertEqual(
            by_name["session.catalog.event"],
            {
                "name": "session.catalog.event",
                "direction": "connector_to_cloud",
                "status": "frozen",
                "required_capability": "session.catalog.v1",
                "payload_schema": (
                    "schemas/cloud/payloads/session-catalog-event-v1.schema.json"
                ),
                "effect": "authoritative_session_directory_apply",
            },
        )
        self.assertEqual(
            by_name["session.catalog.ack"],
            {
                "name": "session.catalog.ack",
                "direction": "cloud_to_connector",
                "status": "frozen",
                "required_capability": "session.catalog.v1",
                "payload_schema": (
                    "schemas/cloud/payloads/session-catalog-ack-v1.schema.json"
                ),
                "effect": "catalog_business_commit_acknowledge",
            },
        )
        self.assertEqual(
            by_name["session.catalog.nack"],
            {
                "name": "session.catalog.nack",
                "direction": "cloud_to_connector",
                "status": "frozen",
                "required_capability": "session.catalog.v1",
                "payload_schema": (
                    "schemas/cloud/payloads/session-catalog-nack-v1.schema.json"
                ),
                "effect": "catalog_full_snapshot_recovery_request",
            },
        )
        for name in (
            "session.catalog.snapshot.page",
            "session.catalog.event",
            "session.catalog.ack",
            "session.catalog.nack",
        ):
            self.assertEqual(
                by_name[name]["required_capability"],
                "session.catalog.v1",
            )

    def test_snapshot_page_and_event_fixtures_match_exact_schemas(self) -> None:
        cases = (
            (
                "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json",
                "fixtures/valid/session-catalog-snapshot-page.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/valid/session-catalog-event-upsert.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/valid/session-catalog-event-remove.json",
            ),
        )
        for schema_path, fixture_path in cases:
            with self.subTest(fixture=fixture_path):
                validator = _validator(schema_path)
                self.assertEqual(
                    list(validator.iter_errors(_load(fixture_path))),
                    [],
                )

    def test_local_rpc_frames_and_cloud_business_responses_are_exact(self) -> None:
        cases = (
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-subscribe.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-subscribe-result.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-page.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-page-result.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-unsubscribe.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-unsubscribe-result.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-event.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-reset-required.json",
            ),
            (
                "schemas/local/session-catalog-rpc-v1.schema.json",
                "fixtures/valid/session-catalog-local-error-response.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-ack-v1.schema.json",
                "fixtures/valid/session-catalog-ack-snapshot.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-ack-v1.schema.json",
                "fixtures/valid/session-catalog-ack-event.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-nack-v1.schema.json",
                "fixtures/valid/session-catalog-nack-event-gap.json",
            ),
            (
                "schemas/session-catalog-degradation-v1.schema.json",
                "fixtures/compatibility/session-catalog-n1-unavailable.json",
            ),
            (
                "schemas/session-catalog-degradation-v1.schema.json",
                "fixtures/degradation/session-catalog-capability-unavailable.json",
            ),
        )
        for schema_path, fixture_path in cases:
            with self.subTest(fixture=fixture_path):
                validator = _validator(schema_path)
                self.assertEqual(
                    list(validator.iter_errors(_load(fixture_path))),
                    [],
                )

    def test_catalog_payloads_reject_identity_assertion_and_cursor_leaks(self) -> None:
        snapshot_validator = _validator(
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        )
        event_validator = _validator(
            "schemas/cloud/payloads/session-catalog-event-v1.schema.json"
        )

        self.assertTrue(
            list(
                snapshot_validator.iter_errors(
                    _load("fixtures/invalid/session-catalog-snapshot-cursor-leak.json")
                )
            )
        )
        self.assertTrue(
            list(
                event_validator.iter_errors(
                    _load("fixtures/invalid/session-catalog-event-agent-id-leak.json")
                )
            )
        )
        self.assertTrue(
            list(
                event_validator.iter_errors(
                    _load("fixtures/invalid/session-catalog-remove-missing-entry.json")
                )
            )
        )

    def test_entry_maps_the_host_dto_without_platform_or_secret_fields(self) -> None:
        entry = _load("schemas/session-catalog-entry-v1.schema.json")
        self.assertEqual(
            set(entry["required"]),
            {
                "session_key",
                "surface",
                "authority_revision",
                "available_actions",
            },
        )
        self.assertFalse(entry["additionalProperties"])
        self.assertEqual(
            set(entry["properties"]["available_actions"]["items"]["enum"]),
            {
                "approval.respond",
                "clarify.respond",
                "prompt.submit",
                "session.interrupt",
                "session.steer",
            },
        )
        lowered = json.dumps(entry, sort_keys=True).lower()
        for forbidden in (
            "agent_id",
            "tenant_id",
            "android",
            "ios",
            "windows",
            "macos",
            "linux",
            "token",
            "secret",
        ):
            self.assertNotIn(f'"{forbidden}"', lowered)

    def test_catalog_integers_are_javascript_safe_and_nonterminal_pages_nonempty(
        self,
    ) -> None:
        snapshot = _load(
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        )
        event = _load(
            "schemas/cloud/payloads/session-catalog-event-v1.schema.json"
        )
        maximum = 9_007_199_254_740_991
        for field in ("catalog_revision", "page_index"):
            self.assertEqual(snapshot["properties"][field]["maximum"], maximum)
        self.assertEqual(
            _load("schemas/session-catalog-entry-v1.schema.json")["properties"]
            ["authority_revision"]["maximum"],
            maximum,
        )
        self.assertEqual(event["properties"]["catalog_sequence"]["maximum"], maximum)
        nonterminal_empty = _load("fixtures/valid/session-catalog-snapshot-page.json")
        nonterminal_empty["is_last"] = False
        nonterminal_empty["sessions"] = []
        self.assertTrue(
            list(
                _validator(
                    "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
                ).iter_errors(nonterminal_empty)
            )
        )
        local_nonterminal_empty = _load(
            "fixtures/valid/session-catalog-local-subscribe-result.json"
        )
        local_nonterminal_empty["result"]["sessions"] = []
        self.assertTrue(
            list(
                _validator(
                    "schemas/local/session-catalog-rpc-v1.schema.json"
                ).iter_errors(local_nonterminal_empty)
            )
        )

    def test_local_rpc_and_cloud_recovery_rules_are_frozen(self) -> None:
        policy = _load("session-catalog-v1.json")
        self.assertEqual(policy["capability"], "session.catalog.v1")
        self.assertEqual(
            policy["local_rpc_methods"],
            {
                "subscribe": "session.catalog.subscribe",
                "page": "session.catalog.page",
                "unsubscribe": "session.catalog.unsubscribe",
                "event": "session.catalog.event",
                "reset_required": "session.catalog.reset_required",
            },
        )
        self.assertEqual(policy["page_size_maximum"], 128)
        self.assertEqual(policy["host_cursor_scope"], "local_only_never_cloud")
        self.assertEqual(
            policy["local_transport"],
            "persistent_observer_role_uds_not_local_gateway_handshake",
        )
        self.assertEqual(
            policy["state_machine"],
            [
                "disconnected",
                "subscribing",
                "staging_pages",
                "snapshot_committed",
                "live",
            ],
        )
        self.assertEqual(policy["event_buffer_maximum"], 1024)
        self.assertEqual(
            policy["event_order"],
            "first_is_snapshot_catalog_revision_plus_one_then_contiguous",
        )
        self.assertEqual(
            policy["remove_representation"],
            "action_remove_with_complete_host_entry",
        )
        self.assertEqual(
            policy["event_rules"],
            {
                "catalog_sequence": "exact_host_session_catalog_event_sequence",
                "entry_scope": (
                    "entry_inherits_outer_profile_and_runtime_generation_no_duplicate_fields"
                ),
                "upsert": (
                    "replace_same_session_key_only_at_equal_or_newer_"
                    "authority_revision"
                ),
                "remove": (
                    "delete_same_session_key_only_when_stored_authority_revision_"
                    "exactly_matches_entry"
                ),
                "without_committed_snapshot": (
                    "reject_and_require_new_full_snapshot"
                ),
            },
        )
        self.assertEqual(
            policy["recovery"],
            {
                "local_disconnect": "new_subscription_and_full_snapshot",
                "runtime_generation_change": "new_snapshot_id_and_full_snapshot",
                "cloud_reconnect": "resume_durable_pages_or_new_full_snapshot",
                "event_gap": "discard_staging_and_require_new_full_snapshot",
                "page_gap": "discard_staging_and_require_new_full_snapshot",
            },
        )
        self.assertEqual(
            policy["snapshot_rules"]["replacement_scope"],
            "authenticated_agent_plus_profile_retiring_all_older_generations",
        )
        self.assertEqual(
            policy["writer_fencing"],
            "one_current_pairing_bound_connector_writer_per_agent_with_monotonic_fence",
        )
        self.assertEqual(
            policy["pairing_boundary"],
            "pairing_authorizes_cloud_identity_but_never_proves_local_runtime_binding",
        )
        self.assertEqual(
            policy["acknowledgement"],
            "cloud_orm_commit_then_exact_business_ack_before_outbox_cleanup",
        )
        self.assertEqual(
            policy["public_projection_identity"],
            {
                "stable_session_id": (
                    "cloud_generated_rfc4122_uuid_persisted_by_orm_for_"
                    "authenticated_agent_profile_host_session_key"
                ),
                "host_session_key": "lineage_root_id_exact_host_session_key",
                "control_resolution": (
                    "stable_session_id_to_authenticated_agent_profile_host_"
                    "session_key_before_ticket_or_command"
                ),
            },
        )

    def test_catalog_capability_and_n_1_degradation_are_explicit(self) -> None:
        capability = _load("schemas/capability-manifest-v1.schema.json")
        self.assertIn(
            "session.catalog.v1",
            capability["properties"]["capabilities"]["items"]["enum"],
        )
        profile = _load("fixtures/manifest.json")["external_profiles"][
            "connector_session_catalog_v1"
        ]
        self.assertEqual(
            profile["n_1"],
            ["fixtures/compatibility/session-catalog-n1-unavailable.json"],
        )
        self.assertEqual(
            profile["capability_degradation"],
            ["fixtures/degradation/session-catalog-capability-unavailable.json"],
        )
        validator = Draft202012Validator(
            _load("schemas/session-catalog-degradation-v1.schema.json")
        )
        for fixture in (*profile["n_1"], *profile["capability_degradation"]):
            self.assertEqual(list(validator.iter_errors(_load(fixture))), [])
        degraded = _load(profile["capability_degradation"][0])
        degraded["available_capabilities"].append("session.catalog.v1")
        self.assertTrue(list(validator.iter_errors(degraded)))
        unknown = _load(profile["capability_degradation"][0])
        unknown["available_capabilities"] = ["unknown.capability"]
        self.assertTrue(list(validator.iter_errors(unknown)))
        extra = _load(profile["n_1"][0])
        extra["synthetic_sessions"] = []
        self.assertTrue(list(validator.iter_errors(extra)))

    def test_public_session_directory_can_represent_catalog_only_sessions(self) -> None:
        api = _load("openapi/cloud-api-v1.json")
        for route in ("/api/v1/agents/{agent_id}/sessions", "/api/sessions"):
            with self.subTest(route=route):
                operation = api["paths"][route]["get"]
                min_messages = next(
                    item
                    for item in operation["parameters"]
                    if item.get("name") == "min_messages"
                )
                self.assertEqual(
                    min_messages["schema"],
                    {"enum": [0, 1], "default": 0},
                )
        projection = api["components"]["schemas"]["SessionProjection"]
        required = set(projection["required"])
        self.assertTrue(
            {
                "directory_source",
                "availability",
                "runtime_generation",
                "surface",
                "authority_revision",
                "available_actions",
                "transcript_available",
            }.issubset(required)
        )
        self.assertEqual(
            projection["properties"]["directory_source"]["enum"],
            ["host_catalog", "transcript_projection"],
        )
        self.assertEqual(
            projection["properties"]["availability"]["enum"],
            ["live", "offline"],
        )
        self.assertIn("null", projection["properties"]["started_at"]["type"])
        self.assertIn("null", projection["properties"]["title"]["type"])
        self.assertIn("null", projection["properties"]["last_active"]["type"])
        self.assertEqual(projection["properties"]["message_count"]["minimum"], 0)
        self.assertEqual(
            projection["properties"]["authority_revision"]["maximum"],
            9_007_199_254_740_991,
        )
        self.assertEqual(
            set(
                projection["properties"]["available_actions"]["items"]["enum"]
            ),
            {
                "approval.respond",
                "clarify.respond",
                "prompt.submit",
                "session.interrupt",
                "session.steer",
            },
        )
        self.assertEqual(projection["properties"]["id"]["format"], "uuid")
        self.assertEqual(
            projection["properties"]["id"]["pattern"],
            (
                "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            ),
        )
        self.assertEqual(
            api["components"]["schemas"]["SessionPage"]["properties"]["sessions"][
                "items"
            ],
            {"$ref": "#/components/schemas/SessionProjection"},
        )

    def test_cloud_api_fixtures_distinguish_catalog_from_transcript_facts(
        self,
    ) -> None:
        api = _load("openapi/cloud-api-v1.json")
        projection_validator = _openapi_component_validator(
            api,
            "SessionProjection",
        )
        page_validator = _openapi_component_validator(api, "SessionPage")
        transcript_projection = _load(
            "fixtures/valid/cloud-api-session-projection.json"
        )
        transcript_page = _load("fixtures/valid/cloud-api-session-page.json")
        catalog_only_page = _load(
            "fixtures/valid/cloud-api-session-catalog-only-page.json"
        )

        self.assertEqual(
            list(projection_validator.iter_errors(transcript_projection)),
            [],
        )
        for page in (transcript_page, catalog_only_page):
            self.assertEqual(list(page_validator.iter_errors(page)), [])

        catalog_only = catalog_only_page["sessions"][0]
        self.assertEqual(catalog_only["directory_source"], "host_catalog")
        self.assertEqual(catalog_only["availability"], "live")
        self.assertIsNone(catalog_only["title"])
        self.assertIsNone(catalog_only["started_at"])
        self.assertIsNone(catalog_only["last_active"])
        self.assertEqual(catalog_only["message_count"], 0)
        self.assertFalse(catalog_only["transcript_available"])
        self.assertTrue(catalog_only["runtime_generation"])
        self.assertTrue(catalog_only["surface"])
        self.assertGreaterEqual(catalog_only["authority_revision"], 1)
        self.assertTrue(catalog_only["available_actions"])
        self.assertRegex(
            catalog_only["id"],
            re.compile(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            ),
        )
        self.assertEqual(
            catalog_only["_lineage_root_id"],
            "durable-session-real",
        )
        self.assertNotEqual(catalog_only["id"], catalog_only["_lineage_root_id"])
        self.assertTrue(catalog_only["agent_id"])
        self.assertTrue(catalog_only["profile"])

        for required_catalog_identity in (
            "agent_id",
            "profile",
            "_lineage_root_id",
        ):
            with self.subTest(missing=required_catalog_identity):
                mutated_page = _load(
                    "fixtures/valid/cloud-api-session-catalog-only-page.json"
                )
                mutated_page["sessions"][0].pop(required_catalog_identity)
                self.assertTrue(list(page_validator.iter_errors(mutated_page)))

        self.assertEqual(
            transcript_projection["directory_source"],
            "transcript_projection",
        )
        self.assertIsNone(transcript_projection["runtime_generation"])
        self.assertIsNone(transcript_projection["surface"])
        self.assertIsNone(transcript_projection["authority_revision"])
        self.assertEqual(transcript_projection["available_actions"], [])
        self.assertTrue(transcript_projection["transcript_available"])

    def test_catalog_business_ack_variants_bind_one_exact_commit_position(
        self,
    ) -> None:
        schema = _load(
            "schemas/cloud/payloads/session-catalog-ack-v1.schema.json"
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())

        snapshot_ack = _load("fixtures/valid/session-catalog-ack-snapshot.json")
        snapshot_ack["catalog_sequence"] = 8
        self.assertTrue(list(validator.iter_errors(snapshot_ack)))

        event_ack = _load("fixtures/valid/session-catalog-ack-event.json")
        event_ack["snapshot_id"] = "33333333-3333-4333-8333-333333333333"
        self.assertTrue(list(validator.iter_errors(event_ack)))

        for fixture_path in (
            "fixtures/valid/session-catalog-ack-snapshot.json",
            "fixtures/valid/session-catalog-ack-event.json",
        ):
            ack = _load(fixture_path)
            for binding in (
                "acked_message_id",
                "acked_payload_digest",
                "acked_connector_sequence",
            ):
                with self.subTest(fixture=fixture_path, missing=binding):
                    mutated = dict(ack)
                    mutated.pop(binding)
                    self.assertTrue(list(validator.iter_errors(mutated)))

    def test_catalog_cloud_payloads_reject_all_self_asserted_identity_fields(
        self,
    ) -> None:
        cases = (
            (
                "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json",
                "fixtures/valid/session-catalog-snapshot-page.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/valid/session-catalog-event-upsert.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-ack-v1.schema.json",
                "fixtures/valid/session-catalog-ack-event.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-nack-v1.schema.json",
                "fixtures/valid/session-catalog-nack-event-gap.json",
            ),
        )
        for schema_path, fixture_path in cases:
            validator = _validator(schema_path)
            for forbidden in ("agent_id", "tenant_id", "device_id"):
                with self.subTest(schema=schema_path, forbidden=forbidden):
                    instance = _load(fixture_path)
                    instance[forbidden] = "must-not-cross-the-boundary"
                    self.assertTrue(list(validator.iter_errors(instance)))

    def test_manifest_registers_catalog_valid_and_invalid_fixtures(self) -> None:
        manifest = _load("fixtures/manifest.json")
        profile = manifest["external_profiles"]["connector_session_catalog_v1"]
        self.assertEqual(profile["authority"], "session-catalog-v1.json")
        self.assertEqual(
            profile["n_1"],
            ["fixtures/compatibility/session-catalog-n1-unavailable.json"],
        )
        self.assertEqual(
            profile["capability_degradation"],
            ["fixtures/degradation/session-catalog-capability-unavailable.json"],
        )
        registered = {
            (item["schema"], item["fixture"])
            for section in ("valid", "invalid")
            for item in manifest[section]
        }
        expected = {
            (
                "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json",
                "fixtures/valid/session-catalog-snapshot-page.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/valid/session-catalog-event-upsert.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/valid/session-catalog-event-remove.json",
            ),
            *{
                (
                    "schemas/local/session-catalog-rpc-v1.schema.json",
                    f"fixtures/valid/{fixture}",
                )
                for fixture in (
                    "session-catalog-local-subscribe.json",
                    "session-catalog-local-subscribe-result.json",
                    "session-catalog-local-page.json",
                    "session-catalog-local-page-result.json",
                    "session-catalog-local-unsubscribe.json",
                    "session-catalog-local-unsubscribe-result.json",
                    "session-catalog-local-event.json",
                    "session-catalog-local-reset-required.json",
                    "session-catalog-local-error-response.json",
                )
            },
            (
                "schemas/cloud/payloads/session-catalog-ack-v1.schema.json",
                "fixtures/valid/session-catalog-ack-snapshot.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-ack-v1.schema.json",
                "fixtures/valid/session-catalog-ack-event.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-nack-v1.schema.json",
                "fixtures/valid/session-catalog-nack-event-gap.json",
            ),
            (
                "schemas/session-catalog-degradation-v1.schema.json",
                "fixtures/compatibility/session-catalog-n1-unavailable.json",
            ),
            (
                "schemas/session-catalog-degradation-v1.schema.json",
                "fixtures/degradation/session-catalog-capability-unavailable.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json",
                "fixtures/invalid/session-catalog-snapshot-cursor-leak.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/invalid/session-catalog-event-agent-id-leak.json",
            ),
            (
                "schemas/cloud/payloads/session-catalog-event-v1.schema.json",
                "fixtures/invalid/session-catalog-remove-missing-entry.json",
            ),
        }
        self.assertTrue(expected.issubset(registered))
        registered_fixtures = {fixture for _, fixture in registered}
        self.assertTrue(set(profile["valid"]).issubset(registered_fixtures))
        self.assertTrue(set(profile["invalid"]).issubset(registered_fixtures))
        self.assertIn(
            "fixtures/valid/cloud-api-session-catalog-only-page.json",
            manifest["external_profiles"]["cloud_api_v1"]["valid"],
        )


if __name__ == "__main__":
    unittest.main()
