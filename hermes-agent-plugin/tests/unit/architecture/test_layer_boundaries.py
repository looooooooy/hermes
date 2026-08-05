"""Architecture fitness tests for the migrated lifecycle slice."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[3] / "src" / "hermes_agent_plugin"
PLUGIN_ROOT = Path(__file__).parents[3]
REPOSITORY_ROOT = PLUGIN_ROOT.parent
RETIRED_IMPORT_SEGMENTS = ("hermes", "mobile", "gateway")
RETIRED_IMPORT_PACKAGE = "_".join(RETIRED_IMPORT_SEGMENTS)


@pytest.mark.parametrize("layer", ("domain", "application", "ports"))
def test_core_layers_do_not_import_operating_system_apis(layer: str) -> None:
    forbidden_roots = {
        "os",
        "pathlib",
        "platform",
        "socket",
        "subprocess",
        "sys",
        "tempfile",
    }
    violations: list[str] = []

    for path in sorted((SOURCE_ROOT / layer).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.partition(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {str(node.module).partition(".")[0]}
            else:
                continue
            if roots & forbidden_roots:
                violations.append(f"{path.name}:{sorted(roots)}")

    assert violations == []


def test_common_local_protocol_relays_do_not_import_platform_io() -> None:
    forbidden_roots = {
        "concurrent",
        "os",
        "pathlib",
        "platform",
        "queue",
        "socket",
        "stat",
        "subprocess",
        "sys",
        "tempfile",
        "threading",
        "websockets",
    }
    violations: list[str] = []

    for module_name in ("control_relay.py", "observer_relay.py"):
        path = SOURCE_ROOT / "adapters" / "local_protocol" / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [str(node.module)]
            else:
                continue
            for module in modules:
                root = module.partition(".")[0]
                if root in forbidden_roots or "platform.macos" in module:
                    violations.append(f"{module_name}:{module}")

    assert violations == []


def test_platform_relay_backends_are_isolated_and_fail_closed() -> None:
    platform_root = SOURCE_ROOT / "adapters" / "platform"

    assert (platform_root / "macos" / "control_relay.py").is_file()
    assert (platform_root / "macos" / "observer_relay.py").is_file()
    for platform_name in ("linux", "windows"):
        source = (platform_root / platform_name / "local_relay.py").read_text(
            encoding="utf-8"
        )
        assert "PlatformLocalGatewayUnavailable" in source
        assert "unix_serve" not in source
        assert "unix_connect" not in source


def test_platform_selection_occurs_only_in_bootstrap() -> None:
    selectors: list[Path] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "sys.platform" in path.read_text(encoding="utf-8"):
            selectors.append(path.relative_to(SOURCE_ROOT))

    assert selectors == [
        Path("bootstrap/platform_adapters.py"),
    ]


def test_behavior_tests_mirror_runtime_boundaries() -> None:
    tests_root = Path(__file__).parents[3] / "tests"
    expected_files = (
        "integration/bootstrap/test_runtime_lifecycle.py",
        "integration/bootstrap/test_runtime_failure_cleanup.py",
        "integration/bootstrap/test_relay_lifecycle_cleanup.py",
        "platform/macos/test_macos_local_gateway_protocol.py",
        "platform/macos/test_macos_local_gateway_lifecycle.py",
        "platform/macos/test_macos_local_trust_directories.py",
        "platform/macos/test_macos_local_trust_registry.py",
        "platform/macos/test_macos_local_trust_sockets.py",
        "platform/macos/test_macos_relay_trust_boundary.py",
        "platform/linux/test_linux_availability.py",
        "platform/windows/test_windows_availability.py",
    )

    assert all((tests_root / relative).is_file() for relative in expected_files)


def test_control_runtime_exists_in_protocol_and_macos_platform_layers() -> None:
    expected = {
        Path("application/control_commands.py"),
        Path("domain/control_lease.py"),
        Path("adapters/local_protocol/control_relay.py"),
        Path("adapters/local_protocol/observer_relay.py"),
        Path("adapters/platform/macos/control_relay.py"),
        Path("adapters/platform/macos/observer_relay.py"),
    }

    assert {
        relative for relative in expected if (SOURCE_ROOT / relative).is_file()
    } == expected


def test_source_tree_contains_only_the_canonical_plugin_package() -> None:
    source_root = SOURCE_ROOT.parent
    packages = {
        path.name
        for path in source_root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }

    assert packages == {"hermes_agent_plugin"}


def test_canonical_package_never_imports_legacy_package() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [str(node.module)]
            else:
                continue
            if any(module.startswith(RETIRED_IMPORT_PACKAGE) for module in modules):
                violations.append(str(path.relative_to(SOURCE_ROOT)))

    assert violations == []


def test_contract_sync_uses_only_the_canonical_plugin_generated_copy() -> None:
    sync_source = (REPOSITORY_ROOT / "contracts/tools/sync_consumers.py").read_text(
        encoding="utf-8"
    )
    assert "hermes_agent_plugin/contracts/generated" in sync_source
    assert f"{RETIRED_IMPORT_PACKAGE}/contracts" not in sync_source


def test_plugin_docs_describe_canonical_control_runtime_as_current() -> None:
    detailed_design = (
        PLUGIN_ROOT / "docs/09-agent-plugin-detailed-design.md"
    ).read_text(encoding="utf-8")
    roadmap = (PLUGIN_ROOT / "docs/07-delivery-roadmap-and-acceptance.md").read_text(
        encoding="utf-8"
    )

    for canonical_path in (
        "src/hermes_agent_plugin/application/control_commands.py",
        "src/hermes_agent_plugin/domain/control_lease.py",
        "src/hermes_agent_plugin/adapters/local_protocol/control_relay.py",
        "src/hermes_agent_plugin/adapters/local_protocol/observer_relay.py",
    ):
        assert canonical_path in detailed_design
        assert canonical_path in roadmap
    for platform_path in (
        "src/hermes_agent_plugin/adapters/platform/macos/control_relay.py",
        "src/hermes_agent_plugin/adapters/platform/macos/observer_relay.py",
    ):
        assert platform_path in detailed_design
        assert platform_path in roadmap
    assert "待迁移旧实现" not in detailed_design
    assert "待迁移旧实现" not in roadmap
