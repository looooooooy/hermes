from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "openapi/cloud-api-v1.json"
REALTIME_PATH = ROOT / "cloud-realtime-v1.json"
EXTERNAL_PROFILE_SCHEMA_PATH = (
    ROOT / "schemas/external-degradation-profile-v1.schema.json"
)
FORBIDDEN_EXTERNAL_FIELD_WORDS = {
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "lease",
    "password",
    "secret",
    "ticket",
    "token",
}
FORBIDDEN_EXTERNAL_FIELD_COMPOUNDS = {
    "privatekey",
    "wsurl",
}
FROZEN_EVENT_TYPES = {
    "message.start",
    "message.delta",
    "message.complete",
    "agent.terminal.output",
    "reasoning.delta",
    "status.update",
    "thinking.delta",
    "tool.output.delta",
}

REST_VALID_FIXTURES = {
    "StatusResponse": "fixtures/valid/cloud-api-status.json",
    "PasswordLoginRequest": "fixtures/valid/cloud-api-password-login.json",
    "PasswordLoginResponse": ("fixtures/valid/cloud-api-password-login-result.json"),
    "LogoutResponse": "fixtures/valid/cloud-api-logout-response.json",
    "RefreshRequest": "fixtures/valid/cloud-api-refresh-request.json",
    "TokenResponse": "fixtures/valid/cloud-api-token.json",
    "SessionProjection": "fixtures/valid/cloud-api-session-projection.json",
    "SessionPage": (
        "fixtures/valid/cloud-api-session-page.json",
        "fixtures/valid/cloud-api-session-catalog-only-page.json",
    ),
    "SessionTranscript": "fixtures/valid/cloud-api-session-transcript.json",
    "WebSocketTicketRequest": (
        "fixtures/valid/cloud-api-observer-ticket-request.json",
        "fixtures/valid/cloud-api-scoped-observer-ticket-request.json",
        "fixtures/valid/cloud-api-control-ticket-request.json",
    ),
    "WebSocketTicketResponse": (
        "fixtures/valid/cloud-api-observer-ticket.json",
        "fixtures/valid/cloud-api-control-ticket.json",
    ),
}

REST_INVALID_FIXTURES = {
    "StatusResponse": (
        "fixtures/invalid/cloud-api-status-inconsistent.json",
        "fixtures/invalid/cloud-api-status-auth-unavailable.json",
        "fixtures/invalid/cloud-api-status-native-pkce.json",
        "fixtures/invalid/cloud-api-status-extra-field.json",
    ),
    "WebSocketTicketRequest": (
        "fixtures/invalid/cloud-api-control-ticket-missing-profile.json",
        "fixtures/invalid/cloud-api-control-ticket-uppercase-uuid.json",
        "fixtures/invalid/cloud-api-control-ticket-nil-uuid.json",
        "fixtures/invalid/cloud-api-control-ticket-v7-uuid.json",
        "fixtures/invalid/cloud-api-control-ticket-invalid-variant-uuid.json",
        "fixtures/invalid/cloud-api-control-ticket-extra-field.json",
    ),
    "WebSocketTicketResponse": (
        "fixtures/invalid/cloud-api-observer-ticket-response-missing-role.json",
    ),
    "SessionPage": (
        "fixtures/invalid/cloud-api-session-page-limit-over-contract.json",
        "fixtures/invalid/cloud-api-session-page-count-over-js-safe.json",
        "fixtures/invalid/cloud-api-session-catalog-only-fabricated-transcript.json",
        "fixtures/invalid/cloud-api-session-invalid-profile.json",
    ),
    "SessionTranscript": (
        "fixtures/invalid/cloud-api-session-transcript-depth-over-contract.json",
    ),
}

REALTIME_VALID_FIXTURES = {
    "gateway_ready": (
        "fixtures/valid/cloud-realtime-ready.json",
        "fixtures/valid/cloud-realtime-control-ready.json",
    ),
    "control_request": "fixtures/valid/cloud-realtime-control-method.json",
    "control_rpc_error": (
        "fixtures/valid/cloud-realtime-control-live-runtime-unavailable.json",
        "fixtures/valid/cloud-realtime-control-method-not-allowed.json",
    ),
    "observe_subscribe_request": ("fixtures/valid/cloud-realtime-subscribe.json"),
    "observe_subscribe_result": (
        "fixtures/valid/cloud-realtime-subscribe-result.json",
        "fixtures/valid/cloud-realtime-coalesced-replay.json",
    ),
    "observe_unsubscribe_request": ("fixtures/valid/cloud-realtime-unsubscribe.json"),
    "observe_unsubscribe_result": (
        "fixtures/valid/cloud-realtime-unsubscribe-result.json"
    ),
    "json_rpc_error": "fixtures/valid/cloud-realtime-rpc-error.json",
    "session_event": (
        "fixtures/valid/cloud-realtime-event.json",
        "fixtures/valid/cloud-realtime-status-running.json",
        "fixtures/valid/cloud-realtime-status-idle.json",
    ),
}

REALTIME_INVALID_FIXTURES = {
    "gateway_ready": (
        "fixtures/invalid/cloud-realtime-ready-n1.json",
        "fixtures/invalid/cloud-realtime-ready-capability-missing.json",
        "fixtures/invalid/cloud-realtime-control-ready-unadvertised-error.json",
    ),
    "control_request": (
        "fixtures/invalid/cloud-realtime-control-method-extra-field.json",
    ),
    "control_rpc_error": (
        "fixtures/invalid/cloud-realtime-control-error-unadvertised-code.json",
    ),
    "observe_subscribe_result": (
        "fixtures/invalid/cloud-realtime-subscribe-snapshot-after-head.json",
        "fixtures/invalid/cloud-realtime-subscribe-empty-replay-gap.json",
        "fixtures/invalid/cloud-realtime-subscribe-running-status-mismatch.json",
        "fixtures/invalid/cloud-realtime-subscribe-running-idle-mismatch.json",
        "fixtures/invalid/cloud-realtime-replay-session-mismatch.json",
        "fixtures/invalid/cloud-realtime-replay-missing-session-id.json",
        "fixtures/invalid/cloud-realtime-replay-gap.json",
        "fixtures/invalid/cloud-realtime-replay-nonmergeable-range.json",
        "fixtures/invalid/cloud-realtime-replay-reversed-range.json",
    ),
    "json_rpc_error": ("fixtures/invalid/cloud-realtime-rpc-error-unknown-code.json",),
    "session_event": (
        "fixtures/invalid/cloud-realtime-event-internal-leak.json",
        "fixtures/invalid/cloud-realtime-event-local-type.json",
        "fixtures/invalid/cloud-realtime-event-payload-type.json",
        "fixtures/invalid/cloud-realtime-event-extra-field.json",
        "fixtures/invalid/cloud-realtime-event-type-too-long.json",
        "fixtures/invalid/cloud-realtime-event-missing-session-id.json",
        "fixtures/invalid/cloud-realtime-status-running-false.json",
        "fixtures/invalid/cloud-realtime-status-idle-true.json",
        "fixtures/invalid/cloud-realtime-status-missing-running.json",
    ),
}

