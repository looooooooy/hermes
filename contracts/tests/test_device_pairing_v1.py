from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "openapi/cloud-api-v1.json"
PROFILE_PATH = ROOT / "device-pairing-v1.json"
PROFILE_SCHEMA_PATH = ROOT / "schemas/cloud/device-pairing-v1.schema.json"

CANONICAL_UUID_PATTERN = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
PAIRING_PATHS = {
    "/api/device-pairing/offers": ("post", "connector_bootstrap"),
    "/api/device-pairing/offers/{pairing_offer_id}": ("get", "pairing_offer"),
    "/api/device-pairing/claims": ("post", "owner"),
    "/api/device-pairing/sessions/{pairing_session_id}": (
        "get",
        "owner",
    ),
    "/api/device-pairing/sessions/{pairing_session_id}/confirm": (
        "post",
        "owner",
    ),
    "/api/device-pairing/sessions/{pairing_session_id}/cancel": (
        "post",
        "owner",
    ),
    "/api/device-pairing/sessions/{pairing_session_id}/proof": (
        "post",
        "pairing_offer",
    ),
    "/api/device-auth/challenges": ("post", "device_bootstrap"),
    "/api/device-auth/tokens": ("post", "device_proof"),
    "/api/devices/{device_id}/revoke": ("post", "owner"),
}
MUTATING_PAIRING_PATHS = {
    path for path, (method, _principal) in PAIRING_PATHS.items() if method == "post"
}
OWNER_AUTHORITY_FIELDS = {
    "tenant_id",
    "user_id",
    "workspace_id",
    "agent_id",
    "device_id",
    "scopes",
}


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_local_refs(
    schema: object,
    components: dict[str, object],
) -> object:
    if isinstance(schema, list):
        return [_resolve_local_refs(item, components) for item in schema]
    if not isinstance(schema, dict):
        return schema
    reference = schema.get("$ref")
    if isinstance(reference, str):
        prefix = "#/components/schemas/"
        if not reference.startswith(prefix):
            raise AssertionError(f"unsupported fixture schema reference: {reference}")
        return _resolve_local_refs(components[reference[len(prefix) :]], components)
    return {
        key: _resolve_local_refs(value, components) for key, value in schema.items()
    }


