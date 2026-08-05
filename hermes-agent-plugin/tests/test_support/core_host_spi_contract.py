"""Load the real pinned Core public Host SPI contract from the patch bundle."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from types import ModuleType

_MODULE_NAME = "hermes_cli.extension_host_v1"
_CONTRACT_PATH = "hermes_cli/extension_host_v1.py"
_EXPECTED_REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
_EXPECTED_VERSION = "0.19.0"
_EXPECTED_COMMIT = "14db1a99e21e5523ee61f10f5c3300a5087e8449"


def _bundle_root() -> Path:
    return Path(__file__).resolve().parents[3] / "upstream/hermes-core-host-spi-v1"


def _locked_patches() -> tuple[Path, ...]:
    bundle = _bundle_root()
    lock = json.loads((bundle / "upstream.lock.json").read_text(encoding="utf-8"))
    upstream = lock["upstream"]
    assert upstream == {
        "distribution": "hermes-agent",
        "repository": _EXPECTED_REPOSITORY,
        "version": _EXPECTED_VERSION,
        "commit": _EXPECTED_COMMIT,
    }
    assert lock["stage"] == 3
    expected_paths = (
        "patches/0001-gateway-extension-host-spi-v1-stage1.patch",
        "patches/0002-session-authority-observer-stage2.patch",
        "patches/0003-production-composition-owner-actions-stage3.patch",
    )
    assert tuple(item["path"] for item in lock["patches"]) == expected_paths
    patch_paths: list[Path] = []
    for item in lock["patches"]:
        patch_path = bundle / item["path"]
        assert hashlib.sha256(patch_path.read_bytes()).hexdigest() == item["sha256"]
        patch_paths.append(patch_path)
    return tuple(patch_paths)


def _stage1_source(patch_path: Path) -> str:
    lines = patch_path.read_text(encoding="utf-8").splitlines(keepends=True)
    marker = f"+++ b/{_CONTRACT_PATH}\n"
    marker_index = lines.index(marker)
    hunk_index = next(
        index
        for index in range(marker_index + 1, len(lines))
        if lines[index].startswith("@@ -0,0 +1,")
    )
    source: list[str] = []
    for line in lines[hunk_index + 1 :]:
        if line.startswith("diff --git "):
            break
        if line.startswith("+") and not line.startswith("+++"):
            source.append(line[1:])
        elif line.startswith("\\ No newline at end of file"):
            continue
        else:
            raise AssertionError("pinned Core contract patch has an unexpected hunk")
    return "".join(source)


def _contract_diff(patch_path: Path) -> str | None:
    lines = patch_path.read_text(encoding="utf-8").splitlines(keepends=True)
    marker = f"diff --git a/{_CONTRACT_PATH} b/{_CONTRACT_PATH}\n"
    try:
        start = lines.index(marker)
    except ValueError:
        return None
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("diff --git ")
        ),
        len(lines),
    )
    return "".join(lines[start:end])


@lru_cache(maxsize=1)
def core_host_spi_source() -> str:
    patch_paths = _locked_patches()
    with tempfile.TemporaryDirectory(prefix="hermes-host-spi-contract-") as raw_target:
        target = Path(raw_target)
        contract = target / _CONTRACT_PATH
        contract.parent.mkdir(parents=True)
        contract.write_text(_stage1_source(patch_paths[0]), encoding="utf-8")
        for patch_path in patch_paths[1:]:
            diff = _contract_diff(patch_path)
            if diff is None:
                continue
            completed = subprocess.run(
                ("git", "apply", "--whitespace=nowarn", "-"),
                cwd=target,
                input=diff,
                capture_output=True,
                text=True,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
        result = contract.read_text(encoding="utf-8")
    assert "GATEWAY_EXTENSION_SPI_VERSION: Literal[1] = 1" in result
    assert "class ObserverRequest:" in result
    assert "class SessionCatalogRequest:" in result
    return result


def install_core_host_spi_contract() -> ModuleType:
    source = core_host_spi_source()
    module = ModuleType(_MODULE_NAME)
    module.__file__ = f"{_bundle_root()}/stage3:{_CONTRACT_PATH}"
    sys.modules[_MODULE_NAME] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def materialize_core_host_spi_contract(target: Path) -> Path:
    package = target / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    contract = package / "extension_host_v1.py"
    contract.write_text(core_host_spi_source(), encoding="utf-8")
    return target


__all__ = [
    "core_host_spi_source",
    "install_core_host_spi_contract",
    "materialize_core_host_spi_contract",
]
