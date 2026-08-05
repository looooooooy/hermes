from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def _load(relative_path: str) -> object:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class LocalGatewayTransportV1Test(unittest.TestCase):
    def test_gateway_error_schema_is_exact_and_catalog_synchronized(self) -> None:
        schema = _load("schemas/local/gateway-error-v1.schema.json")
        catalog = _load("error-codes-v1.json")
        Draft202012Validator.check_schema(schema)

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["error"])
        self.assertEqual(set(schema["properties"]), {"error"})

        error = schema["properties"]["error"]
        self.assertFalse(error["additionalProperties"])
        self.assertEqual(set(error["required"]), {"code", "reason"})
        self.assertEqual(set(error["properties"]), {"code", "reason"})

        schema_pairs = {
            (
                variant["properties"]["code"]["const"],
                variant["properties"]["reason"]["const"],
            )
            for variant in error["oneOf"]
        }
        catalog_pairs = {(item["code"], item["name"]) for item in catalog["errors"]}
        self.assertEqual(schema_pairs, catalog_pairs)

    def test_discovery_descriptor_schema_is_closed_and_platform_neutral(self) -> None:
        schema = _load("schemas/local/gateway-discovery-v1.schema.json")
        Draft202012Validator.check_schema(schema)

        self.assertFalse(schema["additionalProperties"])
        exact_fields = {"version", "pid", "profile", "socket_path", "instance_id"}
        self.assertEqual(set(schema["required"]), exact_fields)
        self.assertEqual(set(schema["properties"]), exact_fields)
        self.assertEqual(schema["properties"]["version"]["const"], 1)

        lowered = json.dumps(schema).lower()
        for platform in ("android", "ios", "web", "desktop"):
            self.assertNotIn(f'"{platform}"', lowered)

    def test_machine_profile_freezes_posix_framing_and_validation(self) -> None:
        profile = _load("local-gateway-transport-v1.json")

        self.assertEqual(profile["contract"], "local-gateway.transport")
        self.assertEqual(profile["version"], 1)
        self.assertEqual(profile["posix_transport"], "unix-domain-stream-socket")
        self.assertEqual(profile["connection"]["max_requests"], 1)
        self.assertEqual(
            profile["framing"],
            {
                "body_encoding": "utf-8-json",
                "length_prefix_bytes": 4,
                "length_prefix_encoding": "unsigned-big-endian",
                "max_body_bytes": 262_144,
            },
        )
        self.assertEqual(
            set(profile["reject"]),
            {
                "zero-length-body",
                "body-too-large",
                "truncated-frame",
                "non-utf8-body",
                "duplicate-json-key",
            },
        )
        self.assertEqual(profile["descriptor"]["file_mode"], "0600")
        self.assertEqual(profile["descriptor"]["file_type"], "regular")
        self.assertEqual(profile["descriptor"]["parent_mode"], "0700")
        self.assertEqual(profile["descriptor"]["owner"], "effective-user")
        self.assertFalse(profile["descriptor"]["allow_symlink"])
        self.assertEqual(profile["socket"]["file_mode"], "0600")
        self.assertEqual(profile["socket"]["file_type"], "socket")
        self.assertEqual(profile["socket"]["owner"], "effective-user")
        self.assertFalse(profile["socket"]["allow_symlink"])
        self.assertTrue(profile["descriptor"]["require_live_pid"])
        self.assertEqual(profile["windows"]["transport"], "named-pipe-adapter")
        self.assertTrue(profile["windows"]["json_body_unchanged"])

    def test_normative_document_covers_lifecycle_and_cleanup(self) -> None:
        protocol = (ROOT / "LOCAL_GATEWAY_TRANSPORT_V1.md").read_text(encoding="utf-8")

        for required_text in (
            "DISCOVERING --> VALIDATING",
            "VALIDATING --> CONNECTING",
            "CONNECTING --> EXCHANGING",
            "EXCHANGING --> CLOSED",
            "atomic",
            "timeout",
            "cancellation",
            "no symlink",
            "duplicate",
            "Named Pipe",
            "JSON body",
        ):
            self.assertIn(required_text, protocol)

    def test_normative_document_freezes_error_response_dispatch(self) -> None:
        protocol = (ROOT / "LOCAL_GATEWAY_TRANSPORT_V1.md").read_text(encoding="utf-8")

        for required_text in (
            '{"error":{"code":4304,"reason":"capability_not_available"}}',
            "error-codes-v1.json",
            "4300..4306",
            "4307..4309",
            "must not contain diagnostic details",
            "must not be parsed as `local.welcome`",
            "same four-byte framing",
        ):
            self.assertIn(required_text, protocol)


if __name__ == "__main__":
    unittest.main()