REST_N_1_FIXTURES = {
    "StatusResponse": "fixtures/compatibility/cloud-api-status-n1.json",
    "WebSocketTicketRequest": (
        "fixtures/compatibility/cloud-api-observer-ticket-request-n1.json"
    ),
    "WebSocketTicketResponse": (
        "fixtures/compatibility/cloud-api-observer-ticket-response-n1.json"
    ),
}
REST_DEGRADATION_FIXTURES = {
    "StatusResponse": "fixtures/degradation/cloud-api-auth-optional-unavailable.json",
}
REALTIME_N_1_FIXTURES = {
    "gateway_ready": "fixtures/compatibility/cloud-realtime-ready-n1.json",
}
REALTIME_DEGRADATION_FIXTURES = {
    "observe_subscribe_result": (
        "fixtures/degradation/cloud-realtime-idle-subscription.json"
    ),
}
REALTIME_N_1_PROFILE_FIXTURE = (
    "fixtures/compatibility/cloud-realtime-observer-n1-profile.json"
)
REALTIME_DEGRADATION_PROFILE_FIXTURE = (
    "fixtures/degradation/cloud-realtime-replay-unavailable-profile.json"
)


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_forbidden_external_field(key: str) -> bool:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    words = {word for word in re.split(r"[^a-zA-Z0-9]+", separated.lower()) if word}
    compact = "".join(words)
    return (
        bool(words & FORBIDDEN_EXTERNAL_FIELD_WORDS)
        or any(term in compact for term in FORBIDDEN_EXTERNAL_FIELD_WORDS)
        or any(term in compact for term in FORBIDDEN_EXTERNAL_FIELD_COMPOUNDS)
    )


def _schema_errors(schema: dict[str, object], instance: object) -> list[str]:
    return [
        error.message
        for error in Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(instance)
    ]


def _openapi_schema_errors(
    openapi: dict[str, object],
    schema_name: str,
    instance: object,
) -> list[str]:
    root_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": f"#/components/schemas/{schema_name}",
        "components": openapi["components"],
    }
    return _schema_errors(root_schema, instance)


