from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from hermes_connector.adapters.contract_codec import (
    InvalidEnvelope,
    decode_cloud_envelope,
    decode_local_gateway_response,
    decode_local_hello,
    decode_local_welcome,
    encode_cloud_envelope,
    encode_local_hello,
    encode_local_welcome,
)

CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts"


def fixture_bytes(relative_path: str) -> bytes:
    return (CONTRACTS_ROOT / "fixtures" / relative_path).read_bytes()


class RootContractFixtureConformanceTest(unittest.TestCase):
    def test_authoritative_manifest_drives_supported_fixture_conformance(self) -> None:
        decoders = {
            "schemas/local/gateway-handshake-v1.schema.json": decode_local_hello,
            "schemas/local/gateway-welcome-v1.schema.json": decode_local_welcome,
            "schemas/local/gateway-error-v1.schema.json": (
                decode_local_gateway_response
            ),
            "schemas/cloud/connector-envelope-v1.schema.json": decode_cloud_envelope,
        }
        manifest = json.loads(
            (CONTRACTS_ROOT / "fixtures/manifest.json").read_text(encoding="utf-8")
        )
        supported_count = 0
        for item in manifest["valid"]:
            decoder = decoders.get(item["schema"])
            if decoder is None:
                continue
            supported_count += 1
            with self.subTest(kind="valid", fixture=item["fixture"]):
                decoder((CONTRACTS_ROOT / item["fixture"]).read_bytes())
        for item in manifest["invalid"]:
            decoder = decoders.get(item["schema"])
            if decoder is None:
                continue
            supported_count += 1
            with (
                self.subTest(kind="invalid", fixture=item["fixture"]),
                self.assertRaises(InvalidEnvelope),
            ):
                decoder((CONTRACTS_ROOT / item["fixture"]).read_bytes())

        self.assertGreaterEqual(supported_count, 7)

    def test_local_hello_consumes_authoritative_valid_fixture(self) -> None:
        raw = fixture_bytes("valid/local-gateway-handshake.json")

        hello = decode_local_hello(raw)

        self.assertEqual(hello.contract_version, 1)
        self.assertEqual(hello.message_type, "local.hello")
        self.assertEqual(
            hello.client_instance_id,
            UUID("11111111-1111-4111-8111-111111111111"),
        )
        self.assertEqual(hello.profile, "default")
        self.assertEqual(hello.required_capabilities, ("session.observe",))
        self.assertEqual(hello.optional_capabilities, ("session.control",))
        self.assertEqual(decode_local_hello(encode_local_hello(hello)), hello)

    def test_local_welcome_consumes_authoritative_valid_fixture(self) -> None:
        raw = fixture_bytes("valid/local-gateway-welcome.json")

        welcome = decode_local_welcome(raw)

        self.assertEqual(welcome.contract_version, 1)
        self.assertEqual(welcome.message_type, "local.welcome")
        self.assertEqual(welcome.runtime_generation, "runtime-20260730-01")
        self.assertEqual(welcome.accepted_capabilities, ("session.observe",))
        self.assertEqual(
            welcome.unavailable_optional_capabilities,
            ("session.control",),
        )
        self.assertEqual(decode_local_welcome(encode_local_welcome(welcome)), welcome)

    def test_cloud_envelope_consumes_authoritative_valid_fixture(self) -> None:
        raw = fixture_bytes("valid/cloud-connector-envelope.json")

        envelope = decode_cloud_envelope(raw)

        self.assertEqual(
            envelope.message_id,
            UUID("22222222-2222-4222-8222-222222222222"),
        )
        self.assertEqual(envelope.message_type, "connector.hello")
        self.assertEqual(envelope.tenant_id, "tenant-test")
        self.assertEqual(envelope.device_id, "device-test")
        self.assertEqual(envelope.sequence, 0)
        self.assertEqual(
            envelope.sent_at,
            datetime(2026, 7, 30, tzinfo=UTC),
        )
        self.assertEqual(envelope.payload["connector_version"], "0.1.0")
        self.assertEqual(
            decode_cloud_envelope(encode_cloud_envelope(envelope)),
            envelope,
        )

    def test_authoritative_invalid_local_fixture_is_rejected(self) -> None:
        raw = fixture_bytes("invalid/local-gateway-extra-field.json")

        with self.assertRaises(InvalidEnvelope):
            decode_local_hello(raw)

    def test_authoritative_invalid_cloud_fixture_is_rejected(self) -> None:
        for fixture_name in (
            "invalid/cloud-envelope-duplicate-key.json",
            "invalid/cloud-envelope-negative-sequence.json",
            "invalid/cloud-envelope-non-utc.json",
        ):
            with (
                self.subTest(fixture=fixture_name),
                self.assertRaises(InvalidEnvelope),
            ):
                decode_cloud_envelope(fixture_bytes(fixture_name))

    def test_round_trip_preserves_authoritative_fixture_fields(self) -> None:
        cases = (
            (
                "valid/local-gateway-handshake.json",
                decode_local_hello,
                encode_local_hello,
            ),
            (
                "valid/local-gateway-welcome.json",
                decode_local_welcome,
                encode_local_welcome,
            ),
            (
                "valid/cloud-connector-envelope.json",
                decode_cloud_envelope,
                encode_cloud_envelope,
            ),
        )
        for relative_path, decoder, encoder in cases:
            with self.subTest(fixture=relative_path):
                expected = json.loads(fixture_bytes(relative_path))
                actual = json.loads(encoder(decoder(fixture_bytes(relative_path))))
                self.assertEqual(actual, expected)

    def test_encoders_emit_canonical_uuid_text(self) -> None:
        hello_json = json.loads(
            encode_local_hello(
                decode_local_hello(fixture_bytes("valid/local-gateway-handshake.json"))
            )
        )
        envelope_json = json.loads(
            encode_cloud_envelope(
                decode_cloud_envelope(
                    fixture_bytes("valid/cloud-connector-envelope.json")
                )
            )
        )

        self.assertRegex(
            hello_json["client_instance_id"],
            r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
        )
        self.assertRegex(
            envelope_json["message_id"],
            r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
        )


if __name__ == "__main__":
    unittest.main()
