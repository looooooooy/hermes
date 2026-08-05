from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermes_connector.domain.canonical_json import canonical_json_bytes

GENERATED_VECTOR = (
    Path(__file__).parents[3]
    / "src/hermes_connector/contracts/generated/canonical-payload-digest-v1.json"
)


def test_connector_consumes_generated_unicode_canonical_digest_vector() -> None:
    contract = json.loads(GENERATED_VECTOR.read_text(encoding="utf-8"))
    vector = contract["vectors"][0]

    encoded = canonical_json_bytes(vector["payload"])

    assert encoded.decode("utf-8") == vector["canonical_utf8"]
    assert hashlib.sha256(encoded).hexdigest() == vector["sha256"]
    assert b"\\u" not in encoded
