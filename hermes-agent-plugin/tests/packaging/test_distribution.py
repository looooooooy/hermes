"""Isolated wheel metadata and content tests."""

from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import textwrap
import tomllib
import venv
import zipfile
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SOURCE_ROOT = PLUGIN_ROOT / "src"
PACKAGE_NAMES = ("hermes_agent_plugin",)
RETIRED_IMPORT_SEGMENTS = ("hermes", "mobile", "gateway")
RETIRED_IMPORT_PACKAGE = "_".join(RETIRED_IMPORT_SEGMENTS)
CANONICAL_VERSION = tomllib.loads(
    (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
CANONICAL_ARTIFACT_NAMES = {
    f"hermes_agent_plugin-{CANONICAL_VERSION}-py3-none-any.whl",
    f"hermes_agent_plugin-{CANONICAL_VERSION}.tar.gz",
}
PLATFORM_BOUNDARY_MODULES = {
    "hermes_agent_plugin/ports/local_relay.py",
    "hermes_agent_plugin/bootstrap/platform_adapters.py",
    "hermes_agent_plugin/adapters/platform/macos/local_relay.py",
    "hermes_agent_plugin/adapters/platform/macos/control_relay.py",
    "hermes_agent_plugin/adapters/platform/macos/observer_relay.py",
    "hermes_agent_plugin/adapters/platform/linux/local_relay.py",
    "hermes_agent_plugin/adapters/platform/windows/local_relay.py",
}
TAMPER_TARGET = "hermes_agent_plugin/ports/local_relay.py"
CONTROLLED_WHEEL_TAMPER_TARGETS = (
    "hermes_agent_plugin/contracts/generated/mobile-control-v1.json",
    f"hermes_agent_plugin-{CANONICAL_VERSION}.dist-info/METADATA",
    f"hermes_agent_plugin-{CANONICAL_VERSION}.dist-info/entry_points.txt",
)


def _python_path(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts/python.exe"
    return environment / "bin/python"


def test_repository_dist_contains_no_legacy_distribution_artifacts() -> None:
    dist_directory = PLUGIN_ROOT / "dist"
    legacy_artifacts = (
        sorted(path.name for path in dist_directory.glob(f"{RETIRED_IMPORT_PACKAGE}-*"))
        if dist_directory.is_dir()
        else []
    )

    assert legacy_artifacts == []


def _is_package_python_path(path: str) -> bool:
    parts = Path(path).parts
    return (
        path.endswith(".py")
        and "__pycache__" not in parts
        and parts
        and parts[0] in PACKAGE_NAMES
    )


def _current_package_sources() -> dict[str, bytes]:
    return {
        source.relative_to(PACKAGE_SOURCE_ROOT).as_posix(): source.read_bytes()
        for package_name in PACKAGE_NAMES
        for source in sorted((PACKAGE_SOURCE_ROOT / package_name).rglob("*.py"))
        if "__pycache__" not in source.parts
    }


def _record_package_source(
    package_sources: dict[str, bytes],
    *,
    path: str,
    content: bytes,
    artifact: Path,
) -> None:
    assert path not in package_sources, (
        f"{artifact.name} contains duplicate package module {path}"
    )
    package_sources[path] = content


def _wheel_package_sources(artifact: Path) -> dict[str, bytes]:
    package_sources: dict[str, bytes] = {}
    with zipfile.ZipFile(artifact) as wheel:
        for path in wheel.namelist():
            if _is_package_python_path(path):
                _record_package_source(
                    package_sources,
                    path=path,
                    content=wheel.read(path),
                    artifact=artifact,
                )
    return package_sources


def _wheel_files(artifact: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(artifact) as wheel:
        for path in wheel.namelist():
            if path.endswith("/"):
                continue
            assert path not in files, f"{artifact.name} contains duplicate file {path}"
            files[path] = wheel.read(path)
    return files


def _sdist_package_sources(artifact: Path) -> dict[str, bytes]:
    package_sources: dict[str, bytes] = {}
    with tarfile.open(artifact, "r:gz") as sdist:
        for member in sdist.getmembers():
            prefix, separator, path = member.name.partition("/src/")
            if (
                not prefix
                or not separator
                or not member.isfile()
                or not _is_package_python_path(path)
            ):
                continue
            extracted = sdist.extractfile(member)
            assert extracted is not None
            _record_package_source(
                package_sources,
                path=path,
                content=extracted.read(),
                artifact=artifact,
            )
    return package_sources


def _sdist_files(artifact: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(artifact, "r:gz") as sdist:
        for member in sdist.getmembers():
            root, separator, path = member.name.partition("/")
            assert member.isfile() or member.isdir(), (
                f"{artifact.name} contains unsupported archive entry {member.name}"
            )
            if not root or not separator or not member.isfile():
                continue
            extracted = sdist.extractfile(member)
            assert extracted is not None
            assert path not in files, f"{artifact.name} contains duplicate file {path}"
            files[path] = extracted.read()
    return files


def _assert_complete_artifact_matches(
    actual: dict[str, bytes],
    expected: dict[str, bytes],
    *,
    artifact: Path,
) -> None:
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    changed = sorted(
        path
        for path in actual.keys() & expected.keys()
        if actual[path] != expected[path]
    )
    assert (missing, unexpected, changed) == ([], [], []), (
        f"{artifact.name} complete controlled content differs from fresh build: "
        f"missing={missing}, unexpected={unexpected}, changed={changed}"
    )


def _assert_wheel_record_is_complete(
    wheel_files: dict[str, bytes],
    *,
    artifact: Path,
) -> None:
    record_paths = [path for path in wheel_files if path.endswith(".dist-info/RECORD")]
    assert len(record_paths) == 1
    record_path = record_paths[0]
    rows = list(csv.reader(io.StringIO(wheel_files[record_path].decode())))
    assert all(len(row) == 3 for row in rows)
    assert len(rows) == len(wheel_files)
    assert {row[0] for row in rows} == set(wheel_files)

    for path, digest, size in rows:
        if path == record_path:
            assert (digest, size) == ("", "")
            continue
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(wheel_files[path]).digest()
        ).rstrip(b"=")
        assert digest == f"sha256={expected_digest.decode()}", (
            f"{artifact.name} RECORD digest mismatch for {path}"
        )
        assert size == str(len(wheel_files[path])), (
            f"{artifact.name} RECORD size mismatch for {path}"
        )


def _assert_platform_boundary(
    package_sources: dict[str, bytes],
    *,
    artifact: Path,
) -> None:
    missing_boundary_modules = sorted(
        PLATFORM_BOUNDARY_MODULES - package_sources.keys()
    )
    assert missing_boundary_modules == [], (
        f"{artifact.name} is missing platform boundary modules: "
        f"{missing_boundary_modules}"
    )

    for platform_name in ("linux", "windows"):
        path = f"hermes_agent_plugin/adapters/platform/{platform_name}/local_relay.py"
        source = package_sources[path].decode()
        assert "UnavailableLocalRelayBackend" in source, path
        assert f"{platform_name}_local_relay_not_implemented" in source, path

    common_prefix = "hermes_agent_plugin/adapters/local_protocol/"
    for path, content in package_sources.items():
        if not path.startswith(common_prefix):
            continue
        tree = ast.parse(content, filename=path)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        imported_modules.update(
            f"{node.module}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        )
        forbidden_imports = sorted(
            module
            for module in imported_modules
            if module.split(".", 1)[0] in {"pathlib", "socket", "threading"}
            or module.endswith("platform.macos.local_trust")
        )
        assert forbidden_imports == [], (
            f"{artifact.name}:{path} contains platform implementation imports: "
            f"{forbidden_imports}"
        )


def _assert_package_sources_match(
    package_sources: dict[str, bytes],
    *,
    artifact: Path,
) -> None:
    current_sources = _current_package_sources()
    missing = sorted(current_sources.keys() - package_sources.keys())
    unexpected = sorted(package_sources.keys() - current_sources.keys())
    changed = sorted(
        path
        for path in current_sources.keys() & package_sources.keys()
        if current_sources[path] != package_sources[path]
    )
    assert (missing, unexpected, changed) == ([], [], []), (
        f"{artifact.name} package sources differ from current build input: "
        f"missing={missing}, unexpected={unexpected}, changed={changed}"
    )
    _assert_platform_boundary(package_sources, artifact=artifact)


def test_repository_dist_matches_current_package_sources(
    canonical_wheel: Path,
    canonical_sdist: Path,
) -> None:
    dist_directory = PLUGIN_ROOT / "dist"
    artifact_names = {
        path.name for path in dist_directory.iterdir() if path.name != ".gitignore"
    }
    assert artifact_names == CANONICAL_ARTIFACT_NAMES

    wheel = dist_directory / (
        f"hermes_agent_plugin-{CANONICAL_VERSION}-py3-none-any.whl"
    )
    sdist = dist_directory / (f"hermes_agent_plugin-{CANONICAL_VERSION}.tar.gz")
    _assert_package_sources_match(
        _wheel_package_sources(wheel),
        artifact=wheel,
    )
    _assert_package_sources_match(
        _sdist_package_sources(sdist),
        artifact=sdist,
    )
    repository_wheel_files = _wheel_files(wheel)
    _assert_complete_artifact_matches(
        repository_wheel_files,
        _wheel_files(canonical_wheel),
        artifact=wheel,
    )
    _assert_wheel_record_is_complete(
        repository_wheel_files,
        artifact=wheel,
    )
    _assert_complete_artifact_matches(
        _sdist_files(sdist),
        _sdist_files(canonical_sdist),
        artifact=sdist,
    )


def _tamper_wheel(
    source: Path,
    target: Path,
    *,
    member_path: str = TAMPER_TARGET,
) -> None:
    with zipfile.ZipFile(source) as wheel:
        files = {path: wheel.read(path) for path in wheel.namelist()}
    files[member_path] += b"\n# tampered\n"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as wheel:
        for path, content in files.items():
            wheel.writestr(path, content)


def _tamper_sdist(source: Path, target: Path) -> None:
    with (
        tarfile.open(source, "r:gz") as input_archive,
        tarfile.open(target, "w:gz") as output_archive,
    ):
        for member in input_archive.getmembers():
            extracted = input_archive.extractfile(member) if member.isfile() else None
            content = extracted.read() if extracted is not None else None
            if member.name.endswith(f"/src/{TAMPER_TARGET}"):
                assert content is not None
                content += b"\n# tampered\n"
                member.size = len(content)
            output_archive.addfile(
                member,
                io.BytesIO(content) if content is not None else None,
            )


def test_package_source_gate_rejects_tampered_wheel(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    tampered = tmp_path / canonical_wheel.name
    _tamper_wheel(canonical_wheel, tampered)

    with pytest.raises(AssertionError, match=r"changed=.*ports/local_relay.py"):
        _assert_package_sources_match(
            _wheel_package_sources(tampered),
            artifact=tampered,
        )


def test_package_source_gate_rejects_tampered_sdist(
    tmp_path: Path,
    canonical_sdist: Path,
) -> None:
    tampered = tmp_path / canonical_sdist.name
    _tamper_sdist(canonical_sdist, tampered)

    with pytest.raises(AssertionError, match=r"changed=.*ports/local_relay.py"):
        _assert_package_sources_match(
            _sdist_package_sources(tampered),
            artifact=tampered,
        )


@pytest.mark.parametrize(
    "member_path",
    CONTROLLED_WHEEL_TAMPER_TARGETS,
    ids=("generated-contract", "metadata", "entry-points"),
)
def test_complete_wheel_gate_rejects_controlled_content_tampering(
    tmp_path: Path,
    canonical_wheel: Path,
    member_path: str,
) -> None:
    tampered = tmp_path / canonical_wheel.name
    _tamper_wheel(
        canonical_wheel,
        tampered,
        member_path=member_path,
    )

    with pytest.raises(
        AssertionError,
        match=rf"changed=.*{Path(member_path).name}",
    ):
        _assert_complete_artifact_matches(
            _wheel_files(tampered),
            _wheel_files(canonical_wheel),
            artifact=tampered,
        )
    with pytest.raises(AssertionError, match="RECORD digest mismatch"):
        _assert_wheel_record_is_complete(
            _wheel_files(tampered),
            artifact=tampered,
        )


def _install_wheels(
    environment: Path,
    wheels: tuple[Path, ...],
) -> Path:
    venv.EnvBuilder(
        with_pip=True,
        symlinks=sys.platform != "win32",
    ).create(environment)
    python = _python_path(environment)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            *(str(wheel) for wheel in wheels),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return python


def test_built_wheel_has_only_owned_canonical_target_entry_point(
    tmp_path: Path,
    canonical_wheel: Path,
    runtime_dependency_wheels: tuple[Path, ...],
) -> None:
    python = _install_wheels(
        tmp_path / "wheel-environment",
        (*runtime_dependency_wheels, canonical_wheel),
    )
    inspection = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m, json;"
                "import hermes_agent_plugin;"
                "eps=[{'name':ep.name,'value':ep.value,"
                "'distribution':ep.dist.metadata['Name'],"
                "'version':ep.dist.version} for ep in m.entry_points("
                "group='hermes_agent.plugins')];"
                "print(json.dumps({"
                "'canonical_version':m.version('hermes-agent-plugin'),"
                "'entry_points':eps"
                "}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(inspection.stdout)
    assert result == {
        "canonical_version": "0.1.0",
        "entry_points": [
            {
                "name": "hermes-agent-plugin",
                "value": "hermes_agent_plugin",
                "distribution": "hermes-agent-plugin",
                "version": "0.1.0",
            }
        ],
    }
    subprocess.run(
        [str(python), "-m", "pip", "check"],
        check=True,
        capture_output=True,
        text=True,
    )


def test_built_wheel_contains_packages_but_not_self_upgrade_tooling(
    canonical_wheel: Path,
) -> None:
    with zipfile.ZipFile(canonical_wheel) as wheel:
        entries = set(wheel.namelist())
        entry_points = wheel.read(
            "hermes_agent_plugin-0.1.0.dist-info/entry_points.txt"
        ).decode()
        metadata = wheel.read("hermes_agent_plugin-0.1.0.dist-info/METADATA").decode()

    assert {
        "hermes_agent_plugin/bootstrap/registration.py",
        "hermes_agent_plugin/adapters/host/extension.py",
        "hermes_agent_plugin/adapters/local_protocol/control_v1.py",
        "hermes_agent_plugin/adapters/local_protocol/control_relay.py",
        "hermes_agent_plugin/adapters/local_protocol/observer_relay.py",
        "hermes_agent_plugin/adapters/platform/macos/control_relay.py",
        "hermes_agent_plugin/adapters/platform/macos/observer_relay.py",
        "hermes_agent_plugin/application/control_commands.py",
        "hermes_agent_plugin/domain/control_lease.py",
        ("hermes_agent_plugin/contracts/generated/mobile-control-v1.json"),
        "hermes_agent_plugin/contracts/generated/observer-output-parity-v2.json",
        (
            "hermes_agent_plugin/contracts/generated/schemas/cloud/payloads/"
            "session-event-v2.schema.json"
        ),
        (
            "hermes_agent_plugin/contracts/generated/schemas/cloud/payloads/"
            "session-snapshot-v2.schema.json"
        ),
    }.issubset(entries)
    assert not any(entry.startswith(f"{RETIRED_IMPORT_PACKAGE}/") for entry in entries)
    assert not any(entry.startswith("packaging/common/") for entry in entries)
    assert not any(entry.startswith("tests/") for entry in entries)
    assert not any("test_support" in entry for entry in entries)
    assert entry_points == (
        "[hermes_agent.plugins]\nhermes-agent-plugin = hermes_agent_plugin\n"
    )
    assert "Name: hermes-agent-plugin\n" in metadata
    assert "Author: Hermes contributors\n" in metadata
    assert "mobile" not in metadata.lower()


def test_fresh_noneditable_wheel_loads_exact_observer_v2_resource_bundle(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    python = _install_wheels(tmp_path / "isolated-observer-v2", (canonical_wheel,))
    inspection = subprocess.run(
        [
            str(python),
            "-c",
            textwrap.dedent(
                """
                from types import SimpleNamespace

                from hermes_agent_plugin.adapters.host.extension import (
                    HermesAgentPluginExtension,
                )
                from hermes_agent_plugin.adapters.host.spi_v1 import HostSpiFactories
                from hermes_agent_plugin.adapters.host.observer_v2 import (
                    OUTPUT_PARITY_CAPABILITY,
                    load_observer_v2_bundle,
                )

                class Registration:
                    def close(self):
                        return None

                def dto(**values):
                    return SimpleNamespace(**values)

                test_host_spi_factories = HostSpiFactories(
                    observer_request=dto,
                    control_scope=dto,
                    owner_action_request=dto,
                    safe_audit_event=dto,
                    session_catalog_request=dto,
                )

                class Prepared:
                    activation_deadline_monotonic = 100.0
                    snapshot = {
                        "observer_contract": 2,
                        "profile": "default",
                        "runtime_generation": "generation-1",
                        "session_key": "durable-1",
                        "runtime_session_id": "runtime-1",
                        "running": True,
                        "status": "running",
                        "event_sequence": 0,
                        "snapshot_event_sequence": 0,
                        "messages": [],
                        "inflight": {
                            "user": None, "assistant": None,
                            "streaming": False, "error": None,
                        },
                        "todo_sections": [], "subagents": [],
                        "tools": [], "terminals": [], "replay_events": [],
                    }
                    def activate(self):
                        return Registration()
                    def close(self):
                        return None

                class Host:
                    host_api_version = 1
                    def __init__(self):
                        self.endpoints = {}
                    def runtime_descriptor(self):
                        return SimpleNamespace(
                            profile="default", runtime_generation="generation-1",
                            state="ready", capabilities=frozenset({
                                "session.observe", "session.control",
                                OUTPUT_PARITY_CAPABILITY,
                            }),
                        )
                    def add_runtime_listener(self, listener):
                        return Registration()
                    def register_local_endpoint(self, endpoint):
                        self.endpoints[endpoint.connection_role] = endpoint
                        return Registration()
                    def prepare_observer(self, request, sink):
                        self.request = request
                        self.sink = sink
                        return Prepared()
                    def control_snapshot(self, scope):
                        return SimpleNamespace(control_revision=0)
                    def invoke_owner_action(self, request):
                        return SimpleNamespace(status="accepted", payload={})
                    def audit(self, event):
                        return None

                class Sink:
                    def __init__(self):
                        self.events = []
                    def on_event(self, event):
                        self.events.append(event)

                bundle = load_observer_v2_bundle()
                host = Host()
                installed = HermesAgentPluginExtension(
                    host_spi_factories=test_host_spi_factories,
                ).install(host)
                sink = Sink()
                prepared = host.endpoints["observer"].prepare_observer({
                    "observer_contract": 2,
                    "profile": "default",
                    "runtime_generation": "generation-1",
                    "session_key": "durable-1",
                }, sink)
                subscription = prepared.activate()
                host.sink.on_event({
                    "observer_contract": 2,
                    "profile": "default",
                    "runtime_generation": "generation-1",
                    "session_key": "durable-1",
                    "session_id": "runtime-1",
                    "type": "status.update",
                    "event_sequence": 1,
                    "payload": {"status": "running", "running": True},
                })
                assert host.request.observer_contract == 2
                assert sink.events[0]["event_sequence"] == 1
                subscription.close()
                installed.close()
                print(bundle.capability)
                """
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert inspection.returncode == 0, inspection.stderr
    assert inspection.stdout.strip() == "session.observe.output-parity.v1"


def test_built_sdist_contains_observer_v2_policy_and_schema_resources(
    canonical_sdist: Path,
) -> None:
    with tarfile.open(canonical_sdist, "r:gz") as archive:
        entries = set(archive.getnames())

    required_suffixes = {
        "/src/hermes_agent_plugin/contracts/generated/observer-output-parity-v2.json",
        (
            "/src/hermes_agent_plugin/contracts/generated/schemas/cloud/payloads/"
            "session-event-v2.schema.json"
        ),
        (
            "/src/hermes_agent_plugin/contracts/generated/schemas/cloud/payloads/"
            "session-snapshot-v2.schema.json"
        ),
    }
    assert all(
        any(entry.endswith(suffix) for entry in entries) for suffix in required_suffixes
    )


def test_fresh_noneditable_wheel_runs_full_future_host_manager_lifecycle(
    tmp_path: Path,
    canonical_wheel: Path,
) -> None:
    python = _install_wheels(
        tmp_path / "isolated-future-host",
        (canonical_wheel,),
    )
    hermes_home = tmp_path / "isolated-hermes-home"
    runtime_root = Path("/tmp").resolve(strict=True) / f"hap-wheel-{os.getpid()}"
    environment = {
        "HERMES_HOME": str(hermes_home),
        "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": str(runtime_root / "local-registry"),
        "HERMES_LOCAL_GATEWAY_SOCKET_DIR": str(runtime_root / "local-sockets"),
        "HERMES_CONTROL_REGISTRY_DIR": str(runtime_root / "control-registry"),
        "HERMES_CONTROL_SOCKET_DIR": str(runtime_root / "control-sockets"),
        "HERMES_OBSERVER_REGISTRY_DIR": str(runtime_root / "observer-registry"),
        "HERMES_OBSERVER_SOCKET_DIR": str(runtime_root / "observer-sockets"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = textwrap.dedent(
        """
        import importlib
        import importlib.metadata as metadata
        import json
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        CAPABILITIES = frozenset({
            "session.observe", "session.control", "prompt.submit",
            "session.interrupt", "session.steer", "approval.respond",
            "clarify.respond",
        })
        SPI_CAPABILITIES = frozenset({
            "audit.safe.v1", "extension.lifecycle.v1",
            "runtime.descriptor.v1", "session.observe.v1",
            "session.owner-actions.v1",
        })

        class Registration:
            def __init__(self, label, events):
                self.label = label
                self.events = events
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                self.events.append(f"close:{self.label}")

        class Prepared:
            snapshot = SimpleNamespace(event_sequence=0)
            activation_deadline_monotonic = 100.0

            def __init__(self, events):
                self.events = events
                self.close_calls = 0
                self.subscription = None

            def activate(self):
                self.events.append("observer:activate")
                self.subscription = Registration("observer-subscription", self.events)
                return self.subscription

            def close(self):
                self.close_calls += 1
                self.events.append("close:prepared")

        class Host:
            host_api_version = 1

            def __init__(self, label, *, fail_role=None):
                self.label = label
                self.fail_role = fail_role
                self.events = []
                self.registrations = {}
                self.endpoints = {}
                self.prepared = []
                self.generation = "generation-1"

            def descriptor(self):
                return SimpleNamespace(
                    profile="default",
                    runtime_generation=self.generation,
                    state="ready",
                    capabilities=CAPABILITIES,
                )

            def runtime_descriptor(self):
                self.events.append("runtime:descriptor")
                return self.descriptor()

            def add_runtime_listener(self, listener):
                self.listener = listener
                self.events.append("runtime:listener")
                registration = Registration("runtime-listener", self.events)
                self.registrations["runtime-listener"] = registration
                return registration

            def register_local_endpoint(self, endpoint):
                role = endpoint.connection_role
                self.events.append(f"endpoint:{role}")
                if role == self.fail_role:
                    raise RuntimeError(f"failed:{role}")
                self.endpoints[role] = endpoint
                registration = Registration(role, self.events)
                self.registrations[role] = registration
                return registration

            def prepare_observer(self, request, sink):
                prepared = Prepared(self.events)
                self.prepared.append(prepared)
                return prepared

            def control_snapshot(self, scope):
                return SimpleNamespace(control_revision=0)

            def invoke_owner_action(self, request):
                return SimpleNamespace(status="accepted", payload={})

            def audit(self, event):
                self.events.append(f"audit:{event.name}")

            def rollover(self):
                self.generation = "generation-2"
                self.listener(self.descriptor())

        class Context:
            gateway_extension_spi_version = 1
            gateway_extension_capabilities = SPI_CAPABILITIES

            def __init__(self, host):
                self.host = host
                self.registration = None

            def register_gateway_extension(self, extension, *, spi_version):
                assert spi_version == 1
                self.registration = extension.install(self.host)

        class Manager:
            def __init__(self):
                self.plugin = None
                self.host = None
                self.registration = None
                self.hosts = []

            def _install(self, label, *, fail_role=None):
                host = Host(label, fail_role=fail_role)
                self.hosts.append(host)
                context = Context(host)
                self.plugin.register(context)
                self.host = host
                self.registration = context.registration

            def discover_and_install(self):
                matches = [
                    ep for ep in metadata.entry_points().select(
                        group="hermes_agent.plugins"
                    ) if ep.name == "hermes-agent-plugin"
                ]
                assert len(matches) == 1
                self.plugin = matches[0].load()
                self._install("initial")

            def force_reload(self, *, fail=False):
                self.registration.close()
                self.registration = None
                self.plugin = importlib.reload(self.plugin)
                try:
                    self._install("failed" if fail else "reloaded", fail_role=(
                        "control" if fail else None
                    ))
                except RuntimeError:
                    self._install("rollback")
                    return False
                return True

            def unload(self):
                if self.registration is None:
                    return
                self.registration.close()
                self.registration = None

            def shutdown(self):
                self.unload()

        distribution = metadata.distribution("hermes-agent-plugin")
        direct_url = distribution.read_text("direct_url.json")
        if direct_url is not None:
            assert not json.loads(direct_url).get("dir_info", {}).get("editable", False)
        import hermes_agent_plugin
        from hermes_agent_plugin.adapters.host.spi_v1 import HostSpiFactories
        from hermes_agent_plugin.bootstrap import registration as registration_module

        def dto(**values):
            return SimpleNamespace(**values)

        test_host_spi_factories = HostSpiFactories(
            observer_request=dto,
            control_scope=dto,
            owner_action_request=dto,
            safe_audit_event=dto,
            session_catalog_request=dto,
        )
        registration_module.load_public_host_spi_factories = (
            lambda: test_host_spi_factories
        )
        module_path = Path(hermes_agent_plugin.__file__).resolve()
        environment_root = Path(sys.prefix).resolve()
        source_root = Path(sys.argv[1]).resolve()
        assert module_path.is_relative_to(environment_root)
        assert not module_path.is_relative_to(source_root)
        installed = {dist.metadata["Name"] for dist in metadata.distributions()}
        assert "hermes-agent-plugin" in installed
        assert "websockets" not in installed

        manager = Manager()
        manager.discover_and_install()
        initial = manager.host
        observer = initial.endpoints["observer"]
        prepared = observer.prepare_observer({
            "profile": "default", "session_key": "durable-1",
            "runtime_generation": "generation-1",
        }, object())
        active = observer.prepare_observer({
            "profile": "default", "session_key": "durable-1",
            "runtime_generation": "generation-1",
        }, object())
        subscription = active.activate()
        initial.rollover()
        assert observer.runtime_generation == "generation-2"
        assert initial.endpoints["control"].runtime_generation == "generation-2"
        assert initial.prepared[0].close_calls == 1
        assert initial.prepared[1].subscription.close_calls == 1
        try:
            prepared.activate()
        except RuntimeError as error:
            assert str(error) == "runtime generation changed"
        else:
            raise AssertionError("revoked prepared observer activated")

        assert manager.force_reload() is True
        assert manager.force_reload(fail=True) is False
        rollback = manager.host
        manager.unload()
        event_count = len(rollback.events)
        manager.shutdown()
        assert len(rollback.events) == event_count

        for host in manager.hosts:
            assert all(
                registration.close_calls == 1
                for registration in host.registrations.values()
            ), (host.label, host.events)
        failed = next(host for host in manager.hosts if host.label == "failed")
        assert failed.events[-4:] == [
            "close:observer", "close:local-gateway", "close:runtime-listener",
            "audit:runtime.lifecycle",
        ]
        for host in (
            next(host for host in manager.hosts if host.label == "initial"),
            next(host for host in manager.hosts if host.label == "reloaded"),
            rollback,
        ):
            assert host.events[-5:] == [
                "close:control", "close:observer", "close:local-gateway",
                "close:runtime-listener",
                "audit:runtime.lifecycle",
            ]
        assert not Path(sys.argv[2]).exists()
        """
    )
    result = subprocess.run(
        [str(python), "-c", script, str(PLUGIN_ROOT), str(hermes_home)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert hermes_home.exists() is False
    assert runtime_root.exists() is False
