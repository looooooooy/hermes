from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import get_type_hints

from hermes_connector.ports.instance_lock import InstanceLockPort
from hermes_connector.ports.local_gateway import (
    AgentDiscoveryPort,
    LocalGatewayTransportPort,
)

CONNECTOR_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = CONNECTOR_ROOT / "src" / "hermes_connector"
_PLATFORM_PROBES = frozenset(
    {
        ("os", "name"),
        ("platform", "system"),
        ("sys", "platform"),
    }
)
_PLATFORM_PROBE_MODULES = frozenset(module for module, _ in _PLATFORM_PROBES)
_PLATFORM_SELECTION_PATH = Path("bootstrap/platform.py")


def _platform_probe_violations(
    relative: Path,
    source: str,
) -> list[str]:
    tree = ast.parse(source, filename=str(relative))
    module_aliases: dict[str, str] = {}
    member_aliases: dict[str, tuple[str, str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name.split(".", 1)[0]
                if module_name not in _PLATFORM_PROBE_MODULES:
                    continue
                module_aliases[alias.asname or module_name] = module_name
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_name = node.module.split(".", 1)[0]
            for alias in node.names:
                probe = (module_name, alias.name)
                if probe in _PLATFORM_PROBES:
                    member_aliases[alias.asname or alias.name] = probe

    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module_name = module_aliases.get(node.value.id)
            if (module_name, node.attr) in _PLATFORM_PROBES:
                lines.add(node.lineno)
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in member_aliases
        ):
            lines.add(node.lineno)

    if relative == _PLATFORM_SELECTION_PATH:
        return []
    return [f"{relative}:{line}" for line in sorted(lines)]


class PlatformBoundaryTest(unittest.TestCase):
    def test_platform_adapter_types_use_explicit_port_factories(self) -> None:
        bootstrap = importlib.import_module("hermes_connector.bootstrap.platform")

        self.assertEqual(
            get_type_hints(bootstrap.PlatformAdapterTypes),
            {
                "platform_name": str,
                "agent_discovery_type": bootstrap.AgentDiscoveryFactory,
                "local_gateway_transport_type": (
                    bootstrap.LocalGatewayTransportFactory
                ),
                "instance_lock_type": bootstrap.InstanceLockFactory,
            },
        )
        self.assertIs(
            get_type_hints(bootstrap.AgentDiscoveryFactory.__call__)["return"],
            AgentDiscoveryPort,
        )
        self.assertIs(
            get_type_hints(bootstrap.LocalGatewayTransportFactory.__call__)["return"],
            LocalGatewayTransportPort,
        )
        self.assertIs(
            get_type_hints(bootstrap.InstanceLockFactory.__call__)["return"],
            InstanceLockPort,
        )

    def test_macos_exports_explicitly_named_platform_adapters(self) -> None:
        discovery = importlib.import_module(
            "hermes_connector.adapters.platform.macos.agent_discovery"
        )
        transport = importlib.import_module(
            "hermes_connector.adapters.platform.macos.local_gateway_transport"
        )
        instance_lock = importlib.import_module(
            "hermes_connector.adapters.platform.macos.instance_lock"
        )
        macos = importlib.import_module("hermes_connector.adapters.platform.macos")

        self.assertEqual(discovery.MacOSAgentDiscovery.__name__, "MacOSAgentDiscovery")
        self.assertEqual(
            transport.MacOSLocalGatewayTransport.__name__,
            "MacOSLocalGatewayTransport",
        )
        self.assertEqual(
            transport.MacOSLocalGatewayConnection.__name__,
            "MacOSLocalGatewayConnection",
        )
        self.assertEqual(instance_lock.MacOSInstanceLock.__name__, "MacOSInstanceLock")
        self.assertEqual(
            macos.MacOSKeychainSecretStore.__name__,
            "MacOSKeychainSecretStore",
        )
        self.assertEqual(
            macos.MacOSKeychainDeviceIdentity.__name__,
            "MacOSKeychainDeviceIdentity",
        )
        self.assertEqual(
            macos.MacOSKeychainCloudTokenProvider.__name__,
            "MacOSKeychainCloudTokenProvider",
        )

    def test_linux_remains_unavailable_with_no_capabilities(self) -> None:
        boundary = importlib.import_module(
            "hermes_connector.adapters.platform.availability"
        )
        linux = importlib.import_module(
            "hermes_connector.adapters.platform.linux.availability"
        )

        self.assertFalse(linux.AVAILABILITY.available)
        self.assertEqual(linux.AVAILABILITY.capabilities, frozenset())
        with self.assertRaises(boundary.PlatformUnavailable):
            linux.AVAILABILITY.require_available()

    def test_windows_partial_foundation_is_declared_but_remains_unavailable(self) -> None:
        boundary = importlib.import_module(
            "hermes_connector.adapters.platform.availability"
        )
        windows = importlib.import_module(
            "hermes_connector.adapters.platform.windows.availability"
        )

        self.assertFalse(windows.AVAILABILITY.available)
        self.assertEqual(
            windows.AVAILABILITY.capabilities,
            frozenset(
                {
                    "instance_lock",
                    "local_gateway.discovery",
                    "local_gateway.handshake",
                }
            ),
        )
        with self.assertRaises(boundary.PlatformUnavailable):
            windows.AVAILABILITY.require_available()

    def test_bootstrap_selects_macos_and_rejects_unimplemented_platforms(
        self,
    ) -> None:
        bootstrap = importlib.import_module("hermes_connector.bootstrap.platform")
        boundary = importlib.import_module(
            "hermes_connector.adapters.platform.availability"
        )

        selected = bootstrap.select_platform_adapters("darwin")

        self.assertEqual(selected.platform_name, "macos")
        self.assertEqual(
            selected.agent_discovery_type.__name__,
            "MacOSAgentDiscovery",
        )
        self.assertEqual(
            selected.local_gateway_transport_type.__name__,
            "MacOSLocalGatewayTransport",
        )
        self.assertEqual(
            selected.instance_lock_type.__name__,
            "MacOSInstanceLock",
        )
        for platform_name in ("linux", "win32"):
            with (
                self.subTest(platform=platform_name),
                self.assertRaises(boundary.PlatformUnavailable),
            ):
                bootstrap.select_platform_adapters(platform_name)

    def test_unimplemented_platform_rejection_does_not_import_macos_package(
        self,
    ) -> None:
        script = """
import sys

from hermes_connector.adapters.platform.availability import PlatformUnavailable
from hermes_connector.bootstrap.platform import select_platform_adapters

try:
    select_platform_adapters(sys.argv[1])
except PlatformUnavailable:
    pass
else:
    raise AssertionError("unimplemented platform did not fail closed")

macos_prefix = "hermes_connector.adapters.platform.macos"
if any(name == macos_prefix or name.startswith(macos_prefix + ".") for name in sys.modules):
    raise AssertionError("macOS package imported for unimplemented platform")
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(CONNECTOR_ROOT / "src")

        for platform_name in ("linux", "win32", "cygwin"):
            with self.subTest(platform=platform_name):
                completed = subprocess.run(
                    [sys.executable, "-c", script, platform_name],
                    check=False,
                    capture_output=True,
                    env=environment,
                    text=True,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_platform_probe_fitness_check_resolves_aliases_and_exact_path(
        self,
    ) -> None:
        forbidden_sources = (
            "import sys as host\nselected = host.platform\n",
            "from sys import platform as host_platform\nselected = host_platform\n",
            "import os as host\nselected = host.name\n",
            "from os import name\nselected = name\n",
            "from os import name as host_name\nselected = host_name\n",
            "import platform as host\nselected = host.system()\n",
            "from platform import system as host_system\nselected = host_system()\n",
        )
        for relative in (
            Path("application/platform_probe.py"),
            Path("bootstrap/runtime.py"),
        ):
            for source in forbidden_sources:
                with self.subTest(path=relative, source=source):
                    self.assertEqual(
                        _platform_probe_violations(relative, source),
                        [f"{relative}:2"],
                    )

        allowed_source = "\n".join(forbidden_sources)
        self.assertEqual(
            _platform_probe_violations(
                Path("bootstrap/platform.py"),
                allowed_source,
            ),
            [],
        )

    def test_runtime_platform_probes_exist_only_in_bootstrap(self) -> None:
        violations: list[str] = []
        for path in SOURCE_ROOT.rglob("*.py"):
            relative = path.relative_to(SOURCE_ROOT)
            violations.extend(
                _platform_probe_violations(
                    relative,
                    path.read_text(encoding="utf-8"),
                )
            )

        self.assertEqual(violations, [])

    def test_domain_application_and_ports_do_not_import_os_modules(self) -> None:
        forbidden = {"fcntl", "os", "platform", "socket", "sys"}
        violations: list[str] = []
        for layer in ("domain", "application", "ports"):
            for path in (SOURCE_ROOT / layer).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    names: set[str] = set()
                    if isinstance(node, ast.Import):
                        names = {alias.name.split(".", 1)[0] for alias in node.names}
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = {node.module.split(".", 1)[0]}
                    for name in names & forbidden:
                        violations.append(
                            f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}:{name}"
                        )

        self.assertEqual(violations, [])

    def test_legacy_posix_modules_are_definition_free_compatibility_layers(
        self,
    ) -> None:
        for module_name in (
            "posix_agent_discovery.py",
            "posix_local_gateway_transport.py",
            "posix_lock.py",
        ):
            with self.subTest(module=module_name):
                path = SOURCE_ROOT / "adapters" / module_name
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                definitions = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(
                        node,
                        (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                ]
                self.assertEqual(definitions, [])

    def test_legacy_posix_names_alias_the_macos_implementations(self) -> None:
        legacy_discovery = importlib.import_module(
            "hermes_connector.adapters.posix_agent_discovery"
        )
        macos_discovery = importlib.import_module(
            "hermes_connector.adapters.platform.macos.agent_discovery"
        )
        legacy_transport = importlib.import_module(
            "hermes_connector.adapters.posix_local_gateway_transport"
        )
        macos_transport = importlib.import_module(
            "hermes_connector.adapters.platform.macos.local_gateway_transport"
        )
        legacy_lock = importlib.import_module("hermes_connector.adapters.posix_lock")
        macos_lock = importlib.import_module(
            "hermes_connector.adapters.platform.macos.instance_lock"
        )

        self.assertIs(
            legacy_discovery.PosixAgentDiscovery,
            macos_discovery.MacOSAgentDiscovery,
        )
        self.assertIs(
            legacy_transport.PosixLocalGatewayTransport,
            macos_transport.MacOSLocalGatewayTransport,
        )
        self.assertIs(
            legacy_transport.PosixLocalGatewayConnection,
            macos_transport.MacOSLocalGatewayConnection,
        )
        self.assertIs(
            legacy_lock.PosixInstanceLock,
            macos_lock.MacOSInstanceLock,
        )

    def test_platform_packages_and_public_exports_are_exact(self) -> None:
        linux_path = SOURCE_ROOT / "adapters" / "platform" / "linux"
        self.assertEqual(
            sorted(path.name for path in linux_path.glob("*.py")),
            ["__init__.py", "availability.py"],
        )
        linux = importlib.import_module("hermes_connector.adapters.platform.linux")
        self.assertEqual(linux.__all__, ["AVAILABILITY"])

        windows_path = SOURCE_ROOT / "adapters" / "platform" / "windows"
        self.assertEqual(
            sorted(path.name for path in windows_path.glob("*.py")),
            [
                "__init__.py",
                "agent_discovery.py",
                "availability.py",
                "control_client.py",
                "dpapi_secret_store.py",
                "duplex_pipe.py",
                "instance_identity.py",
                "instance_lock.py",
                "local_gateway_transport.py",
                "named_pipe.py",
                "observer_client.py",
                "pairing_command_lock.py",
                "pairing_projection.py",
                "plugin_control_relay.py",
                "private_state.py",
                "process_identity.py",
                "session_catalog_client.py",
                "status_receipt.py",
            ],
        )
        windows = importlib.import_module("hermes_connector.adapters.platform.windows")
        self.assertEqual(windows.__all__, ["AVAILABILITY"])

    def test_legacy_shim_and_top_level_adapter_exports_are_exact(self) -> None:
        expected = {
            "hermes_connector.adapters.posix_agent_discovery": [
                "DEFAULT_MAX_CANDIDATES",
                "MAX_DESCRIPTOR_BYTES",
                "PosixAgentDiscovery",
            ],
            "hermes_connector.adapters.posix_local_gateway_transport": [
                "MAX_LOCAL_BODY_BYTES",
                "PosixLocalGatewayConnection",
                "PosixLocalGatewayTransport",
            ],
            "hermes_connector.adapters.posix_lock": [
                "AlreadyRunning",
                "InstanceLockError",
                "MetadataValidator",
                "PosixInstanceLock",
                "UnsafeLockFile",
            ],
            "hermes_connector.adapters": [
                "AlreadyRunning",
                "MacOSInstanceLock",
                "PosixInstanceLock",
                "SQLiteStorageComponent",
                "UnsafeLockFile",
                "decode_cloud_envelope",
                "decode_local_hello",
                "decode_local_welcome",
            ],
        }

        for module_name, exports in expected.items():
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertEqual(module.__all__, exports)


if __name__ == "__main__":
    unittest.main()