def _forbidden_field_errors(
    value: object,
    *,
    path: str = "$",
) -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if not path.endswith(".control_error_codes") and _is_forbidden_external_field(
                key
            ):
                errors.append(f"{path}.{key} is a forbidden external field")
            errors.extend(_forbidden_field_errors(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_forbidden_field_errors(item, path=f"{path}[{index}]"))
    return errors


def _event_payload_errors(
    profile: dict[str, object],
    event: dict[str, object],
) -> list[str]:
    event_type = event.get("type")
    catalog = profile.get("event_types")
    if not isinstance(catalog, dict):
        return ["event_types catalog is missing"]
    entry = catalog.get(event_type)
    if not isinstance(entry, dict):
        return [f"{event_type!r} is not an allowed external event type"]
    payload_schema = entry.get("payload_schema")
    if not isinstance(payload_schema, dict):
        return [f"{event_type!r} payload schema is missing"]
    payload = event.get("payload")
    errors = _schema_errors(payload_schema, payload)
    if event_type == "status.update" and isinstance(payload, dict):
        errors.extend(_running_status_errors(profile, payload))
    return errors


def _running_status_errors(
    profile: dict[str, object],
    value: dict[str, object],
) -> list[str]:
    running_statuses = profile.get("status_semantics", {}).get(
        "running_statuses",
        [],
    )
    running = value.get("running")
    status_is_running = value.get("status") in running_statuses
    if isinstance(running, bool) and running != status_is_running:
        return ["running must be true exactly for running statuses"]
    return []


def _transport_limit_errors(
    profile: dict[str, object],
    frame: object,
) -> list[str]:
    limits = profile.get("limits")
    if not isinstance(limits, dict):
        return ["transport limits are missing"]

    errors: list[str] = []
    encoded = json.dumps(
        frame,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > limits["max_text_frame_bytes"]:
        errors.append("encoded text frame exceeds max_text_frame_bytes")

    def visit(value: object, *, path: str, depth: int) -> None:
        if depth > limits["max_nesting_depth"]:
            errors.append(f"{path} exceeds max_nesting_depth")
            return
        if isinstance(value, str):
            if len(value.encode("utf-8")) > limits["max_string_bytes"]:
                errors.append(f"{path} exceeds max_string_bytes")
            return
        if isinstance(value, dict):
            if len(value) > limits["max_object_fields"]:
                errors.append(f"{path} exceeds max_object_fields")
            for key, item in value.items():
                if len(key.encode("utf-8")) > limits["max_string_bytes"]:
                    errors.append(f"{path} contains a key exceeding max_string_bytes")
                visit(item, path=f"{path}.{key}", depth=depth + 1)
            return
        if isinstance(value, list):
            if len(value) > limits["max_array_items"]:
                errors.append(f"{path} exceeds max_array_items")
            for index, item in enumerate(value):
                visit(item, path=f"{path}[{index}]", depth=depth + 1)

    visit(frame, path="$", depth=1)
    return errors


def _rest_semantic_errors(
    openapi: dict[str, object],
    schema_name: str,
    response: object,
) -> list[str]:
    limits = dict(openapi["x-hermes-response-limits"])
    total_limit_name = (
        "session_transcript_json_bytes"
        if schema_name == "SessionTranscript"
        else "default_json_bytes"
    )
    limits["max_text_frame_bytes"] = limits[total_limit_name]
    return _transport_limit_errors({"limits": limits}, response)


def _realtime_semantic_errors(
    profile: dict[str, object],
    schema_name: str,
    frame: object,
) -> list[str]:
    errors: list[str] = []
    transport = profile.get("transport")
    if (
        not isinstance(transport, dict)
        or transport.get("one_json_document_per_frame") is not True
    ):
        errors.append("transport must require one JSON document per frame")
    if not isinstance(frame, dict):
        return [*errors, "frame must be an object"]
    errors.extend(_transport_limit_errors(profile, frame))
    errors.extend(_forbidden_field_errors(frame))

    if schema_name == "session_event":
        params = frame.get("params")
        if isinstance(params, dict):
            errors.extend(_event_payload_errors(profile, params))
        return errors

    if schema_name != "observe_subscribe_result":
        return errors
    result = frame.get("result")
    if not isinstance(result, dict):
        return errors

    errors.extend(_running_status_errors(profile, result))

    snapshot = result.get("snapshot_event_sequence")
    head = result.get("event_sequence")
    if not isinstance(snapshot, int) or not isinstance(head, int):
        return errors
    if snapshot > head:
        errors.append("snapshot_event_sequence exceeds event_sequence")

    replay = result.get("replay_events")
    if not isinstance(replay, list):
        return errors
    if not replay and snapshot != head:
        errors.append("empty replay requires snapshot and head equality")
        return errors

    mergeable = set(profile.get("sequence", {}).get("mergeable_event_types", []))
    previous = snapshot
    for event in replay:
        if not isinstance(event, dict):
            continue
        if event.get("session_key") != result.get("session_key"):
            errors.append("replay session_key does not match subscription")
        if event.get("session_id") != result.get("runtime_session_id"):
            errors.append("replay session_id does not match runtime session")
        sequence = event.get("event_sequence")
        sequence_start = event.get("event_sequence_start", sequence)
        if not isinstance(sequence, int) or not isinstance(sequence_start, int):
            continue
        if sequence_start != previous + 1:
            errors.append("replay sequence is not contiguous")
        if sequence_start > sequence:
            errors.append("replay range start exceeds event_sequence")
        if sequence_start < sequence and event.get("type") not in mergeable:
            errors.append("only mergeable event types may declare a range")
        if sequence > head:
            errors.append("replay sequence exceeds event_sequence")
        previous = sequence
        errors.extend(_event_payload_errors(profile, event))
    if replay and previous != head:
        errors.append("replay does not end at event_sequence")
    return errors


class CloudApiV1ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = _load(OPENAPI_PATH)
        cls.realtime = _load(REALTIME_PATH)

    def test_openapi_freezes_the_external_client_closure_surface(self) -> None:
        self.assertEqual(self.openapi["openapi"], "3.1.0")
        self.assertEqual(
            set(self.openapi["paths"]),
            {
                "/api/status",
                "/auth/password-login",
                "/auth/native/refresh",
                "/auth/logout",
                "/api/v1/agents",
                "/api/v1/agents/{agent_id}/sessions",
                "/api/agents",
                "/api/sessions",
                "/api/sessions/{session_id}",
                "/api/sessions/{session_id}/messages",
                "/api/auth/ws-ticket",
                "/api/device-pairing/offers",
                "/api/device-pairing/offers/{pairing_offer_id}",
                "/api/device-pairing/claims",
                "/api/device-pairing/sessions/{pairing_session_id}",
                "/api/device-pairing/sessions/{pairing_session_id}/confirm",
                "/api/device-pairing/sessions/{pairing_session_id}/cancel",
                "/api/device-pairing/sessions/{pairing_session_id}/proof",
                "/api/device-auth/challenges",
                "/api/device-auth/tokens",
                "/api/devices/{device_id}/revoke",
            },
        )
        self.assertEqual(
            self.openapi["components"]["securitySchemes"]["bearerAuth"]["scheme"],
            "bearer",
        )

    def test_openapi_preserves_the_deployed_hermes_base_path(self) -> None:
        server = self.openapi["servers"][0]
        self.assertEqual(server["url"], "/{basePath}")
        self.assertEqual(
            server["variables"]["basePath"]["default"],
            "hermes",
        )
        resolved_status = (
            server["url"].replace(
                "{basePath}",
                server["variables"]["basePath"]["default"],
            )
            + "/api/status"
        )
        self.assertEqual(resolved_status, "/hermes/api/status")

    def test_directory_and_session_queries_match_client_compatibility_surface(
        self,
    ) -> None:
        paths = self.openapi["paths"]
        list_parameters = {
            parameter.get("name")
            or parameter["$ref"].rsplit("/", maxsplit=1)[-1]: parameter
            for parameter in paths["/api/v1/agents/{agent_id}/sessions"]["get"][
                "parameters"
            ]
        }
        self.assertEqual(
            set(list_parameters),
            {
                "limit",
                "offset",
                "min_messages",
                "archived",
                "order",
                "profile",
                "AgentId",
            },
        )
        self.assertEqual(list_parameters["limit"]["schema"]["maximum"], 500)
        self.assertEqual(
            list_parameters["min_messages"]["schema"],
            {"enum": [0, 1], "default": 0},
        )
        compatibility_parameters = {
            parameter.get("name")
            or parameter["$ref"].rsplit("/", maxsplit=1)[-1]: parameter
            for parameter in paths["/api/sessions"]["get"]["parameters"]
        }
        self.assertEqual(
            compatibility_parameters["min_messages"]["schema"],
            {"enum": [0, 1], "default": 0},
        )
        self.assertEqual(list_parameters["archived"]["schema"]["const"], "exclude")
        self.assertEqual(list_parameters["order"]["schema"]["const"], "recent")
        self.assertEqual(
            paths["/api/v1/agents/{agent_id}/sessions"]["get"]["responses"][
                "409"
            ],
            {"$ref": "#/components/responses/SessionScopeAmbiguous"},
        )
        self.assertEqual(
            self.openapi["components"]["schemas"][
                "SessionScopeAmbiguousResponse"
            ],
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "reason"],
                "properties": {
                    "code": {
                        "type": "string",
                        "const": "SESSION_SCOPE_AMBIGUOUS",
                    },
                    "reason": {
                        "type": "string",
                        "const": "session scope is ambiguous",
                    },
                },
            },
        )

        detail_parameters = paths["/api/sessions/{session_id}"]["get"]["parameters"]
        self.assertEqual(
            {
                parameter.get("name") or parameter["$ref"].rsplit("/", maxsplit=1)[-1]
                for parameter in detail_parameters
            },
            {"SessionId", "profile", "AgentIdQuery"},
        )

        self.assertEqual(
            paths["/api/v1/agents"]["get"]["parameters"],
            [{"$ref": "#/components/parameters/WorkspaceIdQuery"}],
        )
        self.assertEqual(
            self.openapi["components"]["parameters"]["AgentId"],
            {
                "name": "agent_id",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "format": "uuid",
                    "pattern": (
                        "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                        "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
                    ),
                },
            },
        )

    def test_unversioned_directory_routes_are_exact_deprecated_compatibility_paths(
        self,
    ) -> None:
        paths = self.openapi["paths"]
        canonical_agents = paths["/api/v1/agents"]["get"]
        compatible_agents = paths["/api/agents"]["get"]
        canonical_sessions = paths["/api/v1/agents/{agent_id}/sessions"]["get"]
        compatible_sessions = paths["/api/sessions"]["get"]

        self.assertTrue(compatible_agents["deprecated"])
        self.assertEqual(compatible_agents["operationId"], "listAgentsCompatibility")
        self.assertEqual(compatible_agents["security"], canonical_agents["security"])
        self.assertEqual(
            compatible_agents["x-hermes-cookie-read-policy"],
            canonical_agents["x-hermes-cookie-read-policy"],
        )
        self.assertEqual(compatible_agents["parameters"], canonical_agents["parameters"])
        self.assertEqual(compatible_agents["responses"], canonical_agents["responses"])

        self.assertTrue(compatible_sessions["deprecated"])
        self.assertEqual(
            compatible_sessions["operationId"],
            "listSessionsCompatibility",
        )
        self.assertEqual(compatible_sessions["security"], canonical_sessions["security"])
        self.assertEqual(
            compatible_sessions["x-hermes-cookie-read-policy"],
            canonical_sessions["x-hermes-cookie-read-policy"],
        )
        compatible_parameter_names = {
            parameter.get("name")
            or parameter["$ref"].rsplit("/", maxsplit=1)[-1]
            for parameter in compatible_sessions["parameters"]
        }
        canonical_parameter_names = {
            parameter.get("name")
            or parameter["$ref"].rsplit("/", maxsplit=1)[-1]
            for parameter in canonical_sessions["parameters"]
        }
        self.assertEqual(
            compatible_parameter_names,
            canonical_parameter_names - {"AgentId"} | {"AgentIdQuery"},
        )
        self.assertEqual(compatible_sessions["responses"], canonical_sessions["responses"])

    def test_public_and_authenticated_routes_are_explicit(self) -> None:
        paths = self.openapi["paths"]
        self.assertEqual(paths["/api/status"]["get"]["security"], [])
        self.assertEqual(paths["/auth/password-login"]["post"]["security"], [])
        self.assertEqual(
            paths["/auth/native/refresh"]["post"]["security"],
            [],
        )
        catalog_read_policy = {
            "cookie_name": "hermes_session_at",
            "requires_effective_https": True,
            "same_origin_evidence": {
                "origin_must_match_effective_host_exactly": True,
                "fetch_metadata_alternative": {
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                },
            },
            "forwarded_headers_trusted_only_from_configured_reverse_proxy": True,
            "bearer_and_cookie_must_resolve_to_same_principal": True,
            "invalid_or_conflicting_credentials": "fail_closed",
        }
        for path in (
            "/api/v1/agents",
            "/api/v1/agents/{agent_id}/sessions",
            "/api/agents",
            "/api/sessions",
        ):
            self.assertEqual(
                paths[path]["get"]["security"],
                [{"bearerAuth": []}, {"sessionCookieAuth": []}],
            )
            self.assertEqual(
                paths[path]["get"]["x-hermes-cookie-read-policy"],
                catalog_read_policy,
            )
            self.assertEqual(
                paths[path]["get"]["responses"]["403"],
                {"$ref": "#/components/responses/Forbidden"},
            )

        for path in (
            "/api/sessions/{session_id}",
            "/api/sessions/{session_id}/messages",
        ):
            self.assertEqual(paths[path]["get"]["security"], [{"bearerAuth": []}])

        ticket = paths["/api/auth/ws-ticket"]["post"]
        self.assertEqual(
            ticket["security"],
            [{"bearerAuth": []}, {"sessionCookieAuth": []}],
        )
        cookie_scheme = self.openapi["components"]["securitySchemes"][
            "sessionCookieAuth"
        ]
        self.assertEqual(
            cookie_scheme,
            {
                "type": "apiKey",
                "in": "cookie",
                "name": "hermes_session_at",
                "description": (
                    "Secure HttpOnly SameSite=Strict browser session cookie; "
                    "accepted only by operations that declare sessionCookieAuth "
                    "and their endpoint-specific same-origin policy."
                ),
            },
        )
        csrf = ticket["x-hermes-cookie-csrf"]
        self.assertTrue(csrf["requires_https_origin"])
        self.assertTrue(csrf["origin_must_match_effective_host_exactly"])
        self.assertTrue(
            csrf["forwarded_headers_trusted_only_from_configured_reverse_proxy"]
        )
        self.assertEqual(csrf["invalid_or_conflicting_credentials"], "fail_closed")
        self.assertIn("403", ticket["responses"])

    def test_logout_freezes_empty_body_cookie_and_error_contract(self) -> None:
        logout = self.openapi["paths"]["/auth/logout"]["post"]
        self.assertEqual(logout["security"], [])
        self.assertNotIn("requestBody", logout)
        self.assertTrue(logout["x-hermes-empty-body-required"])
        self.assertEqual(
            logout["x-hermes-same-origin-policy"],
            {
                "requires_https_origin": True,
                "origin_must_match_effective_host_exactly": True,
                "fetch_metadata_not_accepted": True,
                "forwarded_headers_trusted_only_from_configured_reverse_proxy": True,
            },
        )
        self.assertEqual(
            logout["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/LogoutResponse"},
        )
        self.assertEqual(
            logout["responses"]["200"]["x-hermes-cookie-contract"],
            {
                "accepted_names": [
                    "hermes_session_at",
                    "hermes_session_rt",
                    "hermes_session_provider",
                ],
                "expired_names": [
                    "hermes_session_at",
                    "hermes_session_rt",
                    "hermes_session_provider",
                ],
                "path": "/",
                "secure": True,
                "http_only": True,
                "same_site": "strict",
                "max_age": 0,
                "expires_required": True,
                "resolvable_session_revocation_required_before_expiry": True,
                "failure_preserves_session_and_cookies": True,
                "idempotent_when_cookies_absent_or_session_already_revoked": True,
            },
        )
        self.assertEqual(set(logout["responses"]), {"200", "400", "401", "403", "503"})

        schemas = self.openapi["components"]["schemas"]
        expected_errors = {
            "400": ("InvalidLogoutRequestResponse", "INVALID_REQUEST", "empty request body required"),
            "401": ("LogoutAuthenticationFailedResponse", "AUTHENTICATION_FAILED", "authentication failed"),
            "403": ("ForbiddenResponse", "FORBIDDEN", "trusted same-origin request required"),
            "503": ("LogoutFailedResponse", "LOGOUT_FAILED", "logout failed"),
        }
        for status, (schema_name, code, reason) in expected_errors.items():
            with self.subTest(status=status):
                self.assertEqual(
                    logout["responses"][status]["content"]["application/json"]["schema"],
                    {"$ref": f"#/components/schemas/{schema_name}"},
                )
                self.assertEqual(
                    schemas[schema_name],
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["code", "reason"],
                        "properties": {
                            "code": {"const": code},
                            "reason": {"const": reason},
                        },
                    },
                )

    def test_status_auth_flow_vocabulary_matches_client_contract(self) -> None:
        auth_flows = self.openapi["components"]["schemas"]["StatusResponse"][
            "properties"
        ]["auth_flows"]["items"]["enum"]
        self.assertEqual(auth_flows, ["password"])
        self.assertNotIn("native_pkce", OPENAPI_PATH.read_text(encoding="utf-8"))

    def test_ticket_authority_supports_legacy_observer_and_exact_control_scope(
        self,
    ) -> None:
        ticket_request = self.openapi["components"]["schemas"]["WebSocketTicketRequest"]
        self.assertEqual(len(ticket_request["oneOf"]), 4)
        legacy_request, observer_request, observer_v2_request, control_request = (
            ticket_request["oneOf"]
        )
        self.assertEqual(legacy_request["maxProperties"], 0)
        self.assertEqual(
            set(observer_request["required"]),
            {"connection_role", "client_instance_id"},
        )
        self.assertFalse(observer_request["additionalProperties"])
        self.assertEqual(
            observer_request["properties"]["connection_role"]["const"],
            "observer",
        )
        self.assertEqual(
            observer_request["properties"]["agent_id"],
            control_request["properties"]["agent_id"],
        )
        self.assertEqual(
            set(observer_v2_request["required"]),
            {"connection_role", "client_instance_id", "observer_contract"},
        )
        self.assertEqual(
            observer_v2_request["properties"]["observer_contract"]["const"],
            2,
        )
        self.assertEqual(
            observer_v2_request["properties"]["agent_id"],
            control_request["properties"]["agent_id"],
        )
        self.assertEqual(
            set(control_request["required"]),
            {
                "connection_role",
                "client_instance_id",
                "session_id",
                "profile",
            },
        )
        self.assertFalse(control_request["additionalProperties"])
        self.assertEqual(
            control_request["properties"]["connection_role"]["const"],
            "control",
        )
        self.assertEqual(
            control_request["properties"]["client_instance_id"]["pattern"],
            "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        ticket_response = self.openapi["components"]["schemas"][
            "WebSocketTicketResponse"
        ]
        self.assertEqual(
            {
                branch["properties"]["connection_role"]["const"]
                for branch in ticket_response["oneOf"]
            },
            {"observer", "control"},
        )
        observer_v2_responses = [
            branch
            for branch in ticket_response["oneOf"]
            if branch["properties"].get("observer_contract") == {"const": 2}
        ]
        self.assertEqual(len(observer_v2_responses), 1)

    def test_agent_identity_fields_are_required_but_legacy_nullable(self) -> None:
        schemas = self.openapi["components"]["schemas"]
        for schema_name, schema in (
            ("AgentProjection", schemas["AgentProjection"]),
            ("SessionProjection", schemas["SessionProjection"]),
            ("SessionTranscript", schemas["SessionTranscript"]),
        ):
            with self.subTest(schema=schema_name):
                self.assertTrue(
                    {"agent_id", "workspace_id"}.issubset(schema["required"])
                )
                self.assertIn("null", schema["properties"]["agent_id"]["type"])
                self.assertIn("null", schema["properties"]["workspace_id"]["type"])
        self.assertEqual(
            schemas["SessionPage"]["properties"]["sessions"]["items"],
            {"$ref": "#/components/schemas/SessionProjection"},
        )

    def test_v1_subscribe_declares_optional_canonical_agent_scope(self) -> None:
        params = self.realtime["schemas"]["observe_subscribe_request"][
            "properties"
        ]["params"]
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

    def test_password_login_freezes_secure_cookie_contract(self) -> None:
        response = self.openapi["paths"]["/auth/password-login"]["post"]["responses"][
            "200"
        ]
        contract = response["x-hermes-cookie-contract"]
        self.assertEqual(
            contract["required_name_suffixes"],
            [
                "hermes_session_at",
                "hermes_session_rt",
                "hermes_session_provider",
            ],
        )
        self.assertTrue(contract["secure"])
        self.assertTrue(contract["http_only"])
        self.assertEqual(contract["same_site"], "strict")
        self.assertTrue(contract["access_cookie_requires_expiry"])

    def test_golden_rest_fixtures_validate_against_openapi_components(self) -> None:
        for schema_name, configured_paths in REST_VALID_FIXTURES.items():
            relative_paths = (
                configured_paths
                if isinstance(configured_paths, tuple)
                else (configured_paths,)
            )
            for relative_path in relative_paths:
                errors = _openapi_schema_errors(
                    self.openapi,
                    schema_name,
                    fixture := _load(ROOT / relative_path),
                )
                errors.extend(_rest_semantic_errors(self.openapi, schema_name, fixture))
                self.assertEqual(errors, [], relative_path)

    def test_invalid_rest_fixtures_fail_closed(self) -> None:
        for schema_name, relative_paths in REST_INVALID_FIXTURES.items():
            for relative_path in relative_paths:
                errors = _openapi_schema_errors(
                    self.openapi,
                    schema_name,
                    fixture := _load(ROOT / relative_path),
                )
                errors.extend(_rest_semantic_errors(self.openapi, schema_name, fixture))
                self.assertTrue(errors, relative_path)

    def test_rest_and_realtime_limits_are_explicit(self) -> None:
        limits = self.openapi["x-hermes-response-limits"]
        self.assertEqual(limits["default_json_bytes"], 262_144)
        self.assertEqual(limits["session_transcript_json_bytes"], 4 * 1024 * 1024)
        self.assertEqual(limits.get("max_string_bytes"), 128 * 1024)
        self.assertEqual(limits.get("max_nesting_depth"), 32)
        self.assertEqual(limits.get("max_object_fields"), 1024)
        self.assertEqual(limits.get("max_array_items"), 1024)
        self.assertEqual(
            self.realtime["limits"]["max_text_frame_bytes"],
            262_144,
        )

    def test_realtime_profile_freezes_ticket_and_observer_semantics(self) -> None:
        profile = self.realtime
        self.assertEqual(profile["contract"], "cloud-api.realtime")
        self.assertEqual(profile["version"], 1)
        self.assertEqual(profile["path"], "/api/ws")
        self.assertEqual(profile["subprotocol"], "hermes.tui.v1")
        self.assertTrue(profile["ticket"]["single_use"])
        self.assertTrue(profile["ticket"]["query_only"])
        self.assertFalse(profile["ticket"]["allow_bearer_query"])
        self.assertEqual(profile["ticket"]["query_parameter"], "ticket")
        self.assertEqual(profile["transport"]["frame_type"], "text")
        self.assertEqual(
            profile["transport"]["binary_frame_action"],
            "close_protocol_error",
        )
        self.assertEqual(
            profile["transport"]["first_server_frame"],
            "gateway.ready",
        )
        self.assertEqual(
            set(profile["methods"]),
            {"session.observe.subscribe", "session.observe.unsubscribe"},
        )
        self.assertEqual(profile["sequence"]["next_event_rule"], "last_plus_one")
        self.assertEqual(profile["sequence"]["stale_event_action"], "ignore")
        self.assertEqual(profile["sequence"]["gap_action"], "resync")
        self.assertFalse(profile["sequence"]["allow_gap_inference"])
        self.assertTrue(
            profile["replay_semantics"].get(
                "range_start_must_not_exceed_event_sequence",
                False,
            )
        )
        self.assertEqual(
            profile["status_semantics"].get("running_boolean_rule"),
            "iff_status_is_running",
        )
        self.assertEqual(profile["rpc_error_codes"]["session_not_found"], 4001)
        self.assertEqual(profile["rpc_error_codes"]["replay_unavailable"], 4091)
        self.assertEqual(
            profile["control_error_codes"],
            {
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
        error_code_schema = profile["schemas"]["json_rpc_error"]["properties"]["error"][
            "properties"
        ]["code"]
        self.assertEqual(
            set(error_code_schema.get("enum", [])),
            set(profile["rpc_error_codes"].values()),
        )
        control_error_code_schema = profile["schemas"]["control_rpc_error"][
            "properties"
        ]["error"]["properties"]["code"]
        self.assertEqual(
            set(control_error_code_schema.get("enum", [])),
            set(profile["control_error_codes"].values()),
        )
        self.assertEqual(
            set(profile["close_codes"]),
            {"protocol_error", "message_too_big"},
        )

    def test_transport_profile_rejects_multiple_json_documents_per_frame(
        self,
    ) -> None:
        profile = copy.deepcopy(self.realtime)
        profile["transport"]["one_json_document_per_frame"] = False
        fixture = _load(ROOT / "fixtures/valid/cloud-realtime-ready.json")
        self.assertTrue(
            _realtime_semantic_errors(
                profile,
                "gateway_ready",
                fixture,
            )
        )

    def test_golden_realtime_fixtures_validate(self) -> None:
        schemas = self.realtime["schemas"]
        for schema_name, configured_paths in REALTIME_VALID_FIXTURES.items():
            self.assertIn(schema_name, schemas)
            relative_paths = (
                configured_paths
                if isinstance(configured_paths, tuple)
                else (configured_paths,)
            )
            for relative_path in relative_paths:
                fixture = _load(ROOT / relative_path)
                errors = _schema_errors(schemas[schema_name], fixture)
                errors.extend(
                    _realtime_semantic_errors(
                        self.realtime,
                        schema_name,
                        fixture,
                    )
                )
                self.assertEqual(errors, [], relative_path)

    def test_invalid_realtime_fixtures_fail_closed(self) -> None:
        schemas = self.realtime["schemas"]
        for schema_name, relative_paths in REALTIME_INVALID_FIXTURES.items():
            for relative_path in relative_paths:
                with self.subTest(schema=schema_name, fixture=relative_path):
                    fixture = _load(ROOT / relative_path)
                    errors = _schema_errors(schemas[schema_name], fixture)
                    errors.extend(
                        _realtime_semantic_errors(
                            self.realtime,
                            schema_name,
                            fixture,
                        )
                    )
                    self.assertTrue(errors, relative_path)

    def test_event_catalog_is_closed_and_matches_mergeable_types(self) -> None:
        event_types = self.realtime["event_types"]
        self.assertEqual(set(event_types), FROZEN_EVENT_TYPES)
        live_event_enum = self.realtime["schemas"]["session_event"]["properties"][
            "params"
        ]["properties"]["type"]["enum"]
        replay_event_enum = self.realtime["schemas"]["observe_subscribe_result"][
            "properties"
        ]["result"]["properties"]["replay_events"]["items"]["properties"]["type"][
            "enum"
        ]
        self.assertEqual(set(live_event_enum), FROZEN_EVENT_TYPES)
        self.assertEqual(set(replay_event_enum), FROZEN_EVENT_TYPES)
        mergeable = {
            name for name, contract in event_types.items() if contract["mergeable"]
        }
        self.assertEqual(
            mergeable,
            set(self.realtime["sequence"]["mergeable_event_types"]),
        )
        for name, contract in event_types.items():
            payload = contract["payload_schema"]
            self.assertEqual(payload["type"], "object", name)
            self.assertFalse(payload["additionalProperties"], name)
            for field_name in payload.get("properties", {}):
                self.assertFalse(
                    _is_forbidden_external_field(field_name),
                    (name, field_name),
                )

    def test_event_catalog_rejects_an_unnegotiated_addition(self) -> None:
        original = self.realtime
        self.realtime = copy.deepcopy(original)
        self.realtime["event_types"]["custom.notice"] = {
            "mergeable": False,
            "payload_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        }
        try:
            with self.assertRaises(AssertionError):
                self.test_event_catalog_is_closed_and_matches_mergeable_types()
        finally:
            self.realtime = original

    def test_event_catalog_rejects_schema_enum_drift(self) -> None:
        enum_paths = (
            (
                "schemas",
                "session_event",
                "properties",
                "params",
                "properties",
                "type",
                "enum",
            ),
            (
                "schemas",
                "observe_subscribe_result",
                "properties",
                "result",
                "properties",
                "replay_events",
                "items",
                "properties",
                "type",
                "enum",
            ),
        )
        for enum_path in enum_paths:
            with self.subTest(schema=enum_path[1]):
                original = self.realtime
                self.realtime = copy.deepcopy(original)
                event_enum = self.realtime
                for key in enum_path:
                    event_enum = event_enum[key]
                event_enum.remove("thinking.delta")
                try:
                    with self.assertRaises(AssertionError):
                        self.test_event_catalog_is_closed_and_matches_mergeable_types()
                finally:
                    self.realtime = original

    def test_event_payload_rejects_credential_key_variants(self) -> None:
        keys = (
            "authorization",
            "Authorization",
            "access_token",
            "accessToken",
            "ACCESS-TOKEN",
            "ACCESSTOKEN",
            "cookie",
            "Cookie",
            "bearer",
            "Bearer",
            "token",
            "wsTicket",
            "WSTICKET",
            "control_lease_id",
            "controlLeaseId",
            "CONTROLLEASEID",
        )
        for key in keys:
            with self.subTest(key=key):
                profile = copy.deepcopy(self.realtime)
                payload_schema = profile["event_types"]["message.delta"][
                    "payload_schema"
                ]
                payload_schema["properties"][key] = {"type": "string"}
                event = _load(ROOT / "fixtures/valid/cloud-realtime-event.json")
                event["params"]["type"] = "message.delta"
                event["params"]["payload"] = {
                    "text": "safe fixture text",
                    key: "must-not-cross-the-client-boundary",
                }
                errors = _schema_errors(profile["schemas"]["session_event"], event)
                errors.extend(
                    _realtime_semantic_errors(
                        profile,
                        "session_event",
                        event,
                    )
                )
                self.assertTrue(errors, key)

    def test_live_status_update_enforces_running_status_iff_in_both_directions(
        self,
    ) -> None:
        event = _load(ROOT / "fixtures/valid/cloud-realtime-status-running.json")
        for status, running in (("running", False), ("idle", True)):
            with self.subTest(status=status, running=running):
                mutated = copy.deepcopy(event)
                mutated["params"]["payload"] = {
                    "status": status,
                    "running": running,
                }
                self.assertTrue(
                    _realtime_semantic_errors(
                        self.realtime,
                        "session_event",
                        mutated,
                    )
                )

    def test_rest_recursive_limits_accept_boundary_and_reject_boundary_plus_one(
        self,
    ) -> None:
        limits = self.openapi["x-hermes-response-limits"]

        def transcript(content: object) -> dict[str, object]:
            return {
                "session_id": "session-tip-1",
                "messages": [{"role": "assistant", "content": content}],
                "pagination": {"limit": 200, "offset": 0, "returned": 1},
            }

        max_string_bytes = limits["max_string_bytes"]
        boundary_string = "界" * (max_string_bytes // 3) + "x" * (max_string_bytes % 3)
        self.assertEqual(len(boundary_string.encode("utf-8")), max_string_bytes)
        self.assertEqual(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript(boundary_string),
            ),
            [],
        )
        self.assertTrue(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript(boundary_string + "x"),
            )
        )

        max_depth = limits["max_nesting_depth"]
        boundary_depth: object = "leaf"
        for _ in range(max_depth - 4):
            boundary_depth = [boundary_depth]
        self.assertEqual(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript(boundary_depth),
            ),
            [],
        )
        self.assertTrue(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript([boundary_depth]),
            )
        )

        max_fields = limits["max_object_fields"]
        boundary_object = {f"k{index}": None for index in range(max_fields)}
        self.assertEqual(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript(boundary_object),
            ),
            [],
        )
        boundary_object["overflow"] = None
        self.assertTrue(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript(boundary_object),
            )
        )

        max_items = limits["max_array_items"]
        self.assertEqual(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript([None] * max_items),
            ),
            [],
        )
        self.assertTrue(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript([None] * (max_items + 1)),
            )
        )

        oversized_key = "界" * (max_string_bytes // 3) + "xxx"
        self.assertTrue(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript({oversized_key: None}),
            )
        )

    def test_rest_transcript_total_limit_accepts_boundary_and_rejects_plus_one(
        self,
    ) -> None:
        limits = self.openapi["x-hermes-response-limits"]
        max_string_bytes = limits["max_string_bytes"]
        total_limit = limits["session_transcript_json_bytes"]
        content = ["x" * max_string_bytes for _ in range(31)] + [""]
        transcript = {
            "session_id": "session-tip-1",
            "messages": [{"role": "assistant", "content": content}],
            "pagination": {"limit": 200, "offset": 0, "returned": 1},
        }
        encoded = json.dumps(
            transcript,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        content[-1] = "x" * (total_limit - len(encoded))
        self.assertEqual(
            len(
                json.dumps(
                    transcript,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            total_limit,
        )
        self.assertEqual(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript,
            ),
            [],
        )
        content[-1] += "x"
        self.assertTrue(
            _rest_semantic_errors(
                self.openapi,
                "SessionTranscript",
                transcript,
            )
        )

    def test_realtime_limits_reject_boundary_plus_one(self) -> None:
        event = _load(ROOT / "fixtures/valid/cloud-realtime-event.json")
        event["params"]["type"] = "message.delta"
        event["params"]["payload"] = {"text": "界" * 43_691}
        self.assertEqual(
            _schema_errors(self.realtime["schemas"]["session_event"], event),
            [],
        )
        errors = _realtime_semantic_errors(
            self.realtime,
            "session_event",
            event,
        )
        self.assertTrue(errors)

        oversized_frame = _load(
            ROOT / "fixtures/valid/cloud-realtime-subscribe-result.json"
        )
        oversized_frame["result"]["messages"] = [
            {
                "role": "assistant",
                "content": "x" * 100_000,
            },
            {
                "role": "assistant",
                "content": "y" * 100_000,
            },
            {
                "role": "assistant",
                "content": "z" * 100_000,
            },
        ]
        encoded = json.dumps(
            oversized_frame,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertGreater(
            len(encoded),
            self.realtime["limits"]["max_text_frame_bytes"],
        )
        self.assertEqual(
            _schema_errors(
                self.realtime["schemas"]["observe_subscribe_result"],
                oversized_frame,
            ),
            [],
        )
        self.assertTrue(
            _realtime_semantic_errors(
                self.realtime,
                "observe_subscribe_result",
                oversized_frame,
            )
        )

    def test_manifest_registers_external_valid_invalid_n1_and_degradation(
        self,
    ) -> None:
        manifest = _load(ROOT / "fixtures/manifest.json")
        matrices = {
            "cloud_api_v1": {
                "valid": REST_VALID_FIXTURES,
                "invalid": REST_INVALID_FIXTURES,
                "n_1": REST_N_1_FIXTURES,
                "capability_degradation": REST_DEGRADATION_FIXTURES,
            },
            "cloud_realtime_v1": {
                "valid": REALTIME_VALID_FIXTURES,
                "invalid": REALTIME_INVALID_FIXTURES,
                "n_1": REALTIME_N_1_FIXTURES,
                "capability_degradation": REALTIME_DEGRADATION_FIXTURES,
            },
        }
        classification_profiles = {
            "cloud_api_v1": {},
            "cloud_realtime_v1": {
                "n_1": {REALTIME_N_1_PROFILE_FIXTURE},
                "capability_degradation": {REALTIME_DEGRADATION_PROFILE_FIXTURE},
            },
        }
        for profile_name, categories in matrices.items():
            profile = manifest["external_profiles"][profile_name]
            registered_sets: list[set[str]] = []
            for category, configured in categories.items():
                expected = {
                    path
                    for paths in configured.values()
                    for path in (paths if isinstance(paths, tuple) else (paths,))
                }
                expected.update(
                    classification_profiles[profile_name].get(category, set())
                )
                registered = set(profile[category])
                self.assertEqual(registered, expected, (profile_name, category))
                self.assertTrue(
                    all((ROOT / path).is_file() for path in registered),
                    (profile_name, category),
                )
                registered_sets.append(registered)
            for index, left in enumerate(registered_sets):
                for right in registered_sets[index + 1 :]:
                    self.assertTrue(left.isdisjoint(right), profile_name)

        for schema_name, path in REST_N_1_FIXTURES.items():
            fixture = _load(ROOT / path)
            errors = _openapi_schema_errors(
                self.openapi,
                schema_name,
                fixture,
            )
            errors.extend(_rest_semantic_errors(self.openapi, schema_name, fixture))
            self.assertEqual(
                errors,
                [],
                path,
            )
        for schema_name, path in REST_DEGRADATION_FIXTURES.items():
            fixture = _load(ROOT / path)
            errors = _openapi_schema_errors(
                self.openapi,
                schema_name,
                fixture,
            )
            errors.extend(_rest_semantic_errors(self.openapi, schema_name, fixture))
            self.assertEqual(
                errors,
                [],
                path,
            )
        for configured in (
            REALTIME_N_1_FIXTURES,
            REALTIME_DEGRADATION_FIXTURES,
        ):
            for schema_name, path in configured.items():
                fixture = _load(ROOT / path)
                errors = _schema_errors(
                    self.realtime["schemas"][schema_name],
                    fixture,
                )
                errors.extend(
                    _realtime_semantic_errors(
                        self.realtime,
                        schema_name,
                        fixture,
                    )
                )
                self.assertEqual(errors, [], path)

    def test_realtime_compatibility_uses_explicit_client_neutral_profiles(
        self,
    ) -> None:
        profile_paths = (
            ROOT / "fixtures/compatibility/cloud-realtime-observer-n1-profile.json",
            ROOT
            / "fixtures/degradation/cloud-realtime-replay-unavailable-profile.json",
        )
        self.assertTrue(EXTERNAL_PROFILE_SCHEMA_PATH.is_file())
        for path in profile_paths:
            self.assertTrue(path.is_file(), str(path))
        self.assertTrue(
            (ROOT / "fixtures/compatibility/cloud-realtime-ready-n1.json").is_file()
        )
        self.assertFalse(
            (ROOT / "fixtures/compatibility/cloud-realtime-event-n1.json").exists()
        )

    def test_compatibility_profiles_enforce_version_capability_and_safe_effect(
        self,
    ) -> None:
        schema = _load(EXTERNAL_PROFILE_SCHEMA_PATH)
        n_1 = _load(
            ROOT / "fixtures/compatibility/cloud-realtime-observer-n1-profile.json"
        )
        degradation = _load(
            ROOT / "fixtures/degradation/cloud-realtime-replay-unavailable-profile.json"
        )
        for profile in (n_1, degradation):
            self.assertEqual(_schema_errors(schema, profile), [])

        mutations = []
        wrong_n_1_version = copy.deepcopy(n_1)
        wrong_n_1_version["peer_contract_version"] = 1
        mutations.append(wrong_n_1_version)
        n_1_without_observer = copy.deepcopy(n_1)
        n_1_without_observer["available_capabilities"] = []
        mutations.append(n_1_without_observer)
        degradation_without_unavailable = copy.deepcopy(degradation)
        degradation_without_unavailable["unavailable_capabilities"] = []
        mutations.append(degradation_without_unavailable)
        degradation_with_unsafe_effect = copy.deepcopy(degradation)
        degradation_with_unsafe_effect["safe_effect"] = (
            "observer_available_without_optional_capabilities"
        )
        mutations.append(degradation_with_unsafe_effect)
        for mutation in mutations:
            self.assertTrue(_schema_errors(schema, mutation), mutation)

        n_1_evidence = _load(ROOT / n_1["evidence_fixture"])
        n_1_errors = _schema_errors(
            self.realtime["schemas"]["gateway_ready"],
            n_1_evidence,
        )
        n_1_errors.extend(
            _realtime_semantic_errors(
                self.realtime,
                "gateway_ready",
                n_1_evidence,
            )
        )
        self.assertEqual(n_1_errors, [])
        self.assertEqual(
            n_1_evidence["params"]["payload"]["connection_role"],
            "observer",
        )

        degradation_evidence = _load(ROOT / degradation["evidence_fixture"])
        degradation_errors = _schema_errors(
            self.realtime["schemas"]["observe_subscribe_result"],
            degradation_evidence,
        )
        degradation_errors.extend(
            _realtime_semantic_errors(
                self.realtime,
                "observe_subscribe_result",
                degradation_evidence,
            )
        )
        self.assertEqual(degradation_errors, [])
        self.assertFalse(degradation_evidence["result"]["running"])
        self.assertEqual(degradation_evidence["result"]["status"], "idle")
        self.assertEqual(degradation_evidence["result"]["replay_events"], [])
        self.assertNotEqual(
            degradation_evidence,
            _load(ROOT / "fixtures/valid/cloud-realtime-subscribe-result.json"),
        )

    def test_rest_n_1_and_degradation_fixtures_express_safe_distinct_effects(
        self,
    ) -> None:
        current = _load(ROOT / "fixtures/valid/cloud-api-status.json")
        n_1 = _load(ROOT / REST_N_1_FIXTURES["StatusResponse"])
        degradation = _load(ROOT / REST_DEGRADATION_FIXTURES["StatusResponse"])

        self.assertNotEqual(n_1, current)
        self.assertNotIn("version", n_1)
        self.assertNotIn("release_date", n_1)
        self.assertTrue(n_1["gateway_running"])
        self.assertEqual(n_1["gateway_state"], "ready")
        self.assertEqual(n_1["auth_flows"], ["password"])

        self.assertNotEqual(degradation, current)
        self.assertTrue(degradation["gateway_running"])
        self.assertEqual(degradation["gateway_state"], "degraded")
        self.assertFalse(degradation["auth_required"])
        self.assertEqual(degradation["auth_providers"], [])
        self.assertEqual(degradation["auth_flows"], [])
        self.assertEqual(degradation["overall"], "degraded")

    def test_external_contract_does_not_expose_internal_connector_or_lease(
        self,
    ) -> None:
        contract_text = (
            OPENAPI_PATH.read_text(encoding="utf-8")
            + REALTIME_PATH.read_text(encoding="utf-8")
        ).lower()
        for forbidden in (
            "connector.hello",
            "connector.welcome",
            "local.welcome",
            "lease_id",
            "native_pkce",
        ):
            self.assertNotIn(forbidden, contract_text)


if __name__ == "__main__":
    unittest.main()
