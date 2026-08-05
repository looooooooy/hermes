"""Load the repository-owned Local Gateway v1 fixtures as E2E inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_ROOT = REPOSITORY_ROOT / "contracts"


def _load_json(relative_path: str) -> dict[str, Any]:
    value = json.loads((CONTRACTS_ROOT / relative_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(  # noqa: TRY004 - fixture contract assertion
            f"{relative_path} must contain one JSON object"
        )
    return value


@dataclass(frozen=True, slots=True)
class LocalGatewayContractAuthority:
    """The checked-in contract profile and its registered golden fixtures."""

    hello: dict[str, Any]
    welcome: dict[str, Any]
    discovery: dict[str, Any]
    transport: dict[str, Any]

    @classmethod
    def load(cls) -> LocalGatewayContractAuthority:
        manifest = _load_json("fixtures/manifest.json")
        registered = {
            item["fixture"]
            for item in manifest["valid"]
            if isinstance(item, dict) and "fixture" in item
        }
        fixture_paths = {
            "fixtures/valid/local-gateway-handshake.json",
            "fixtures/valid/local-gateway-welcome.json",
            "fixtures/valid/local-gateway-discovery.json",
        }
        if not fixture_paths.issubset(registered):
            raise AssertionError("Local Gateway fixtures are not authoritative")
        return cls(
            hello=_load_json("fixtures/valid/local-gateway-handshake.json"),
            welcome=_load_json("fixtures/valid/local-gateway-welcome.json"),
            discovery=_load_json("fixtures/valid/local-gateway-discovery.json"),
            transport=_load_json("local-gateway-transport-v1.json"),
        )

    @property
    def version(self) -> int:
        versions = {
            self.hello["contract_version"],
            self.welcome["contract_version"],
            self.discovery["version"],
            self.transport["version"],
        }
        if versions != {1}:
            raise AssertionError("Local Gateway fixture versions diverged")
        return 1
