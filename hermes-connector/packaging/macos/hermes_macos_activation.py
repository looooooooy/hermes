"""Fail-closed macOS activation orchestration for immutable Hermes releases.

The controller owns ordering, validation, receipts, and rollback.  OS observation and
command execution are injected so dry-run and tests never mutate the live service domain.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hermes_activation_contract import (
    ActivationContractError,
    CommandResult,
    HealthGate,
    HostRuntimeEvidence,
    ProcessIdentity,
    SystemCommand,
    ValidatedRelease,
    validate_b1_release,
)

_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SERVICE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}\Z")
_STATUS_FIELDS = {
    "release_id",
    "pid",
    "process_start_time_ns",
    "process_executable",
    "process_executable_device",
    "process_executable_inode",
    "runtime_generation",
    "local_authority_identity",
    "cloud_state",
    "updated_at",
    "ready",
}
_AUTHORITY_FIELDS = {"profile", "instance_id", "host_bundle_id"}
_SECRET_MARKERS = ("token", "password", "secret", "authorization", "credential")


class ActivationError(RuntimeError):
    """Activation failed closed, optionally after successful rollback."""


class ActivationBlocked(ActivationError):
    """Activation and rollback both failed; operator coordination is required."""

    def __init__(self, message: str, *, evidence_path: Path) -> None:
        super().__init__(message)
        self.evidence_path = evidence_path


class ActivationPlatform(Protocol):
    def discover_hosts(
        self, profile: str, hermes_home: Path
    ) -> tuple[ProcessIdentity, ...]: ...

    def inspect_candidate_runtime(
        self, release_dir: Path, profile: str
    ) -> HostRuntimeEvidence: ...

    def run(self, command: SystemCommand) -> CommandResult: ...


@dataclass(frozen=True)
class ActivationRequest:
    release_dir: Path
    profile: str
    hermes_home: Path
    state_root: Path
    launch_agents_dir: Path
    old_receipt_path: Path
    domain_target: str
    health_gates: tuple[HealthGate, ...]
    reviewed_health_helpers: tuple[Path, ...]
    apply: bool = False


@dataclass(frozen=True)
class ActivationPlan:
    release_id: str
    release_digest: str
    profile: str
    commands: tuple[SystemCommand, ...]


@dataclass(frozen=True)
class ActivationResult:
    applied: bool
    reused: bool
    plan: ActivationPlan
    active_receipt: Path


@dataclass(frozen=True)
class _OldService:
    kind: str
    label: str
    plist_path: Path
    plist_sha256: str
    start_argv: tuple[str, ...]


@dataclass(frozen=True)
class _OldReceipt:
    release_id: str
    authority: ProcessIdentity
    host: _OldService
    connector: _OldService


class ActivationController:
    def __init__(self, *, platform: ActivationPlatform) -> None:
        self._platform = platform

    def activate(self, request: ActivationRequest) -> ActivationResult:
        _validate_prelock_request(request)
        paths = _activation_paths(request)
        _prepare_private_lock_path(paths)
        with _profile_lock(paths["lock"]):
            validated = self._validate_request(request, paths)
            if paths["pending"].exists():
                if not request.apply:
                    raise ActivationError(
                        "pending activation requires explicit apply recovery"
                    )
                old = _read_old_receipt(request, validate_current_plists=False)
                pending = _read_pending(paths["pending"], validated, request)
                try:
                    pending_authority = ProcessIdentity.from_json(
                        pending["old_authority"]
                    )
                except ActivationContractError as exc:
                    raise ActivationError(
                        "pending activation old authority is invalid"
                    ) from exc
                if pending_authority != old.authority:
                    raise ActivationError(
                        "pending activation old authority does not match receipt"
                    )
                recovery_backup = Path(pending["attempt_dir"]) / "backup"
                _validate_backup(recovery_backup, old)
                self._rollback(
                    request=request,
                    validated=validated,
                    old=old,
                    backup=recovery_backup,
                    paths=paths,
                    cause=ActivationError("recovering interrupted activation"),
                )

            reused = self._reuse_if_active(request, validated, paths)
            if reused is not None:
                return reused
            old = _read_old_receipt(request)
            discovered = self._platform.discover_hosts(
                request.profile, request.hermes_home
            )
            _require_exact_old_authority(discovered, old.authority)
            _validate_process_executable(old.authority)
            plan = _build_plan(request, validated, old)
            result = ActivationResult(
                applied=False,
                reused=False,
                plan=plan,
                active_receipt=paths["active"],
            )
            if not request.apply:
                return result

            current = self._platform.discover_hosts(
                request.profile, request.hermes_home
            )
            if current != (old.authority,):
                raise ActivationError("authority changed before cutover")
            _validate_process_executable(old.authority)
            _ensure_mutable_state(paths, expected_store_root=paths["plugin_store"])
            attempt = paths["activation"] / f"attempt-{uuid.uuid4().hex}"
            backup = attempt / "backup"
            backup.mkdir(parents=True, mode=0o700)
            _copy_private(old.host.plist_path, backup / "host.plist")
            _copy_private(old.connector.plist_path, backup / "connector.plist")
            _validate_backup(backup, old)
            _write_json(
                paths["pending"],
                {
                    "schema_version": 1,
                    "release_id": validated.release_id,
                    "release_digest": validated.release_digest,
                    "profile": request.profile,
                    "phase": "prepared",
                    "attempt_dir": str(attempt),
                    "old_authority": old.authority.to_json(),
                },
            )
            executed: list[str] = []
            try:
                for command in plan.commands[:2]:
                    self._run(request, validated, command)
                    executed.append(command.purpose)
                if (
                    self._platform.discover_hosts(request.profile, request.hermes_home)
                    != ()
                ):
                    raise ActivationError("old Host authority did not reach zero")
                _atomic_install(
                    validated.host_plist,
                    request.launch_agents_dir / "com.hermes.host.plist",
                )
                _atomic_install(
                    validated.connector_plist,
                    request.launch_agents_dir / "com.hermes.connector.plist",
                )
                _update_pending(paths["pending"], "candidate-plists-installed")

                self._run(request, validated, plan.commands[2])
                executed.append(plan.commands[2].purpose)
                runtime = self._platform.inspect_candidate_runtime(
                    validated.release_dir, request.profile
                )
                candidate_hosts = self._platform.discover_hosts(
                    request.profile, request.hermes_home
                )
                _validate_candidate_runtime(
                    runtime,
                    candidate_hosts,
                    request,
                    validated,
                )

                for command in plan.commands[3:]:
                    response = self._run(request, validated, command)
                    executed.append(command.purpose)
                    if command.purpose == "connector-status":
                        _validate_connector_status(
                            response, validated.release_id, runtime.process
                        )
                    elif command.purpose.startswith("health-"):
                        gate_name = command.purpose.removeprefix("health-")
                        gate = next(
                            item
                            for item in request.health_gates
                            if item.name == gate_name
                        )
                        _validate_health(response, validated.release_id, gate)

                active_payload = {
                    "schema_version": 1,
                    "release_id": validated.release_id,
                    "release_digest": validated.release_digest,
                    "profile": request.profile,
                    "authority": runtime.process.to_json(),
                    "plugin_store_root": str(paths["plugin_store"]),
                    "commands": executed,
                    "status": "active",
                }
                _write_json(paths["active"], active_payload)
                _remove_file(paths["pending"])
                return ActivationResult(True, False, plan, paths["active"])
            except Exception as activation_failure:
                try:
                    self._rollback(
                        request=request,
                        validated=validated,
                        old=old,
                        backup=backup,
                        paths=paths,
                        cause=activation_failure,
                    )
                except Exception as rollback_failure:
                    evidence = {
                        "schema_version": 1,
                        "release_id": validated.release_id,
                        "profile": request.profile,
                        "status": "blocked",
                        "activation_error": type(activation_failure).__name__,
                        "rollback_error": type(rollback_failure).__name__,
                        "executed_purposes": executed,
                    }
                    _write_json(paths["blocked"], evidence)
                    raise ActivationBlocked(
                        "activation failed and rollback failed; blocked evidence retained",
                        evidence_path=paths["blocked"],
                    ) from rollback_failure
                raise ActivationError(
                    "activation failed and was rolled back"
                ) from activation_failure

    def _reuse_if_active(
        self,
        request: ActivationRequest,
        validated: ValidatedRelease,
        paths: Mapping[str, Path],
    ) -> ActivationResult | None:
        if not paths["active"].exists():
            return None
        active = _read_active(paths["active"])
        if (
            active.get("release_id") != validated.release_id
            or active.get("release_digest") != validated.release_digest
            or active.get("profile") != request.profile
            or active.get("status") != "active"
        ):
            return None
        commands = _readiness_commands(request, validated)
        plan = ActivationPlan(
            validated.release_id,
            validated.release_digest,
            request.profile,
            commands,
        )
        if not request.apply:
            return ActivationResult(False, True, plan, paths["active"])
        runtime = self._platform.inspect_candidate_runtime(
            validated.release_dir, request.profile
        )
        candidate_hosts = self._platform.discover_hosts(
            request.profile, request.hermes_home
        )
        _validate_candidate_runtime(runtime, candidate_hosts, request, validated)
        for command in commands:
            response = self._run(request, validated, command)
            if command.purpose == "connector-status":
                _validate_connector_status(
                    response, validated.release_id, runtime.process
                )
            else:
                gate = next(
                    item
                    for item in request.health_gates
                    if command.purpose == f"health-{item.name}"
                )
                _validate_health(response, validated.release_id, gate)
        return ActivationResult(True, True, plan, paths["active"])

    def _validate_request(
        self,
        request: ActivationRequest,
        paths: Mapping[str, Path],
    ) -> ValidatedRelease:
        if not _PROFILE.fullmatch(request.profile):
            raise ActivationError("invalid activation profile")
        if request.domain_target != f"gui/{os.getuid()}":
            raise ActivationError("launch service domain does not match effective user")
        if not request.hermes_home.is_absolute():
            raise ActivationError("HERMES_HOME must be absolute")
        if {gate.name for gate in request.health_gates} != {
            "cloud",
            "catalog",
            "h5",
        } or len(request.health_gates) != 3:
            raise ActivationError(
                "Cloud, catalog, and H5 require three independent gates"
            )
        try:
            validated = validate_b1_release(
                request.release_dir,
                expected_store_root=paths["plugin_store"],
            )
        except ActivationContractError as exc:
            raise ActivationError(str(exc)) from exc
        _validate_candidate_plists(validated)
        _validate_health_commands(request, validated)
        return validated

    def _run(
        self,
        request: ActivationRequest,
        validated: ValidatedRelease,
        command: SystemCommand,
    ) -> CommandResult:
        _validate_command(request, validated, command)
        result = self._platform.run(command)
        if result.exit_code not in {0, 2}:
            raise ActivationError(f"system action failed: {command.purpose}")
        if (
            not (
                command.purpose == "connector-status"
                or command.purpose.startswith("health-")
            )
            and result.exit_code != 0
        ):
            raise ActivationError(f"system action failed: {command.purpose}")
        return result

    def _rollback(
        self,
        *,
        request: ActivationRequest,
        validated: ValidatedRelease,
        old: _OldReceipt,
        backup: Path,
        paths: Mapping[str, Path],
        cause: Exception,
    ) -> None:
        candidate_host = _candidate_plist(validated.host_plist)
        candidate_connector = _candidate_plist(validated.connector_plist)
        commands = (
            SystemCommand(
                "rollback-bootout-candidate-connector",
                (
                    "/bin/launchctl",
                    "bootout",
                    f"{request.domain_target}/{candidate_connector['Label']}",
                ),
            ),
            SystemCommand(
                "rollback-bootout-candidate-host",
                (
                    "/bin/launchctl",
                    "bootout",
                    f"{request.domain_target}/{candidate_host['Label']}",
                ),
            ),
        )
        executed: list[str] = []
        for command in commands:
            self._run(request, validated, command)
            executed.append(command.purpose)
        _atomic_install(backup / "host.plist", old.host.plist_path)
        _atomic_install(backup / "connector.plist", old.connector.plist_path)
        if old.host.kind == "desktop":
            restart_host = SystemCommand(
                "rollback-restart-old-host", old.host.start_argv
            )
        else:
            restart_host = SystemCommand(
                "rollback-restart-old-host",
                (
                    "/bin/launchctl",
                    "bootstrap",
                    request.domain_target,
                    str(old.host.plist_path),
                ),
            )
        restart_connector = SystemCommand(
            "rollback-restart-old-connector",
            (
                "/bin/launchctl",
                "bootstrap",
                request.domain_target,
                str(old.connector.plist_path),
            ),
        )
        for command in (restart_host, restart_connector):
            self._run(request, validated, command)
            executed.append(command.purpose)
        restored = self._platform.discover_hosts(request.profile, request.hermes_home)
        if restored != (old.authority,):
            raise ActivationError("rollback did not restore unique old authority")
        _write_json(
            paths["rollback"],
            {
                "schema_version": 1,
                "release_id": validated.release_id,
                "profile": request.profile,
                "status": "rolled-back",
                "cause": type(cause).__name__,
                "commands": executed,
            },
        )
        _remove_file(paths["pending"])


def _activation_paths(request: ActivationRequest) -> Mapping[str, Path]:
    managed = request.state_root.resolve(strict=False)
    profile_root = managed / "state" / request.profile
    activation = profile_root / "activation"
    return {
        "managed": managed,
        "profile": profile_root,
        "plugin_store": profile_root / "plugin-store",
        "activation": activation,
        "lock": activation / "activation.lock",
        "pending": activation / "pending.json",
        "active": activation / "active.json",
        "rollback": activation / "rollback.json",
        "blocked": activation / "blocked.json",
    }


def _validate_prelock_request(request: ActivationRequest) -> None:
    if not _PROFILE.fullmatch(request.profile):
        raise ActivationError("invalid activation profile")
    for label, path in (
        ("release", request.release_dir),
        ("HERMES_HOME", request.hermes_home),
        ("managed state", request.state_root),
        ("LaunchAgents", request.launch_agents_dir),
    ):
        if not path.is_absolute() or path != path.resolve(strict=False):
            raise ActivationError(f"{label} path must be absolute and canonical")
        for candidate in (path, *path.parents):
            if candidate.is_symlink():
                raise ActivationError(f"{label} path contains a symlink")
    if _is_within(request.state_root, request.release_dir) or _is_within(
        request.state_root, request.hermes_home
    ):
        raise ActivationError(
            "managed activation state must remain outside release and HERMES_HOME"
        )
    if request.domain_target != f"gui/{os.getuid()}":
        raise ActivationError("launch service domain does not match effective user")


def _prepare_private_lock_path(paths: Mapping[str, Path]) -> None:
    for path in (
        paths["managed"],
        paths["profile"].parent,
        paths["profile"],
        paths["activation"],
    ):
        path.mkdir(exist_ok=True, mode=0o700)
        path.chmod(0o700)
        if path.is_symlink():
            raise ActivationError("activation lock path is unsafe")


@contextmanager
def _profile_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActivationError("activation lock is busy") from exc
        yield
    finally:
        os.close(descriptor)


def _ensure_mutable_state(
    paths: Mapping[str, Path], *, expected_store_root: Path
) -> None:
    for key in ("managed", "profile", "plugin_store", "activation"):
        path = paths[key]
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        if path.is_symlink() or path.resolve(strict=False) != path:
            raise ActivationError("mutable activation state path is unsafe")
    if paths["plugin_store"] != expected_store_root:
        raise ActivationError("Plugin store root mismatch")


def _read_old_receipt(
    request: ActivationRequest,
    *,
    validate_current_plists: bool = True,
) -> _OldReceipt:
    path = request.old_receipt_path
    _require_file(path, 0o600)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationError("old service receipt is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "profile",
        "release_id",
        "authority",
        "host",
        "connector",
    }:
        raise ActivationError("old service receipt has invalid fields")
    if value["schema_version"] != 1 or value["profile"] != request.profile:
        raise ActivationError("old service receipt profile mismatch")
    try:
        authority = ProcessIdentity.from_json(value["authority"])
    except ActivationContractError as exc:
        raise ActivationError(str(exc)) from exc
    if (
        authority.profile != request.profile
        or authority.hermes_home != request.hermes_home
    ):
        raise ActivationError("old authority scope does not match request")
    host = _parse_old_service(value["host"], host=True)
    connector = _parse_old_service(value["connector"], host=False)
    expected_host = request.launch_agents_dir / "com.hermes.host.plist"
    expected_connector = request.launch_agents_dir / "com.hermes.connector.plist"
    if host.plist_path != expected_host or connector.plist_path != expected_connector:
        raise ActivationError("old service receipt plist path mismatch")
    if validate_current_plists:
        for service in (host, connector):
            _require_file(service.plist_path, 0o600)
            if _sha256(service.plist_path) != service.plist_sha256:
                raise ActivationError("old service receipt plist digest mismatch")
    return _OldReceipt(str(value["release_id"]), authority, host, connector)


def _validate_backup(backup: Path, old: _OldReceipt) -> None:
    for name, service in (("host.plist", old.host), ("connector.plist", old.connector)):
        path = backup / name
        _require_file(path, 0o600)
        if _sha256(path) != service.plist_sha256:
            raise ActivationError("pending activation backup digest mismatch")


def _parse_old_service(value: Any, *, host: bool) -> _OldService:
    required = (
        {"kind", "label", "plist_path", "plist_sha256", "start_argv"}
        if host
        else {
            "label",
            "plist_path",
            "plist_sha256",
        }
    )
    if not isinstance(value, dict) or set(value) != required:
        raise ActivationError("old service receipt entry is invalid")
    kind = str(value.get("kind", "launchagent"))
    start_argv = tuple(value.get("start_argv", ()))
    if host and kind not in {"desktop", "launchagent"}:
        raise ActivationError("old Host service kind is invalid")
    if kind == "desktop":
        if (
            len(start_argv) != 3
            or start_argv[:2] != ("/usr/bin/open", "-b")
            or not _BUNDLE_ID.fullmatch(start_argv[2])
        ):
            raise ActivationError("Desktop restore argv is not exact")
    elif start_argv:
        raise ActivationError("LaunchAgent restore must use captured plist")
    label = str(value["label"])
    if not _SERVICE_LABEL.fullmatch(label):
        raise ActivationError("old service label is invalid")
    return _OldService(
        kind=kind,
        label=label,
        plist_path=Path(value["plist_path"]),
        plist_sha256=str(value["plist_sha256"]),
        start_argv=start_argv,
    )


def _require_exact_old_authority(
    discovered: tuple[ProcessIdentity, ...], expected: ProcessIdentity
) -> None:
    if len(discovered) != 1:
        raise ActivationError("expected exactly one old Host authority")
    if discovered[0] != expected:
        raise ActivationError("old Host authority does not match service receipt")


def _validate_process_executable(process: ProcessIdentity) -> None:
    path = process.process_executable
    if path.is_symlink() or path.resolve(strict=False) != path:
        raise ActivationError("Host process executable path is unsafe")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ActivationError("Host process executable is missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != process.process_executable_device
        or metadata.st_ino != process.process_executable_inode
    ):
        raise ActivationError("Host process executable identity mismatch")


def _build_plan(
    request: ActivationRequest,
    release: ValidatedRelease,
    old: _OldReceipt,
) -> ActivationPlan:
    host_plist = _candidate_plist(release.host_plist)
    connector_plist = _candidate_plist(release.connector_plist)
    if old.host.kind == "desktop":
        stop_host = SystemCommand(
            "stop-old-host", ("/bin/kill", "-TERM", str(old.authority.pid))
        )
    else:
        stop_host = SystemCommand(
            "stop-old-host",
            ("/bin/launchctl", "bootout", f"{request.domain_target}/{old.host.label}"),
        )
    commands = (
        SystemCommand(
            "stop-old-connector",
            (
                "/bin/launchctl",
                "bootout",
                f"{request.domain_target}/{old.connector.label}",
            ),
        ),
        stop_host,
        SystemCommand(
            "bootstrap-candidate-host",
            (
                "/bin/launchctl",
                "bootstrap",
                request.domain_target,
                str(request.launch_agents_dir / "com.hermes.host.plist"),
            ),
        ),
        SystemCommand(
            "bootstrap-candidate-connector",
            (
                "/bin/launchctl",
                "bootstrap",
                request.domain_target,
                str(request.launch_agents_dir / "com.hermes.connector.plist"),
            ),
        ),
        *_readiness_commands(request, release),
    )
    if host_plist["Label"] == connector_plist["Label"]:
        raise ActivationError("candidate Host and Connector labels must differ")
    return ActivationPlan(
        release.release_id,
        release.release_digest,
        request.profile,
        commands,
    )


def _readiness_commands(
    request: ActivationRequest,
    release: ValidatedRelease,
) -> tuple[SystemCommand, ...]:
    return (
        SystemCommand(
            "connector-status",
            (
                str(
                    release.release_dir
                    / "connector"
                    / "venv"
                    / "bin"
                    / "hermes-connector"
                ),
                "status",
                "--json",
            ),
        ),
        *(
            SystemCommand(f"health-{gate.name}", gate.argv)
            for gate in request.health_gates
        ),
    )


def _validate_candidate_plists(release: ValidatedRelease) -> None:
    host = _candidate_plist(release.host_plist)
    connector = _candidate_plist(release.connector_plist)
    expected_host = [str(release.release_dir / "host" / "venv" / "bin" / "hermes")]
    expected_connector = [
        str(release.release_dir / "connector" / "venv" / "bin" / "hermes-connector"),
        "run",
        "--release-id",
        release.release_id,
    ]
    if host.get("ProgramArguments") != expected_host:
        raise ActivationError("candidate Host plist ProgramArguments mismatch")
    if connector.get("ProgramArguments") != expected_connector:
        raise ActivationError("candidate Connector plist ProgramArguments mismatch")
    expected_environment = {
        "HERMES_PLUGIN_STORE_MANIFEST": str(
            release.release_dir / "plugin" / "metadata" / "signed-plugin-manifest.json"
        ),
        "HERMES_PLUGIN_STORE_TRUST_STORE": str(
            release.release_dir / "plugin" / "metadata" / "trust-store.json"
        ),
    }
    if host.get("EnvironmentVariables") != expected_environment:
        raise ActivationError("candidate Host Plugin Store environment mismatch")


def _candidate_plist(path: Path) -> Mapping[str, Any]:
    _require_file(path, 0o600)
    try:
        value = plistlib.loads(path.read_bytes())
    except Exception as exc:
        raise ActivationError(f"invalid LaunchAgent plist: {path.name}") from exc
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("Label"), str)
        or not _SERVICE_LABEL.fullmatch(value["Label"])
    ):
        raise ActivationError(f"invalid LaunchAgent plist: {path.name}")
    return value


def _validate_health_commands(
    request: ActivationRequest, release: ValidatedRelease
) -> None:
    reviewed = {path.resolve(strict=False) for path in request.reviewed_health_helpers}
    for helper in reviewed:
        _require_executable(helper)
    for gate in request.health_gates:
        if (
            not gate.argv
            or not gate.expected_fields
            or {"ready", "release_id"} & set(gate.expected_fields)
        ):
            raise ActivationError("health gate command and task fields are required")
        executable = Path(gate.argv[0])
        allowed_in_release = any(
            _is_within(executable, release.release_dir / runtime / "venv")
            for runtime in ("host", "connector")
        )
        if executable.resolve(strict=False) not in reviewed and not allowed_in_release:
            raise ActivationError("health command executable is not allowlisted")
        _require_executable(executable)
        _reject_secret_argv(gate.argv)


def _validate_command(
    request: ActivationRequest,
    release: ValidatedRelease,
    command: SystemCommand,
) -> None:
    if not command.argv or any(
        not isinstance(item, str) or "\x00" in item for item in command.argv
    ):
        raise ActivationError("system command argv is invalid")
    _reject_secret_argv(command.argv)
    executable = command.argv[0]
    if executable == "/bin/launchctl":
        if command.argv[1:2] == ("bootout",):
            if (
                len(command.argv) != 3
                or not command.argv[2].startswith(request.domain_target + "/")
                or not _SERVICE_LABEL.fullmatch(
                    command.argv[2].removeprefix(request.domain_target + "/")
                )
            ):
                raise ActivationError("service command is outside allowlist")
        elif command.argv[1:2] == ("bootstrap",):
            allowed_plists = {
                request.launch_agents_dir / "com.hermes.host.plist",
                request.launch_agents_dir / "com.hermes.connector.plist",
            }
            if (
                len(command.argv) != 4
                or command.argv[2] != request.domain_target
                or Path(command.argv[3]) not in allowed_plists
            ):
                raise ActivationError("service command is outside allowlist")
        else:
            raise ActivationError("service command is outside allowlist")
    elif executable == "/bin/kill":
        if command.argv[1:2] != ("-TERM",) or len(command.argv) != 3:
            raise ActivationError("process stop command is outside allowlist")
    elif executable == "/usr/bin/open":
        if command.argv[1:2] != ("-b",):
            raise ActivationError("Desktop restore command is outside allowlist")
    else:
        allowed = {
            release.release_dir / "connector" / "venv" / "bin" / "hermes-connector",
            *(path.resolve(strict=False) for path in request.reviewed_health_helpers),
        }
        if Path(executable).resolve(strict=False) not in allowed and not any(
            _is_within(Path(executable), release.release_dir / runtime / "venv")
            for runtime in ("host", "connector")
        ):
            raise ActivationError("command executable is outside allowlist")


def _validate_candidate_runtime(
    runtime: HostRuntimeEvidence,
    candidate_hosts: tuple[ProcessIdentity, ...],
    request: ActivationRequest,
    release: ValidatedRelease,
) -> None:
    process = runtime.process
    if candidate_hosts != (process,):
        raise ActivationError("candidate Host authority is not unique")
    _validate_process_executable(process)
    if process.profile != request.profile or process.hermes_home != request.hermes_home:
        raise ActivationError("candidate Host authority scope mismatch")
    if not _is_within(
        process.process_executable, release.release_dir / "host" / "venv"
    ):
        raise ActivationError("candidate Host executable is outside release venv")
    metadata = process.process_executable.stat()
    if (
        metadata.st_dev != process.process_executable_device
        or metadata.st_ino != process.process_executable_inode
    ):
        raise ActivationError("candidate Host executable identity mismatch")
    expected_manifest = (
        release.release_dir / "plugin" / "metadata" / "signed-plugin-manifest.json"
    )
    expected_trust = release.release_dir / "plugin" / "metadata" / "trust-store.json"
    if (
        not runtime.plugin_store_active
        or runtime.plugin_manifest_path != expected_manifest
        or runtime.trust_store_path != expected_trust
    ):
        raise ActivationError("candidate Plugin Store is not active")
    if {item.role for item in runtime.descriptors} != {
        "local",
        "control",
        "observer",
    } or len(runtime.descriptors) != 3:
        raise ActivationError("candidate role descriptor set is incomplete")
    for descriptor in runtime.descriptors:
        if (
            descriptor.pid != process.pid
            or descriptor.process_start_time_ns != process.process_start_time_ns
            or descriptor.process_executable != process.process_executable
            or descriptor.process_executable_device != process.process_executable_device
            or descriptor.process_executable_inode != process.process_executable_inode
            or descriptor.authority_id != process.authority_id
            or descriptor.runtime_generation != process.runtime_generation
            or descriptor.instance_id != process.instance_id
            or descriptor.host_bundle_id != process.host_bundle_id
            or descriptor.peer_pid != process.pid
            or not descriptor.is_socket
        ):
            raise ActivationError("role descriptor authority mismatch")
        try:
            socket_metadata = descriptor.socket_path.stat()
        except OSError as exc:
            raise ActivationError("role descriptor UDS is missing") from exc
        if (
            not stat.S_ISSOCK(socket_metadata.st_mode)
            or socket_metadata.st_dev != descriptor.socket_device
            or socket_metadata.st_ino != descriptor.socket_inode
        ):
            raise ActivationError("role descriptor UDS identity mismatch")


def _validate_connector_status(
    result: CommandResult,
    release_id: str,
    process: ProcessIdentity,
) -> None:
    value = _json_object(result.stdout, "Connector status")
    if set(value) != _STATUS_FIELDS:
        raise ActivationError("Connector status fields are invalid")
    authority = value["local_authority_identity"]
    if not isinstance(authority, dict) or set(authority) != _AUTHORITY_FIELDS:
        raise ActivationError("Connector status authority fields are invalid")
    if (
        result.exit_code != 0
        or value["ready"] is not True
        or value["release_id"] != release_id
        or value["cloud_state"] != "active"
        or not isinstance(value["pid"], int)
        or value["pid"] <= 0
        or not isinstance(value["process_start_time_ns"], int)
        or value["process_start_time_ns"] <= 0
        or not isinstance(value["process_executable_device"], int)
        or value["process_executable_device"] < 0
        or not isinstance(value["process_executable_inode"], int)
        or value["process_executable_inode"] <= 0
        or not isinstance(value["runtime_generation"], int)
        or value["runtime_generation"] <= 0
        or value["runtime_generation"] != process.runtime_generation
        or not isinstance(value["process_executable"], str)
        or not isinstance(value["updated_at"], str)
        or any(
            not isinstance(authority[field], str) or not authority[field]
            for field in _AUTHORITY_FIELDS
        )
        or authority
        != {
            "profile": process.profile,
            "instance_id": process.instance_id,
            "host_bundle_id": process.host_bundle_id,
        }
    ):
        raise ActivationError("Connector is not ready for candidate release")


def _validate_health(result: CommandResult, release_id: str, gate: HealthGate) -> None:
    value = _json_object(result.stdout, f"{gate.name} health")
    expected_keys = {"ready", "release_id", *gate.expected_fields.keys()}
    if set(value) != expected_keys:
        raise ActivationError(f"{gate.name} health fields are invalid")
    if (
        result.exit_code != 0
        or value["ready"] is not True
        or value["release_id"] != release_id
        or any(
            value.get(key) != expected for key, expected in gate.expected_fields.items()
        )
    ):
        raise ActivationError(f"{gate.name} health gate is not ready")


def _json_object(raw: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActivationError(f"{label} did not return JSON") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{label} did not return a JSON object")
    return value


def _read_pending(
    path: Path,
    release: ValidatedRelease,
    request: ActivationRequest,
) -> Mapping[str, Any]:
    _require_file(path, 0o600)
    value = _json_object(path.read_text(encoding="utf-8"), "pending activation receipt")
    if set(value) != {
        "schema_version",
        "release_id",
        "release_digest",
        "profile",
        "phase",
        "attempt_dir",
        "old_authority",
    }:
        raise ActivationError("pending activation receipt fields are invalid")
    if (
        value["schema_version"] != 1
        or value["release_id"] != release.release_id
        or value["release_digest"] != release.release_digest
        or value["profile"] != request.profile
    ):
        raise ActivationError("pending activation receipt does not match request")
    attempt_dir = Path(value["attempt_dir"])
    activation_root = _activation_paths(request)["activation"]
    if not _is_within(attempt_dir, activation_root):
        raise ActivationError("pending activation attempt path is unsafe")
    return value


def _read_active(path: Path) -> Mapping[str, Any]:
    _require_file(path, 0o600)
    value = _json_object(path.read_text(encoding="utf-8"), "active receipt")
    expected = {
        "schema_version",
        "release_id",
        "release_digest",
        "profile",
        "authority",
        "plugin_store_root",
        "commands",
        "status",
    }
    if set(value) != expected or value["schema_version"] != 1:
        raise ActivationError("active receipt fields are invalid")
    return value


def _copy_private(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{destination.name}.tmp.{uuid.uuid4().hex}"
    with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _atomic_install(source: Path, destination: Path) -> None:
    _require_file(source, None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp.{uuid.uuid4().hex}"
    with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = path.parent / f".{path.name}.tmp.{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _update_pending(path: Path, phase: str) -> None:
    value = _json_object(path.read_text(encoding="utf-8"), "pending activation receipt")
    updated = dict(value)
    updated["phase"] = phase
    _write_json(path, updated)


def _remove_file(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        path.unlink()
        _fsync_directory(path.parent)


def _require_file(path: Path, mode: int | None) -> None:
    if path.is_symlink():
        raise ActivationError(f"symlink file rejected: {path}")
    for candidate in (path.parent.absolute(), *path.parent.absolute().parents):
        if candidate.is_symlink():
            raise ActivationError(f"symlink path component rejected: {candidate}")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise ActivationError(f"required file is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ActivationError(f"required path is not a regular file: {path}")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise ActivationError(f"required file mode must be {mode:04o}: {path}")


def _require_executable(path: Path) -> None:
    _require_file(path, None)
    if not os.access(path, os.X_OK) or path.resolve(strict=False) != path:
        raise ActivationError(
            f"health executable is not canonical and executable: {path}"
        )


def _reject_secret_argv(argv: tuple[str, ...]) -> None:
    for value in argv[1:]:
        lowered = value.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            raise ActivationError("secret-like values are forbidden in activation argv")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
