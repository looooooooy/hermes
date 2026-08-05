"""Check or update contract copies consumed by platform adapters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CONTRACT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CONTRACT_ROOT.parent
SYNCHRONIZED_CONTRACTS = {
    CONTRACT_ROOT / "canonical-payload-digest-v1.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "canonical-payload-digest-v1.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "canonical-payload-digest-v1.json"
        ),
    ),
    CONTRACT_ROOT / "sources/mobile-control-v1.json": (
        REPOSITORY_ROOT
        / "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/mobile-control-v1.json",
        REPOSITORY_ROOT
        / "hermes-android/core/protocol/src/test/resources/contracts/mobile-control-v1.json",
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "sources/mobile-control-v1.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "mobile-control-v1.json"
        ),
        REPOSITORY_ROOT
        / "hermes-web/src/shared/contracts/generated/mobile-control-v1.json",
    ),
    CONTRACT_ROOT / "message-types-v1.json": (
        REPOSITORY_ROOT
        / "hermes-connector/src/hermes_connector/contracts/generated/message-types-v1.json",
    ),
    CONTRACT_ROOT / "session-catalog-v1.json": (
        REPOSITORY_ROOT
        / "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/session-catalog-v1.json",
        REPOSITORY_ROOT
        / "hermes-connector/src/hermes_connector/contracts/generated/session-catalog-v1.json",
        REPOSITORY_ROOT
        / "hermes-cloud/src/hermes_cloud/contracts/generated/session-catalog-v1.json",
    ),
    CONTRACT_ROOT / "schemas/local/session-catalog-rpc-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/"
            "schemas/local/session-catalog-rpc-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/local/session-catalog-rpc-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/session-catalog-entry-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/"
            "schemas/session-catalog-entry-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/session-catalog-entry-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/session-catalog-entry-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/cloud/connector-envelope-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/connector-envelope-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/command-deliver-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/command-deliver-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/command-receipt-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/command-receipt-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/command-result-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/command-result-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/control-request-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/control-request-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/control-response-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/control-response-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-snapshot-page-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-catalog-event-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-event-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-event-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-catalog-ack-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-ack-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-ack-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-catalog-nack-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-nack-v1.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-catalog-nack-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-snapshot-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-snapshot-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-event-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-event-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-observe-open-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-observe-open-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-observe-close-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-observe-close-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/cloud/payloads/stream-ack-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/stream-ack-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/cloud/payloads/stream-nack-v1.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/stream-nack-v1.schema.json"
        ),
    ),
    CONTRACT_ROOT / "observer-output-parity-v2.json": (
        REPOSITORY_ROOT
        / "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/observer-output-parity-v2.json",
        REPOSITORY_ROOT
        / "hermes-connector/src/hermes_connector/contracts/generated/observer-output-parity-v2.json",
        REPOSITORY_ROOT
        / "hermes-cloud/src/hermes_cloud/contracts/generated/observer-output-parity-v2.json",
        REPOSITORY_ROOT
        / "hermes-android/core/protocol/src/test/resources/contracts/observer-output-parity-v2.json",
        REPOSITORY_ROOT
        / "hermes-web/src/shared/contracts/generated/observer-output-parity-v2.json",
    ),
    CONTRACT_ROOT / "cloud-realtime-v2.json": (
        REPOSITORY_ROOT
        / "hermes-cloud/src/hermes_cloud/contracts/generated/cloud-realtime-v2.json",
        REPOSITORY_ROOT
        / "hermes-android/core/protocol/src/test/resources/contracts/cloud-realtime-v2.json",
        REPOSITORY_ROOT
        / "hermes-web/src/shared/contracts/generated/cloud-realtime-v2.json",
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-snapshot-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/"
            "schemas/cloud/payloads/session-snapshot-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-snapshot-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-snapshot-v2.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/cloud/payloads/session-event-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-agent-plugin/src/hermes_agent_plugin/contracts/generated/"
            "schemas/cloud/payloads/session-event-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-event-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-event-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-android/core/protocol/src/test/resources/contracts/"
            "schemas/cloud/payloads/session-event-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-web/src/shared/contracts/generated/"
            "schemas/cloud/payloads/session-event-v2.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/public/session-event-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/public/session-event-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-android/core/protocol/src/test/resources/contracts/"
            "schemas/public/session-event-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-web/src/shared/contracts/generated/"
            "schemas/public/session-event-v2.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-observe-open-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-observe-open-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-observe-open-v2.schema.json"
        ),
    ),
    CONTRACT_ROOT
    / "schemas/cloud/payloads/session-observe-close-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/session-observe-close-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/session-observe-close-v2.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/cloud/payloads/stream-ack-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/stream-ack-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/stream-ack-v2.schema.json"
        ),
    ),
    CONTRACT_ROOT / "schemas/cloud/payloads/stream-nack-v2.schema.json": (
        REPOSITORY_ROOT
        / (
            "hermes-connector/src/hermes_connector/contracts/generated/"
            "schemas/cloud/payloads/stream-nack-v2.schema.json"
        ),
        REPOSITORY_ROOT
        / (
            "hermes-cloud/src/hermes_cloud/contracts/generated/"
            "schemas/cloud/payloads/stream-nack-v2.schema.json"
        ),
    ),
}


def _canonical_json(path: Path) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check() -> list[Path]:
    """Return consumer copies that differ from their normative source."""
    drifted: list[Path] = []
    for source, consumers in SYNCHRONIZED_CONTRACTS.items():
        expected = _canonical_json(source)
        for consumer in consumers:
            if not consumer.is_file() or _canonical_json(consumer) != expected:
                drifted.append(consumer)
    return drifted


def write() -> None:
    """Synchronize declared consumer copies from the normative source."""
    for source, consumers in SYNCHRONIZED_CONTRACTS.items():
        expected = _canonical_json(source)
        for consumer in consumers:
            consumer.parent.mkdir(parents=True, exist_ok=True)
            consumer.write_text(expected, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="update consumer copies; default behavior is read-only checking",
    )
    args = parser.parse_args()
    if args.write:
        write()
    drifted = check()
    if drifted:
        for path in drifted:
            print(path.relative_to(REPOSITORY_ROOT))
        return 1
    print("consumer contract copies are synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
