from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest

COMMON_PACKAGING = Path(__file__).parents[2] / "packaging" / "common"
MACOS_PACKAGING = Path(__file__).parents[2] / "packaging" / "macos"
sys.path.insert(0, str(COMMON_PACKAGING))
sys.path.insert(0, str(MACOS_PACKAGING))

import hermes_macos_activation as activation_module
from hermes_activation_contract import (
    CommandResult,
    HealthGate,
    HostRuntimeEvidence,
    ProcessIdentity,
    RoleDescriptorEvidence,
    SystemCommand,
)
from hermes_local_release import (
    ArtifactInput,
    BuildCommand,
    ReleaseBuilder,
    ReleaseInputs,
    RuntimeReleaseInput,
)
from hermes_macos_activation import (
    ActivationBlocked,
    ActivationController,
    ActivationError,
    ActivationRequest,
)
from hermes_macos_launch_agents import render_release_launch_agents


def _artifact(tmp_path: Path, name: str, content: bytes) -> ArtifactInput:
    path = tmp_path / name
    path.write_bytes(content)
    return ArtifactInput(path, hashlib.sha256(content).hexdigest())


class BuildRunner:
    def run(self, command: BuildCommand) -> CommandResult:
        if command.purpose.startswith("verify-"):
            runtime = (
                "host" if command.purpose == "verify-host-runtime" else "connector"
            )
            return CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    {
                        "module_origin": str(
                            command.release_dir
                            / runtime
                            / "venv"
                            / "lib"
                            / "site-packages"
                        ),
                        "console_entrypoint": str(
                            command.release_dir
                            / runtime
                            / "venv"
                            / "bin"
                            / command.argv[-3]
                        ),
                    }
                ),
            )
        return CommandResult(exit_code=0, stdout="")


def _candidate_release(tmp_path: Path) -> Path:
    releases = tmp_path / "releases"
    release_id = "2026.08.03-b2b"
    plugin = _artifact(
        tmp_path, "hermes_agent_plugin-1.0.0-py3-none-any.whl", b"plugin"
    )
    store = tmp_path / "activation-state" / "state" / "default" / "plugin-store"
    wheel_path = (
        releases
        / release_id
        / "plugin"
        / "artifacts"
        / "hermes-agent-plugin"
        / "1.0.0"
        / plugin.sha256
        / plugin.path.name
    )
    trust = {
        "schema_version": 1,
        "keys": [
            {
                "key_id": "key-1",
                "signature_algorithm": "ed25519",
                "public_key": base64.b64encode(b"p" * 32).decode(),
                "not_before": "2026-01-01T00:00:00Z",
                "not_after": "2027-01-01T00:00:00Z",
            }
        ],
    }
    inputs = ReleaseInputs(
        release_id=release_id,
        core=RuntimeReleaseInput(
            "hermes-core",
            "1.2.3",
            _artifact(tmp_path, "hermes_core-1.2.3-py3-none-any.whl", b"core"),
            _artifact(tmp_path, "core.uv.lock", b"version = 1\n"),
            _artifact(
                tmp_path, "core.pyproject.toml", b"[project]\nname='hermes-core'\n"
            ),
            "hermes",
            "hermes_cli.main:main",
            "hermes_cli.main",
        ),
        plugin_bundle=plugin,
        plugin_store_manifest=_artifact(
            tmp_path, "trust-input.json", json.dumps(trust, sort_keys=True).encode()
        ),
        signed_plugin_manifest={
            "schema_version": 1,
            "plugin_id": "hermes-agent-plugin",
            "version": "1.0.0",
            "wheel_path": str(wheel_path),
            "wheel_sha256": plugin.sha256,
            "store_root": str(store),
            "entrypoint": {
                "group": "hermes_agent.plugins",
                "name": "hermes-agent-plugin",
                "value": "hermes_agent_plugin",
            },
            "signature_algorithm": "ed25519",
            "key_id": "key-1",
            "issued_at": "2026-04-01T00:00:00Z",
            "expires_at": "2026-12-01T00:00:00Z",
            "signature": base64.b64encode(b"s" * 64).decode(),
        },
        connector=RuntimeReleaseInput(
            "hermes-connector",
            "0.1.0",
            _artifact(
                tmp_path, "hermes_connector-0.1.0-py3-none-any.whl", b"connector"
            ),
            _artifact(tmp_path, "connector.uv.lock", b"version = 1\n"),
            _artifact(
                tmp_path,
                "connector.pyproject.toml",
                b"[project]\nname='hermes-connector'\n",
            ),
            "hermes-connector",
            "hermes_connector.cli:main",
            "hermes_connector.cli",
        ),
    )
    result = ReleaseBuilder(
        releases_root=releases,
        runner=BuildRunner(),
        service_renderer=render_release_launch_agents,
    ).build(inputs)
    for relative in (
        "host/venv/bin/hermes",
        "host/venv/bin/python",
        "connector/venv/bin/hermes-connector",
    ):
        path = result.release_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("executable")
        path.chmod(0o700)
    return result.release_dir


