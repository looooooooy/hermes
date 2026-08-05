from __future__ import annotations

import json
import unittest
from pathlib import Path

from hermes_connector.adapters.contract_codec import (
    ContractUnsupported,
    FrameTooLarge,
    InvalidEnvelope,
    InvalidUtf8,
    UnsupportedMessageType,
    decode_cloud_envelope,
    decode_local_hello,
    decode_local_welcome,
)
from hermes_connector.application.capability_negotiation import (
    RequiredCapabilityUnavailable,
    negotiate_local_capabilities,
)

CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts"
MAX_FRAME_BYTES = 262_144


def load_fixture(relative_path: str) -> dict[str, object]:
    path = CONTRACTS_ROOT / "fixtures" / relative_path
    return json.loads(path.read_text(encoding="utf-8"))


def frame(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class SharedTransportLimitTest(unittest.TestCase):
    def test_all_codecs_reject_invalid_utf8_with_stable_error(self) -> None:
        for decoder in (
            decode_local_hello,
            decode_local_welcome,
            decode_cloud_envelope,
        ):
            with self.subTest(decoder=decoder.__name__):
                with self.assertRaises(InvalidUtf8) as raised:
                    decoder(b"\xff")
                self.assertEqual(raised.exception.code, 4303)
                self.assertEqual(raised.exception.error_name, "invalid_utf8")

    def test_frame_limit_is_checked_before_json_decode(self) -> None:
        hello = frame(load_fixture("valid/local-gateway-handshake.json"))
        exact_limit = hello + (b" " * (MAX_FRAME_BYTES - len(hello)))
        self.assertEqual(len(exact_limit), MAX_FRAME_BYTES)
        self.assertEqual(decode_local_hello(exact_limit).message_type, "local.hello")

        with self.assertRaises(FrameTooLarge) as raised:
            decode_cloud_envelope(b" " * (MAX_FRAME_BYTES + 1))

        self.assertEqual(raised.exception.code, 4302)
        self.assertEqual(raised.exception.error_name, "frame_too_large")

    def test_oversized_utf8_string_is_rejected(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")
        exact = dict(envelope)
        exact["payload"] = {"value": "a" * 131_072}
        self.assertEqual(
            len(decode_cloud_envelope(frame(exact)).payload["value"]),
            131_072,
        )

        envelope["payload"] = {"value": "é" * 65_537}

        with self.assertRaisesRegex(InvalidEnvelope, "string limit"):
            decode_cloud_envelope(frame(envelope))

    def test_depth_array_and_object_limits_are_enforced(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")
        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(33):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child

        cases = (
            ("depth", nested),
            ("array", {"items": list(range(1_025))}),
            ("object", {f"field_{index}": index for index in range(1_025)}),
        )
        for limit_name, payload in cases:
            with self.subTest(limit=limit_name):
                candidate = dict(envelope)
                candidate["payload"] = payload
                with self.assertRaises(InvalidEnvelope):
                    decode_cloud_envelope(frame(candidate))

    def test_root_object_counts_as_depth_one(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")

        at_limit: dict[str, object] = {}
        cursor = at_limit
        for _ in range(30):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        candidate = dict(envelope)
        candidate["payload"] = at_limit
        decode_cloud_envelope(frame(candidate))

        beyond_limit: dict[str, object] = {}
        cursor = beyond_limit
        for _ in range(31):
            child = {}
            cursor["child"] = child
            cursor = child
        candidate["payload"] = beyond_limit
        with self.assertRaises(InvalidEnvelope):
            decode_cloud_envelope(frame(candidate))

    def test_parser_recursion_is_mapped_to_invalid_envelope(self) -> None:
        deeply_nested = (b"[" * 2_000) + b"0" + (b"]" * 2_000)

        with self.assertRaises(InvalidEnvelope):
            decode_cloud_envelope(deeply_nested)

    def test_array_and_object_limits_are_inclusive(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")
        payloads = (
            {"items": list(range(1_024))},
            {f"field_{index}": index for index in range(1_024)},
        )
        for payload in payloads:
            with self.subTest(shape=next(iter(payload))):
                candidate = dict(envelope)
                candidate["payload"] = payload
                decoded = decode_cloud_envelope(frame(candidate)).payload
                if "items" in payload:
                    self.assertEqual(len(decoded["items"]), 1_024)
                else:
                    self.assertEqual(len(decoded), 1_024)


class ExactFieldAndExtensionTest(unittest.TestCase):
    def test_local_hello_rejects_noncanonical_uuid_text(self) -> None:
        hello = load_fixture("valid/local-gateway-handshake.json")
        invalid_ids = (
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "11111111111141118111111111111111",
        )
        for invalid_id in invalid_ids:
            with self.subTest(client_instance_id=invalid_id):
                candidate = dict(hello)
                candidate["client_instance_id"] = invalid_id
                with self.assertRaises(InvalidEnvelope):
                    decode_local_hello(frame(candidate))

    def test_platform_specific_top_level_fields_are_rejected(self) -> None:
        hello = load_fixture("valid/local-gateway-handshake.json")
        welcome = load_fixture("valid/local-gateway-welcome.json")
        envelope = load_fixture("valid/cloud-connector-envelope.json")

        cases = (
            (decode_local_hello, hello, "android"),
            (decode_local_welcome, welcome, "web"),
            (decode_cloud_envelope, envelope, "desktop"),
        )
        for decoder, value, platform_field in cases:
            with self.subTest(decoder=decoder.__name__, field=platform_field):
                candidate = dict(value)
                candidate[platform_field] = {}
                with self.assertRaisesRegex(InvalidEnvelope, "top-level"):
                    decoder(frame(candidate))

    def test_only_namespaced_object_extensions_are_accepted(self) -> None:
        hello = load_fixture("valid/local-gateway-handshake.json")
        valid = dict(hello)
        valid["extensions"] = {"com.example.feature": {"enabled": True}}
        decoded = decode_local_hello(frame(valid))
        self.assertEqual(decoded.extensions["com.example.feature"]["enabled"], True)

        invalid_extensions = (
            None,
            {"android": {}},
            {"Com.example.feature": {}},
            {"com.example_feature": {}},
            {"com.example.feature": "not-an-object"},
        )
        for extensions in invalid_extensions:
            with self.subTest(extensions=extensions):
                candidate = dict(hello)
                candidate["extensions"] = extensions
                with self.assertRaises(InvalidEnvelope):
                    decode_local_hello(frame(candidate))


class CapabilityNegotiationTest(unittest.TestCase):
    def test_required_and_optional_overlap_is_rejected(self) -> None:
        hello = load_fixture("valid/local-gateway-handshake.json")
        hello["optional_capabilities"] = ["session.observe"]

        with self.assertRaisesRegex(InvalidEnvelope, "overlap"):
            decode_local_hello(frame(hello))

    def test_capability_array_limit_is_inclusive(self) -> None:
        hello = load_fixture("valid/local-gateway-handshake.json")
        hello["required_capabilities"] = [
            f"com.example.capability-{index}" for index in range(64)
        ]
        hello["optional_capabilities"] = []
        self.assertEqual(
            len(decode_local_hello(frame(hello)).required_capabilities),
            64,
        )

        hello["required_capabilities"] = [
            f"com.example.capability-{index}" for index in range(65)
        ]
        with self.assertRaises(InvalidEnvelope):
            decode_local_hello(frame(hello))

    def test_missing_required_capability_fails_stably(self) -> None:
        hello = decode_local_hello(
            frame(load_fixture("valid/local-gateway-handshake.json"))
        )

        with self.assertRaises(RequiredCapabilityUnavailable) as raised:
            negotiate_local_capabilities(
                hello,
                runtime_generation="runtime-test",
                available_capabilities=(),
            )

        self.assertEqual(raised.exception.code, 4304)
        self.assertEqual(
            raised.exception.error_name,
            "capability_not_available",
        )
        self.assertEqual(
            raised.exception.missing_capabilities,
            ("session.observe",),
        )

    def test_missing_optional_capability_is_reported_without_core_changes(
        self,
    ) -> None:
        hello = decode_local_hello(
            frame(load_fixture("valid/local-gateway-handshake.json"))
        )

        welcome = negotiate_local_capabilities(
            hello,
            runtime_generation="runtime-test",
            available_capabilities=("session.observe",),
        )

        self.assertEqual(welcome.message_type, "local.welcome")
        self.assertEqual(welcome.profile, hello.profile)
        self.assertEqual(welcome.accepted_capabilities, ("session.observe",))
        self.assertEqual(
            welcome.unavailable_optional_capabilities,
            ("session.control",),
        )
        self.assertEqual(dict(welcome.extensions), {})


class CloudEnvelopeValidationTest(unittest.TestCase):
    def test_contract_version_and_unknown_message_type_fail_closed(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")

        unsupported_version = dict(envelope)
        unsupported_version["contract_version"] = 2
        with self.assertRaises(ContractUnsupported):
            decode_cloud_envelope(frame(unsupported_version))

        unknown_type = dict(envelope)
        unknown_type["message_type"] = "android.command"
        with self.assertRaises(UnsupportedMessageType):
            decode_cloud_envelope(frame(unknown_type))

    def test_cloud_identity_sequence_time_and_context_are_validated(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")
        invalid_fields = (
            ("message_id", "not-a-uuid"),
            ("tenant_id", ""),
            ("device_id", ""),
            ("sequence", -1),
            ("sequence", True),
            ("sent_at", "2026-07-30T08:00:00+08:00"),
            ("sent_at", "2026-07-30T00:00:00+00:00"),
            ("sent_at", "2026-07-30 00:00:00"),
            ("traceparent", "00-not-a-traceparent"),
            ("traceparent", None),
            ("idempotency_key", ""),
            ("idempotency_key", None),
        )
        for field_name, invalid_value in invalid_fields:
            with self.subTest(field=field_name, value=invalid_value):
                candidate = dict(envelope)
                candidate[field_name] = invalid_value
                with self.assertRaises(InvalidEnvelope):
                    decode_cloud_envelope(frame(candidate))

    def test_cloud_extension_namespace_is_validated(self) -> None:
        envelope = load_fixture("valid/cloud-connector-envelope.json")
        envelope["extensions"] = {"ios": {}}

        with self.assertRaises(InvalidEnvelope):
            decode_cloud_envelope(frame(envelope))


if __name__ == "__main__":
    unittest.main()