class DevicePairingV1ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = _load(OPENAPI_PATH)

    def setUp(self) -> None:
        self.assertTrue(PROFILE_SCHEMA_PATH.is_file(), str(PROFILE_SCHEMA_PATH))
        self.assertTrue(PROFILE_PATH.is_file(), str(PROFILE_PATH))
        self.profile_schema = _load(PROFILE_SCHEMA_PATH)
        self.profile = _load(PROFILE_PATH)

    def test_profile_is_a_strict_draft_2020_12_contract(self) -> None:
        Draft202012Validator.check_schema(self.profile_schema)
        self.assertEqual(
            self.profile_schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        errors = list(
            Draft202012Validator(
                self.profile_schema,
                format_checker=FormatChecker(),
            ).iter_errors(self.profile)
        )
        self.assertEqual(errors, [])
        self.assertFalse(self.profile_schema["additionalProperties"])
        self.assertEqual(self.profile["contract"], "hermes.device-pairing")
        self.assertEqual(self.profile["version"], 1)

    def test_pairing_routes_and_authentication_subjects_are_explicit(self) -> None:
        paths = self.openapi["paths"]
        auth_contract = self.profile["authentication_subjects"]
        security_by_subject = {
            "connector_bootstrap": [],
            "pairing_offer": [{"pairingOfferAuth": []}],
            "owner": [{"bearerAuth": []}],
            "device_bootstrap": [],
            "device_proof": [],
        }

        for path, (method, subject) in PAIRING_PATHS.items():
            self.assertIn(path, paths)
            operation = paths[path][method]
            self.assertEqual(operation["security"], security_by_subject[subject])
            self.assertEqual(
                operation["x-hermes-authentication-subject"],
                subject,
            )
            self.assertEqual(auth_contract[operation["operationId"]], subject)

        schemes = self.openapi["components"]["securitySchemes"]
        self.assertEqual(
            schemes["pairingOfferAuth"],
            {
                "type": "apiKey",
                "in": "header",
                "name": "X-Hermes-Pairing-Offer",
                "description": (
                    "Raw connector-only pairing offer secret. "
                    "The server stores only its SHA-256 digest."
                ),
            },
        )

    def test_connector_offer_is_tenant_neutral_and_cannot_self_authorize(self) -> None:
        schema = self.openapi["components"]["schemas"]["CreatePairingOfferRequest"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "connector_instance_id",
                "display_name",
                "platform_family",
                "connector_version",
                "key_algorithm",
                "public_key",
            },
        )
        properties = set(schema["properties"])
        self.assertTrue(properties.isdisjoint(OWNER_AUTHORITY_FIELDS))
        self.assertEqual(schema["properties"]["key_algorithm"]["const"], "Ed25519")
        self.assertEqual(
            schema["properties"]["public_key"]["contentEncoding"],
            "base64url",
        )
        self.assertEqual(
            self.profile["authority"]["connector_offer"],
            {
                "trust": "tenant_neutral",
                "forbidden_self_asserted_fields": sorted(OWNER_AUTHORITY_FIELDS),
                "authorization_effect": "none",
            },
        )

    def test_authenticated_owner_claims_and_confirms_the_authoritative_binding(
        self,
    ) -> None:
        claim = self.openapi["components"]["schemas"]["ClaimPairingSessionRequest"]
        self.assertFalse(claim["additionalProperties"])
        self.assertEqual(
            set(claim["required"]),
            {
                "pairing_code",
                "workspace_id",
                "agent_id",
                "device_display_name",
                "scopes",
                "expected_revision",
            },
        )
        self.assertNotIn("tenant_id", claim["properties"])
        self.assertNotIn("user_id", claim["properties"])
        self.assertNotIn("device_id", claim["properties"])
        self.assertEqual(
            claim["properties"]["scopes"]["items"]["enum"],
            ["session.observe", "session.control.request"],
        )
        claim_operation = self.openapi["paths"]["/api/device-pairing/claims"]["post"]
        self.assertEqual(claim_operation["operationId"], "claimPairingCode")
        self.assertNotIn(
            "#/components/parameters/PairingOfferId",
            {
                parameter.get("$ref")
                for parameter in claim_operation.get("parameters", [])
            },
        )
        self.assertEqual(
            self.profile["authority"]["owner_claim_lookup"],
            {
                "lookup_input": "pairing_code_request_body_only",
                "lookup_operation": "atomic_pairing_code_digest_match",
                "unauthenticated_lookup": False,
                "offer_id_required_from_owner": False,
                "unknown_or_unavailable_response": "PAIRING_CLAIM_UNAVAILABLE",
                "unknown_or_wrong_code_offer_effect": "none",
                "correct_code_transition": ("atomic_offer_digest_compare_and_swap"),
                "idempotent_replay": ("same_key_same_digest_replays_prior_result"),
            },
        )

        confirm = self.openapi["components"]["schemas"]["ConfirmPairingSessionRequest"]
        self.assertEqual(
            set(confirm["required"]),
            {
                "credential_fingerprint",
                "expected_revision",
            },
        )
        self.assertEqual(
            self.profile["authority"]["owner_binding"],
            {
                "principal_derived_fields": ["tenant_id", "user_id"],
                "owner_selected_fields": [
                    "workspace_id",
                    "agent_id",
                    "device_display_name",
                    "scopes",
                ],
                "server_generated_fields": ["device_id", "credential_id"],
                "confirmation_requires_fingerprint_match": True,
            },
        )
        owner_view = self.openapi["components"]["schemas"]["PairingOwnerView"]
        self.assertTrue(
            {
                "pairing_offer_id",
                "pairing_session_id",
                "binding",
            }.issubset(owner_view["required"])
        )
        self.assertTrue(
            OWNER_AUTHORITY_FIELDS.issubset(
                owner_view["properties"]["binding"]["required"]
            )
        )
        review_fields = {
            "display_name",
            "platform_family",
            "connector_version",
            "key_algorithm",
            "credential_fingerprint",
            "expires_at",
        }
        self.assertTrue(review_fields.issubset(owner_view["required"]))
        self.assertEqual(
            owner_view["properties"]["key_algorithm"]["const"],
            "Ed25519",
        )
        self.assertEqual(
            owner_view["properties"]["credential_fingerprint"]["pattern"],
            "^SHA256:[A-Za-z0-9_-]{43}$",
        )
        self.assertEqual(
            self.profile["authority"]["owner_review"],
            {
                "connector_supplied_untrusted_fields": [
                    "display_name",
                    "platform_family",
                    "connector_version",
                    "key_algorithm",
                ],
                "server_derived_fields": ["credential_fingerprint", "expires_at"],
                "trust": "display_only_not_authorization",
            },
        )

    def test_owner_can_poll_the_authoritative_pairing_snapshot(self) -> None:
        operation = self.openapi["paths"][
            "/api/device-pairing/sessions/{pairing_session_id}"
        ]["get"]
        owner_view = self.openapi["components"]["schemas"]["PairingOwnerView"]
        revoke = self.openapi["components"]["schemas"]["RevokeDeviceRequest"]

        self.assertEqual(operation["operationId"], "getPairingSession")
        self.assertEqual(operation["security"], [{"bearerAuth": []}])
        self.assertEqual(operation["x-hermes-authentication-subject"], "owner")
        self.assertNotIn("x-hermes-idempotency", operation)
        self.assertEqual(
            operation["x-hermes-polling"],
            {
                "minimum_interval_ms": 1000,
                "response": "complete_authoritative_owner_snapshot",
                "stop_activation_states": ["active", "blocked"],
                "stop_session_states": ["expired", "cancelled"],
            },
        )
        self.assertEqual(
            operation["x-hermes-error-codes"],
            [
                "UNAUTHORIZED",
                "FORBIDDEN",
                "PAIRING_NOT_FOUND",
            ],
        )
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/PairingOwnerView"},
        )
        self.assertEqual(
            operation["responses"]["200"]["headers"]["Cache-Control"]["schema"],
            {"type": "string", "const": "no-store"},
        )
        self.assertEqual(
            operation["responses"]["404"],
            {"$ref": "#/components/responses/PairingError"},
        )
        self.assertEqual(
            self.profile["authentication_subjects"]["getPairingSession"],
            "owner",
        )
        self.assertTrue(
            {"revision", "device_revision"}.issubset(owner_view["required"])
        )
        self.assertIn(
            "pairing session",
            owner_view["properties"]["revision"]["description"].lower(),
        )
        self.assertIn(
            "device lifecycle",
            owner_view["properties"]["device_revision"]["description"].lower(),
        )
        self.assertIn(
            "device_revision",
            revoke["properties"]["expected_revision"]["description"],
        )

        owner_fixture = _load(
            ROOT / "fixtures/valid/device-pairing-owner-view.json"
        )
        revoke_fixture = _load(ROOT / "fixtures/valid/device-revoke-request.json")
        revoked_fixture = _load(ROOT / "fixtures/valid/device-revoke-response.json")
        self.assertNotEqual(
            owner_fixture["revision"],
            owner_fixture["device_revision"],
        )
        self.assertEqual(owner_fixture["state"], "claimed")
        self.assertEqual(
            owner_fixture["activation_state"],
            "waiting_owner_confirmation",
        )
        self.assertEqual(owner_fixture["device_revision"], 1)
        self.assertEqual(
            revoke_fixture["expected_revision"],
            owner_fixture["device_revision"],
        )
        self.assertEqual(
            revoked_fixture["device_id"],
            owner_fixture["binding"]["device_id"],
        )
        self.assertEqual(
            revoked_fixture["revision"],
            revoke_fixture["expected_revision"] + 1,
        )

    def test_pairing_code_resolution_is_authenticated_and_not_a_lookup_api(
        self,
    ) -> None:
        paths = self.openapi["paths"]
        claim = paths["/api/device-pairing/claims"]["post"]
        self.assertEqual(claim["security"], [{"bearerAuth": []}])
        request_schema = claim["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(
            request_schema,
            {"$ref": "#/components/schemas/ClaimPairingSessionRequest"},
        )
        self.assertNotIn(
            "/api/device-pairing/offers/{pairing_offer_id}/claim",
            paths,
        )
        for path, item in paths.items():
            for operation in item.values():
                if not isinstance(operation, dict):
                    continue
                if operation.get("security") == []:
                    self.assertNotIn("pairing_code", path)
                    self.assertNotEqual(
                        operation.get("operationId"),
                        "lookupPairingCode",
                    )

    def test_failed_pairing_code_lookup_is_principal_limited_and_non_enumerable(
        self,
    ) -> None:
        limits = self.profile["limits"]
        self.assertEqual(limits["failed_code_lookup_window_seconds"], 300)
        self.assertEqual(
            limits["max_failed_code_lookups_per_owner_principal"],
            5,
        )
        self.assertEqual(
            limits["failed_code_lookup_principal_fields"],
            ["tenant_id", "user_id"],
        )
        self.assertEqual(
            limits["failed_code_lookup_effect"],
            "block_principal_claims_until_window_expires",
        )
        self.assertEqual(limits["failed_code_lookup_offer_effect"], "none")
        self.assertNotIn("max_failed_claim_attempts", limits)
        self.assertNotIn("failed_attempt_limit_effect", limits)

        claim = self.openapi["paths"]["/api/device-pairing/claims"]["post"]
        self.assertEqual(
            claim["x-hermes-pairing-claim-policy"],
            {
                "principal_key": ["tenant_id", "user_id"],
                "failed_lookup_window_seconds": 300,
                "max_failed_lookups": 5,
                "failed_lookup_effect": ("block_principal_claims_until_window_expires"),
                "failed_lookup_offer_effect": "none",
                "unavailable_response_code": "PAIRING_CLAIM_UNAVAILABLE",
                "correct_code_effect": ("atomic_offer_digest_compare_and_swap"),
                "idempotency_replay": "same_key_same_digest",
            },
        )
        self.assertEqual(
            claim["x-hermes-error-codes"],
            [
                "UNAUTHORIZED",
                "FORBIDDEN",
                "PAIRING_INVALID_REQUEST",
                "PAIRING_CLAIM_UNAVAILABLE",
                "IDEMPOTENCY_CONFLICT",
                "PAIRING_CLAIM_RATE_LIMITED",
            ],
        )
        for code in (
            "PAIRING_NOT_FOUND",
            "PAIRING_STATE_CONFLICT",
            "PAIRING_EXPIRED",
            "PAIRING_ATTEMPTS_EXCEEDED",
        ):
            self.assertNotIn(code, claim["x-hermes-error-codes"])

        responses = claim["responses"]
        self.assertEqual(
            responses["404"],
            {"$ref": "#/components/responses/PairingClaimUnavailable"},
        )
        self.assertEqual(
            responses["429"],
            {"$ref": "#/components/responses/PairingClaimRateLimited"},
        )
        unavailable = self.openapi["components"]["schemas"][
            "PairingClaimUnavailableResponse"
        ]
        self.assertEqual(
            unavailable,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "reason"],
                "properties": {
                    "code": {
                        "type": "string",
                        "const": "PAIRING_CLAIM_UNAVAILABLE",
                    },
                    "reason": {
                        "type": "string",
                        "const": "pairing claim unavailable",
                    },
                },
            },
        )
        retry_after = self.openapi["components"]["responses"][
            "PairingClaimRateLimited"
        ]["headers"]["Retry-After"]["schema"]
        self.assertEqual(
            retry_after,
            {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
            },
        )

    def test_state_machine_separates_owner_confirmation_from_key_proof(self) -> None:
        self.assertEqual(
            self.profile["state_machine"],
            {
                "offer": {
                    "initial": "pending",
                    "transitions": {
                        "pending": ["claimed", "expired", "cancelled"],
                        "claimed": [],
                        "expired": [],
                        "cancelled": [],
                    },
                },
                "session": {
                    "initial": "claimed",
                    "transitions": {
                        "claimed": ["confirmed", "expired", "cancelled"],
                        "confirmed": ["expired", "cancelled"],
                        "expired": [],
                        "cancelled": [],
                    },
                },
                "activation_states": [
                    "waiting_owner",
                    "waiting_owner_confirmation",
                    "awaiting_proof",
                    "active",
                    "blocked",
                ],
                "credential_activation_rule": (
                    "confirmed pairing plus verified single-use Ed25519 challenge"
                ),
            },
        )
        poll = self.openapi["components"]["schemas"]["ConnectorPairingStatusResponse"]
        branches = poll["oneOf"]
        self.assertEqual(len(branches), 5)
        self.assertEqual(
            {
                (
                    branch["properties"]["state"].get("const"),
                    branch["properties"]["activation_state"]["const"],
                )
                for branch in branches
            },
            {
                ("pending", "waiting_owner"),
                ("claimed", "waiting_owner_confirmation"),
                ("confirmed", "awaiting_proof"),
                ("confirmed", "active"),
                (None, "blocked"),
            },
        )

    def test_pairing_code_session_secret_and_challenge_are_short_lived(self) -> None:
        ttl = self.profile["ttl_seconds"]
        self.assertEqual(ttl["pairing_session"], 300)
        self.assertEqual(ttl["pairing_expiry_origin"], "offer_created_at")
        self.assertFalse(ttl["claim_or_confirm_extends_pairing"])
        self.assertTrue(ttl["challenge_must_not_outlive_pairing"])
        self.assertLessEqual(ttl["device_challenge"], 60)
        self.assertLessEqual(ttl["connector_token_max"], 3600)
        self.assertEqual(ttl["connector_token_min"], 1)

        create_response = self.openapi["components"]["schemas"][
            "CreatePairingOfferResponse"
        ]
        self.assertEqual(
            create_response["properties"]["ttl_seconds"]["const"],
            300,
        )
        self.assertEqual(
            create_response["properties"]["pairing_code"]["pattern"],
            "^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$",
        )
        self.assertEqual(
            create_response["properties"]["credential_fingerprint"]["pattern"],
            "^SHA256:[A-Za-z0-9_-]{43}$",
        )
        self.assertIn(
            "pairing_offer_secret",
            create_response["required"],
        )
        self.assertEqual(
            create_response["properties"]["pairing_offer_secret"]["pattern"],
            "^[A-Za-z0-9_-]{43}$",
        )
        self.assertFalse(
            self.profile["secrets"]["pairing_code"]["alone_authorizes_activation"]
        )
        self.assertEqual(
            self.profile["secrets"]["pairing_offer_secret"]["transport"],
            "x_hermes_pairing_offer_header_only",
        )
        self.assertFalse(
            self.profile["secrets"]["pairing_offer_secret"][
                "alone_authorizes_activation"
            ]
        )

    def test_ed25519_proof_activates_and_reauthenticates_the_device(self) -> None:
        components = self.openapi["components"]["schemas"]
        proof = components["DeviceChallengeProofRequest"]
        self.assertEqual(proof["properties"]["signature_algorithm"]["const"], "Ed25519")
        self.assertEqual(
            proof["properties"]["signature"]["contentEncoding"],
            "base64url",
        )
        self.assertEqual(
            proof["properties"]["signing_payload"]["contentEncoding"],
            "base64url",
        )
        self.assertNotIn("private_key", json.dumps(components).lower())
        self.assertEqual(
            self.openapi["paths"][
                "/api/device-pairing/sessions/{pairing_session_id}/proof"
            ]["post"]["security"],
            [{"pairingOfferAuth": []}],
        )
        self.assertEqual(
            self.profile["proof_of_possession"]["additional_authentication"],
            "pairing_offer_secret",
        )

        repeated_challenge = components["DeviceAuthenticationChallengeRequest"]
        self.assertEqual(
            set(repeated_challenge["required"]),
            {"device_id", "credential_id"},
        )
        repeated_token = components["DeviceAuthenticationTokenRequest"]
        self.assertEqual(
            set(repeated_token["required"]),
            {
                "device_id",
                "credential_id",
                "challenge_id",
                "signing_payload",
                "signature_algorithm",
                "signature",
            },
        )
        self.assertEqual(
            self.profile["proof_of_possession"],
            {
                "algorithm": "Ed25519",
                "private_key_location": "connector_os_secure_store_only",
                "challenge_storage": "sha256_digest_only",
                "challenge_single_use": True,
                "signing_payload_prefix": "hermes-device-auth-v1\\0",
                "signature_input": "base64url_decoded_signing_payload_bytes",
                "required_for_initial_activation": True,
                "required_for_every_connector_token": True,
                "additional_authentication": "pairing_offer_secret",
            },
        )

    def test_connector_token_is_short_lived_bound_and_never_a_control_lease(
        self,
    ) -> None:
        response = self.openapi["components"]["schemas"]["ConnectorTokenResponse"]
        self.assertLessEqual(
            response["properties"]["ttl_seconds"]["maximum"],
            3600,
        )
        binding = response["properties"]["binding"]
        self.assertEqual(
            set(binding["required"]),
            {
                "tenant_id",
                "device_id",
                "credential_id",
                "agent_id",
                "scopes",
            },
        )
        self.assertTrue(
            {"tenant_id", "device_id", "credential_id", "agent_id", "scopes"}.issubset(
                self.profile["connector_token"]["required_binding_claims"]
            )
        )
        self.assertEqual(
            self.profile["connector_token"]["revocation_check"],
            "on_issue_and_every_gateway_connection",
        )
        self.assertEqual(
            self.profile["connector_token"]["blocked_device_states"],
            ["suspended", "revoked"],
        )
        self.assertEqual(
            self.profile["connector_token"]["blocked_credential_states"],
            ["expired", "revoked"],
        )
        self.assertFalse(
            self.profile["controller_invariant"]["pairing_acquires_control"]
        )
        self.assertEqual(
            self.profile["controller_invariant"]["controller_cardinality"],
            "at_most_one_per_realtime_session",
        )
        self.assertEqual(
            self.profile["controller_invariant"]["control_scope_semantics"],
            "permission_to_request_control_not_control_ownership",
        )

    def test_every_mutation_has_digest_bound_idempotency(self) -> None:
        parameter = self.openapi["components"]["parameters"]["IdempotencyKey"]
        self.assertEqual(parameter["name"], "Idempotency-Key")
        self.assertEqual(parameter["in"], "header")
        self.assertTrue(parameter["required"])
        self.assertEqual(parameter["schema"]["pattern"], CANONICAL_UUID_PATTERN)

        for path in MUTATING_PAIRING_PATHS:
            operation = self.openapi["paths"][path]["post"]
            refs = {
                item.get("$ref")
                for item in operation.get("parameters", [])
                if isinstance(item, dict)
            }
            self.assertIn("#/components/parameters/IdempotencyKey", refs, path)
            self.assertEqual(
                operation["x-hermes-idempotency"],
                {
                    "same_key_same_digest": "replay_business_result",
                    "same_key_different_digest": "reject_idempotency_conflict",
                },
            )

        self.assertEqual(
            self.profile["idempotency"]["request_digest"],
            "sha256_of_canonical_method_path_principal_and_body",
        )
        self.assertEqual(
            self.profile["idempotency"]["same_key_same_digest"],
            "replay_business_result_without_duplicate_effect",
        )
        self.assertEqual(
            self.profile["idempotency"]["same_key_different_digest"],
            "reject_with_idempotency_conflict",
        )

    def test_secret_log_trace_and_persistence_classification_is_closed(self) -> None:
        secrets = self.profile["secrets"]
        self.assertEqual(
            set(secrets),
            {
                "device_private_key",
                "pairing_code",
                "pairing_offer_secret",
                "device_challenge",
                "device_signature",
                "connector_token",
            },
        )
        for name, contract in secrets.items():
            with self.subTest(secret=name):
                self.assertEqual(contract["log"], "forbidden")
                self.assertEqual(contract["trace"], "forbidden")
                self.assertEqual(contract["diagnostic"], "forbidden")
                self.assertNotEqual(contract["persistence"], "plaintext")
        self.assertEqual(
            secrets["device_private_key"]["persistence"],
            "os_secure_store_only",
        )
        self.assertEqual(
            secrets["pairing_code"]["persistence"],
            "sha256_digest_only",
        )
        self.assertEqual(
            secrets["pairing_offer_secret"]["persistence"],
            "sha256_digest_only",
        )
        self.assertEqual(
            secrets["device_challenge"]["persistence"],
            "sha256_digest_only",
        )
        self.assertEqual(
            secrets["connector_token"]["persistence"],
            "never_plaintext_at_rest",
        )

    def test_cancel_and_revoke_fail_closed_and_do_not_auto_repair(self) -> None:
        revocation = self.profile["revocation"]
        self.assertEqual(
            revocation["effects"],
            [
                "close_existing_connector_wss",
                "reject_new_device_challenges",
                "reject_connector_token_issuance",
                "clear_unexecuted_sensitive_commands",
                "invalidate_pairing_session_auth",
                "retain_redacted_security_audit",
            ],
        )
        self.assertFalse(revocation["automatic_repairing"])
        self.assertEqual(
            revocation["owner_endpoint"],
            "/api/devices/{device_id}/revoke",
        )

        cancel = self.openapi["components"]["schemas"]["CancelPairingSessionRequest"]
        self.assertEqual(
            cancel["properties"]["reason"]["enum"],
            ["owner_cancelled", "fingerprint_mismatch"],
        )
        revoke = self.openapi["components"]["schemas"]["RevokeDeviceRequest"]
        self.assertEqual(
            revoke["properties"]["reason"]["enum"],
            ["user_requested", "device_lost", "security_event"],
        )

    def test_connector_lifecycle_is_signalled_only_by_exact_policy_close(
        self,
    ) -> None:
        self.assertEqual(
            self.profile["connector_wss_lifecycle_close"],
            {
                "policy_close_code": 1008,
                "matching": "code_and_exact_reason",
                "reasons": {
                    "revoked": "device_authorization_revoked",
                    "suspended": "device_authorization_suspended",
                },
                "unknown_close_effect": (
                    "disconnect_and_reconnect_without_lifecycle_change"
                ),
                "envelope_message_type": "forbidden",
            },
        )

    def test_pairing_error_catalog_and_operation_mappings_are_closed(self) -> None:
        expected = {
            "PAIRING_INVALID_REQUEST": 400,
            "UNAUTHORIZED": 401,
            "FORBIDDEN": 403,
            "PAIRING_NOT_FOUND": 404,
            "PAIRING_STATE_CONFLICT": 409,
            "IDEMPOTENCY_CONFLICT": 409,
            "PAIRING_EXPIRED": 410,
            "CHALLENGE_EXPIRED": 410,
            "CHALLENGE_INVALID": 401,
            "CHALLENGE_REPLAYED": 409,
            "PAIRING_CLAIM_UNAVAILABLE": 404,
            "PAIRING_CLAIM_RATE_LIMITED": 429,
            "DEVICE_AUTH_UNAVAILABLE": 403,
            "RATE_LIMITED": 429,
        }
        self.assertEqual(self.profile["errors"], expected)

        for path, (method, _subject) in PAIRING_PATHS.items():
            operation = self.openapi["paths"][path][method]
            declared = operation["x-hermes-error-codes"]
            self.assertTrue(declared, path)
            self.assertTrue(set(declared).issubset(expected), path)
            response_statuses = {int(status) for status in operation["responses"]}
            for code in declared:
                self.assertIn(expected[code], response_statuses, (path, code))

    def test_pairing_fixtures_validate_against_openapi_components(self) -> None:
        components = self.openapi["components"]["schemas"]
        fixture_contract = self.profile["fixtures"]
        for category in ("valid", "invalid"):
            for fixture in fixture_contract[category]:
                relative_path = fixture["path"]
                schema = _resolve_local_refs(
                    components[fixture["schema"]],
                    components,
                )
                errors = list(
                    Draft202012Validator(
                        schema,
                        format_checker=FormatChecker(),
                    ).iter_errors(_load(ROOT / relative_path))
                )
                if category == "valid":
                    self.assertEqual(errors, [], relative_path)
                else:
                    self.assertTrue(errors, relative_path)


if __name__ == "__main__":
    unittest.main()