def _identity(tmp_path: Path, *, pid: int = 100, start: int = 1000) -> ProcessIdentity:
    executable = tmp_path / "old-python"
    executable.write_text("old")
    executable.chmod(0o700)
    metadata = executable.stat()
    return ProcessIdentity(
        pid=pid,
        process_start_time_ns=start,
        process_executable=executable,
        process_executable_device=metadata.st_dev,
        process_executable_inode=metadata.st_ino,
        profile="default",
        hermes_home=tmp_path / "hermes-home",
        authority_id="old-authority",
        runtime_generation=1,
        instance_id="old-instance",
        host_bundle_id="old-bundle",
    )


def _candidate_runtime(release: Path, hermes_home: Path) -> HostRuntimeEvidence:
    executable = release / "host" / "venv" / "bin" / "python"
    metadata = executable.stat()
    process = ProcessIdentity(
        pid=200,
        process_start_time_ns=2000,
        process_executable=executable,
        process_executable_device=metadata.st_dev,
        process_executable_inode=metadata.st_ino,
        profile="default",
        hermes_home=hermes_home,
        authority_id="candidate-authority",
        runtime_generation=1,
        instance_id="instance",
        host_bundle_id="bundle",
    )
    descriptors_list: list[RoleDescriptorEvidence] = []
    for role in ("local", "control", "observer"):
        short_id = hashlib.sha256(str(hermes_home).encode()).hexdigest()[:10]
        socket_path = Path("/tmp") / f"hb2-{short_id}-{role[0]}.sock"
        if socket_path.exists():
            socket_path.unlink()
        uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        uds.bind(str(socket_path))
        uds.close()
        socket_metadata = socket_path.stat()
        descriptors_list.append(
            RoleDescriptorEvidence(
                role=role,
                pid=process.pid,
                process_start_time_ns=process.process_start_time_ns,
                process_executable=process.process_executable,
                process_executable_device=process.process_executable_device,
                process_executable_inode=process.process_executable_inode,
                authority_id=process.authority_id,
                runtime_generation=process.runtime_generation,
                instance_id=process.instance_id,
                host_bundle_id=process.host_bundle_id,
                socket_path=socket_path,
                socket_device=socket_metadata.st_dev,
                socket_inode=socket_metadata.st_ino,
                is_socket=True,
                peer_pid=process.pid,
            )
        )
    return HostRuntimeEvidence(
        process=process,
        plugin_store_active=True,
        plugin_manifest_path=release
        / "plugin"
        / "metadata"
        / "signed-plugin-manifest.json",
        trust_store_path=release / "plugin" / "metadata" / "trust-store.json",
        descriptors=tuple(descriptors_list),
    )


class ScriptedPlatform:
    def __init__(
        self,
        hosts: list[tuple[ProcessIdentity, ...]],
        runtime: HostRuntimeEvidence,
        *,
        fail_purpose: str | None = None,
        gate_failure: str | None = None,
        rollback_failure: str | None = None,
        status_release_id: str = "2026.08.03-b2b",
        status_profile: str = "default",
    ) -> None:
        self.hosts = hosts
        self.runtime = runtime
        self.fail_purpose = fail_purpose
        self.gate_failure = gate_failure
        self.rollback_failure = rollback_failure
        self.status_release_id = status_release_id
        self.status_profile = status_profile
        self.commands: list[SystemCommand] = []

    def discover_hosts(
        self, profile: str, hermes_home: Path
    ) -> tuple[ProcessIdentity, ...]:
        assert profile == "default"
        return self.hosts.pop(0)

    def inspect_candidate_runtime(
        self, release_dir: Path, profile: str
    ) -> HostRuntimeEvidence:
        return self.runtime

    def run(self, command: SystemCommand) -> CommandResult:
        self.commands.append(command)
        if command.purpose in {self.fail_purpose, self.rollback_failure}:
            raise RuntimeError(f"injected {command.purpose} failure")
        if command.purpose == "connector-status":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "release_id": self.status_release_id,
                        "pid": 300,
                        "process_start_time_ns": 3000,
                        "process_executable": "/venv/bin/python",
                        "process_executable_device": 1,
                        "process_executable_inode": 2,
                        "runtime_generation": 1,
                        "local_authority_identity": {
                            "profile": self.status_profile,
                            "instance_id": "instance",
                            "host_bundle_id": "bundle",
                        },
                        "cloud_state": "active",
                        "updated_at": "2026-08-03T00:00:00Z",
                        "ready": True,
                    }
                ),
            )
        if command.purpose.startswith("health-"):
            name = command.purpose.removeprefix("health-")
            ready = self.gate_failure != name
            field = {
                "cloud": ("cloud_transport", "active"),
                "catalog": ("catalog_source", "real"),
                "h5": ("h5_flow", "ready"),
            }[name]
            return CommandResult(
                0 if ready else 2,
                json.dumps(
                    {"ready": ready, "release_id": "2026.08.03-b2b", field[0]: field[1]}
                ),
            )
        return CommandResult(0, "")


def _request(
    tmp_path: Path, release: Path, old: ProcessIdentity, *, apply: bool
) -> ActivationRequest:
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    old_host = launch_agents / "com.hermes.host.plist"
    old_connector = launch_agents / "com.hermes.connector.plist"
    old_host.write_bytes(plistlib.dumps({"Label": "old-host"}))
    old_connector.write_bytes(plistlib.dumps({"Label": "old-connector"}))
    old_host.chmod(0o600)
    old_connector.chmod(0o600)
    old_receipt = tmp_path / "old-active.json"
    old_receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profile": "default",
                "release_id": "old-release",
                "authority": old.to_json(),
                "host": {
                    "kind": "desktop",
                    "label": "old-host",
                    "plist_path": str(old_host),
                    "plist_sha256": hashlib.sha256(old_host.read_bytes()).hexdigest(),
                    "start_argv": ["/usr/bin/open", "-b", "com.hermes.desktop"],
                },
                "connector": {
                    "label": "old-connector",
                    "plist_path": str(old_connector),
                    "plist_sha256": hashlib.sha256(
                        old_connector.read_bytes()
                    ).hexdigest(),
                },
            },
            sort_keys=True,
        )
    )
    old_receipt.chmod(0o600)
    helper = tmp_path / "reviewed-health"
    helper.write_text("helper")
    helper.chmod(0o700)
    return ActivationRequest(
        release_dir=release,
        profile="default",
        hermes_home=old.hermes_home,
        state_root=tmp_path / "activation-state",
        launch_agents_dir=launch_agents,
        old_receipt_path=old_receipt,
        domain_target="gui/501",
        health_gates=(
            HealthGate("cloud", (str(helper), "cloud"), {"cloud_transport": "active"}),
            HealthGate("catalog", (str(helper), "catalog"), {"catalog_source": "real"}),
            HealthGate("h5", (str(helper), "h5"), {"h5_flow": "ready"}),
        ),
        reviewed_health_helpers=(helper,),
        apply=apply,
    )


def test_default_dry_run_builds_auditable_plan_without_cutover(tmp_path: Path) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=False)
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform([(old,)], runtime)

    result = ActivationController(platform=platform).activate(request)

    assert result.applied is False
    assert platform.commands == []
    assert [command.purpose for command in result.plan.commands][-4:] == [
        "connector-status",
        "health-cloud",
        "health-catalog",
        "health-h5",
    ]
    assert not result.active_receipt.exists()


def test_apply_has_no_dual_host_and_writes_private_active_receipt(
    tmp_path: Path,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    old.hermes_home.mkdir(parents=True, exist_ok=True)
    config_sentinel = old.hermes_home / "config-sentinel"
    config_sentinel.write_text("unchanged")
    immutable_wheel = Path(
        json.loads((release / "manifest" / "release.json").read_text())[
            "signed_plugin_manifest"
        ]["wheel_path"]
    )
    immutable_digest = hashlib.sha256(immutable_wheel.read_bytes()).hexdigest()
    platform = ScriptedPlatform([(old,), (old,), (), (runtime.process,)], runtime)

    result = ActivationController(platform=platform).activate(request)

    assert result.applied is True
    assert result.active_receipt.stat().st_mode & 0o777 == 0o600
    receipt = json.loads(result.active_receipt.read_text())
    assert receipt["release_id"] == release.name
    purposes = [command.purpose for command in platform.commands]
    assert purposes.index("stop-old-host") < purposes.index("bootstrap-candidate-host")
    assert purposes.index("bootstrap-candidate-host") < purposes.index(
        "bootstrap-candidate-connector"
    )
    assert purposes[-4:] == [
        "connector-status",
        "health-cloud",
        "health-catalog",
        "health-h5",
    ]
    assert config_sentinel.read_text() == "unchanged"
    assert hashlib.sha256(immutable_wheel.read_bytes()).hexdigest() == immutable_digest
    assert immutable_wheel.stat().st_mode & 0o777 == 0o400
    plugin_store = request.state_root / "state" / "default" / "plugin-store"
    assert plugin_store.is_dir() and plugin_store.stat().st_mode & 0o777 == 0o700
    assert plistlib.loads(
        (request.launch_agents_dir / "com.hermes.connector.plist").read_bytes()
    )["ProgramArguments"] == [
        str(release / "connector" / "venv" / "bin" / "hermes-connector"),
        "run",
        "--release-id",
        release.name,
    ]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_refuses_zero_or_ambiguous_old_authority(
    tmp_path: Path, ambiguous: bool
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    discovered = (old, replace(old, pid=101)) if ambiguous else ()
    request = _request(tmp_path, release, old, apply=False)
    platform = ScriptedPlatform(
        [discovered], _candidate_runtime(release, old.hermes_home)
    )

    with pytest.raises(ActivationError, match="exactly one old Host authority"):
        ActivationController(platform=platform).activate(request)
    assert platform.commands == []


def test_pid_reuse_or_new_process_between_plan_and_apply_fails_closed(
    tmp_path: Path,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    reused = replace(old, process_start_time_ns=old.process_start_time_ns + 1)
    request = _request(tmp_path, release, old, apply=True)
    platform = ScriptedPlatform(
        [(old,), (reused,)], _candidate_runtime(release, old.hermes_home)
    )

    with pytest.raises(ActivationError, match="authority changed before cutover"):
        ActivationController(platform=platform).activate(request)
    assert platform.commands == []


def test_profile_lock_contention_fails_without_system_command(tmp_path: Path) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=False)
    lock_path = (
        request.state_root / "state" / "default" / "activation" / "activation.lock"
    )
    lock_path.parent.mkdir(parents=True)
    lock_path.touch(mode=0o600)
    descriptor = os.open(lock_path, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        platform = ScriptedPlatform(
            [(old,)], _candidate_runtime(release, old.hermes_home)
        )
        with pytest.raises(ActivationError, match="activation lock is busy"):
            ActivationController(platform=platform).activate(request)
        assert platform.commands == []
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("failed_gate", ["cloud", "catalog", "h5"])
def test_health_gate_failure_rolls_back_connector_then_host_and_restores_old(
    tmp_path: Path, failed_gate: str
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    old_host = (request.launch_agents_dir / "com.hermes.host.plist").read_bytes()
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform(
        [(old,), (old,), (), (runtime.process,), (old,)],
        runtime,
        gate_failure=failed_gate,
    )

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)

    purposes = [command.purpose for command in platform.commands]
    assert purposes.index("rollback-bootout-candidate-connector") < purposes.index(
        "rollback-bootout-candidate-host"
    )
    assert purposes.index("rollback-restart-old-host") < purposes.index(
        "rollback-restart-old-connector"
    )
    assert (
        request.launch_agents_dir / "com.hermes.host.plist"
    ).read_bytes() == old_host
    rollback = request.state_root / "state" / "default" / "activation" / "rollback.json"
    assert rollback.is_file() and rollback.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("failed_purpose", "hosts"),
    [
        ("stop-old-connector", "before-stop"),
        ("stop-old-host", "before-stop"),
        ("bootstrap-candidate-host", "after-stop"),
        ("bootstrap-candidate-connector", "after-candidate"),
    ],
)
def test_each_service_command_failure_rolls_back(
    tmp_path: Path,
    failed_purpose: str,
    hosts: str,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    if hosts == "before-stop":
        snapshots = [(old,), (old,), (old,)]
    elif hosts == "after-stop":
        snapshots = [(old,), (old,), (), (old,)]
    else:
        snapshots = [(old,), (old,), (), (runtime.process,), (old,)]
    platform = ScriptedPlatform(
        snapshots,
        runtime,
        fail_purpose=failed_purpose,
    )

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)

    assert (
        request.state_root / "state" / "default" / "activation" / "rollback.json"
    ).is_file()


def test_connector_status_release_mismatch_rolls_back(tmp_path: Path) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform(
        [(old,), (old,), (), (runtime.process,), (old,)],
        runtime,
        status_release_id="other-release",
    )

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)


def test_connector_status_authority_mismatch_rolls_back(tmp_path: Path) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform(
        [(old,), (old,), (), (runtime.process,), (old,)],
        runtime,
        status_profile="other-profile",
    )

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)


def test_repeated_apply_revalidates_health_without_second_cutover(
    tmp_path: Path,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    first = ScriptedPlatform([(old,), (old,), (), (runtime.process,)], runtime)
    ActivationController(platform=first).activate(request)

    second = ScriptedPlatform([(runtime.process,)], runtime)
    result = ActivationController(platform=second).activate(request)

    assert result.applied is True and result.reused is True
    purposes = [command.purpose for command in second.commands]
    assert purposes == [
        "connector-status",
        "health-cloud",
        "health-catalog",
        "health-h5",
    ]
    assert not any("stop" in purpose or "bootstrap" in purpose for purpose in purposes)


def test_pending_power_loss_is_rolled_back_before_new_apply(tmp_path: Path) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    activation = request.state_root / "state" / "default" / "activation"
    backup = activation / "attempt-power-loss" / "backup"
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(
        request.launch_agents_dir / "com.hermes.host.plist", backup / "host.plist"
    )
    shutil.copy2(
        request.launch_agents_dir / "com.hermes.connector.plist",
        backup / "connector.plist",
    )
    (backup / "host.plist").chmod(0o600)
    (backup / "connector.plist").chmod(0o600)
    shutil.copy2(
        release / "services" / "com.hermes.host.plist",
        request.launch_agents_dir / "com.hermes.host.plist",
    )
    shutil.copy2(
        release / "services" / "com.hermes.connector.plist",
        request.launch_agents_dir / "com.hermes.connector.plist",
    )
    manifest = json.loads((release / "manifest" / "release.json").read_text())
    pending = activation / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": release.name,
                "release_digest": manifest["release_digest"],
                "profile": "default",
                "phase": "candidate-plists-installed",
                "attempt_dir": str(backup.parent),
                "old_authority": old.to_json(),
            },
            sort_keys=True,
        )
    )
    pending.chmod(0o600)
    platform = ScriptedPlatform(
        [(old,), (old,), (old,), (), (runtime.process,)],
        runtime,
    )

    result = ActivationController(platform=platform).activate(request)

    assert result.applied is True
    purposes = [command.purpose for command in platform.commands]
    assert purposes[:4] == [
        "rollback-bootout-candidate-connector",
        "rollback-bootout-candidate-host",
        "rollback-restart-old-host",
        "rollback-restart-old-connector",
    ]
    assert not pending.exists()


def test_secret_like_health_argv_is_rejected_before_system_commands(
    tmp_path: Path,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=False)
    bad_gate = replace(
        request.health_gates[0],
        argv=(*request.health_gates[0].argv, "--token=must-not-log"),
    )
    request = replace(request, health_gates=(bad_gate, *request.health_gates[1:]))
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform([(old,)], runtime)

    with pytest.raises(ActivationError, match="secret-like"):
        ActivationController(platform=platform).activate(request)
    assert platform.commands == []


@pytest.mark.parametrize("tamper", ["artifact", "plist-mode", "store-root"])
def test_release_or_profile_state_tamper_is_rejected_before_commands(
    tmp_path: Path,
    tamper: str,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=False)
    if tamper == "artifact":
        manifest = json.loads((release / "manifest" / "release.json").read_text())
        wheel = Path(manifest["signed_plugin_manifest"]["wheel_path"])
        wheel.chmod(0o600)
        wheel.write_bytes(b"tampered")
    elif tamper == "plist-mode":
        (release / "services" / "com.hermes.host.plist").chmod(0o644)
    else:
        request = replace(request, state_root=tmp_path / "other-managed")
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform([(old,)], runtime)

    with pytest.raises(ActivationError):
        ActivationController(platform=platform).activate(request)
    assert platform.commands == []


@pytest.mark.parametrize("runtime_fault", ["descriptor", "store-inactive"])
def test_candidate_runtime_fault_rolls_back(
    tmp_path: Path,
    runtime_fault: str,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    if runtime_fault == "descriptor":
        bad_descriptor = replace(runtime.descriptors[0], peer_pid=999)
        runtime = replace(
            runtime, descriptors=(bad_descriptor, *runtime.descriptors[1:])
        )
    else:
        runtime = replace(runtime, plugin_store_active=False)
    platform = ScriptedPlatform(
        [(old,), (old,), (), (runtime.process,), (old,)],
        runtime,
    )

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)


def test_atomic_plist_install_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform([(old,), (old,), (), (old,)], runtime)
    real_install = activation_module._atomic_install
    calls = 0

    def fail_second_install(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected atomic install failure")
        real_install(source, destination)

    monkeypatch.setattr(activation_module, "_atomic_install", fail_second_install)

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)


def test_active_receipt_fsync_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform(
        [(old,), (old,), (), (runtime.process,), (old,)], runtime
    )
    real_write = activation_module._write_json

    def fail_active(path: Path, value: dict[str, object]) -> None:
        if path.name == "active.json":
            raise OSError("injected active receipt failure")
        real_write(path, value)

    monkeypatch.setattr(activation_module, "_write_json", fail_active)

    with pytest.raises(ActivationError, match="rolled back"):
        ActivationController(platform=platform).activate(request)
    assert not (
        request.state_root / "state" / "default" / "activation" / "active.json"
    ).exists()


def test_rollback_failure_reports_blocked_and_preserves_private_evidence(
    tmp_path: Path,
) -> None:
    release = _candidate_release(tmp_path)
    old = _identity(tmp_path)
    request = _request(tmp_path, release, old, apply=True)
    runtime = _candidate_runtime(release, old.hermes_home)
    platform = ScriptedPlatform(
        [(old,), (old,), (), (runtime.process,)],
        runtime,
        gate_failure="catalog",
        rollback_failure="rollback-restart-old-host",
    )

    with pytest.raises(ActivationBlocked, match="rollback failed") as captured:
        ActivationController(platform=platform).activate(request)

    assert captured.value.evidence_path.is_file()
    assert captured.value.evidence_path.stat().st_mode & 0o777 == 0o600
    assert "signature" not in captured.value.evidence_path.read_text().lower()
