#!/usr/bin/env python3
"""Execute one reviewed command with candidate-bound SQLite environment."""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_RESULT_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_SEED_KEYS = (
    "HERMES_SEED_TENANT_SLUG",
    "HERMES_SEED_TENANT_DISPLAY_NAME",
    "HERMES_SEED_USERNAME",
    "HERMES_SEED_USER_DISPLAY_NAME",
    "HERMES_SEED_WORKSPACE_KEY",
    "HERMES_SEED_WORKSPACE_DISPLAY_NAME",
    "HERMES_SEED_OWNER_CONTROL_ENABLED",
    "HERMES_SEED_AGENT_KEY",
    "HERMES_SEED_DEVICE_KEY",
)
_ENVIRONMENT_FILE_KEYS = frozenset(
    {
        "HERMES_DEPLOY_ROOT",
        "HERMES_RELEASES_DIR",
        "HERMES_PREVIOUS",
        "HERMES_BUSINESS_API_BIND",
        "HERMES_BUSINESS_API_PORT",
        "HERMES_CONNECTOR_GATEWAY_BIND",
        "HERMES_CONNECTOR_GATEWAY_PORT",
        "HERMES_CONNECTOR_TOKEN_TENANT_ID",
        "HERMES_CONNECTOR_TOKEN_DEVICE_ID",
        "HERMES_CONNECTOR_TOKEN_TTL_SECONDS",
        "HERMES_CONNECTOR_TOKEN_OUTPUT",
        *_SEED_KEYS,
    }
)
_IGNORED_REBOUND_KEYS = frozenset({"HERMES_CURRENT", "HERMES_VENV"})
_EXPECTED_GROUP = "hermes-cloud"
_SUBJECT_USERS = {
    "migration": "hermes-cloud-migrate",
    "runtime": "hermes-cloud",
}
_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SAFE_ENVIRONMENT = {
    "PATH": _SAFE_PATH,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TMPDIR": "/tmp",
    "PYTHONNOUSERSITE": "1",
}
_EXPECTATIONS = {
    "cleanup-plan": {
        "cleanup_mode": "plan",
        "status": "ready",
        "schema_version": "10",
        "sessions": "1",
        "messages": "2",
        "events": "1",
        "cursors": "1",
        "tickets": "1",
    },
    "cleanup-apply": {
        "cleanup_mode": "apply",
        "status": "removed",
        "schema_version": "10",
        "sessions": "1",
        "messages": "2",
        "events": "1",
        "cursors": "1",
        "tickets": "1",
    },
    "cleanup-absent": {
        "cleanup_mode": "plan",
        "status": "absent",
        "schema_version": "10",
        "sessions": "0",
        "messages": "0",
        "events": "0",
        "cursors": "0",
        "tickets": "0",
    },
    "migration-plan": {
        "sqlite_migration_mode": "plan",
        "table_count": "38",
        "schema_version": "11",
        "historical_source_count": "10",
        "source": "versioned-10",
        "recent_two_covered": "true",
    },
    "migration-apply": {
        "sqlite_migration_mode": "apply",
        "table_count": "38",
        "database_existing": "true",
        "schema_version": "11",
        "source": "versioned-10",
        "recent_two_covered": "true",
    },
    "migration-current": {
        "sqlite_migration_mode": "plan",
        "table_count": "38",
        "schema_version": "11",
        "historical_source_count": "10",
        "source": "current",
        "recent_two_covered": "true",
    },
}


class CandidateCommandError(RuntimeError):
    """Raised when a command or environment is outside the reviewed contract."""


def require_execution_identity(subject: str = "migration") -> str:
    expected_user = _SUBJECT_USERS.get(subject)
    if expected_user is None:
        raise CandidateCommandError
    try:
        user_record = pwd.getpwuid(os.geteuid())
        user = user_record.pw_name
        primary_group = grp.getgrgid(os.getegid()).gr_name
        supplementary_groups = {grp.getgrgid(group_id).gr_name for group_id in os.getgroups()}
    except (KeyError, OSError):
        raise CandidateCommandError from None
    if (
        user != expected_user
        or primary_group != _EXPECTED_GROUP
        or _EXPECTED_GROUP not in supplementary_groups | {primary_group}
    ):
        raise CandidateCommandError
    home = Path(getattr(user_record, "pw_dir", "/"))
    try:
        metadata = home.stat()
    except OSError:
        return "/"
    if (
        not home.is_absolute()
        or home.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        return "/"
    return str(home)


def require_readable_reference(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise CandidateCommandError
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
    except OSError:
        raise CandidateCommandError from None
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise CandidateCommandError
    return str(path)


def require_candidate_executable(candidate: Path, executable: Path) -> str:
    if (
        not executable.is_absolute()
        or ".." in executable.parts
        or not executable.is_relative_to(candidate)
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise CandidateCommandError
    relative = executable.relative_to(candidate)
    current = candidate
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir() or not os.access(current, os.X_OK):
            raise CandidateCommandError
    return str(executable)


def require_candidate_readable(candidate: Path, source: Path) -> str:
    if (
        not source.is_absolute()
        or ".." in source.parts
        or not source.is_relative_to(candidate)
        or source.is_symlink()
        or not source.is_file()
        or not os.access(source, os.R_OK)
    ):
        raise CandidateCommandError
    relative = source.relative_to(candidate)
    current = candidate
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or not current.is_dir() or not os.access(current, os.X_OK):
            raise CandidateCommandError
    return str(source)


def mode_allows(
    metadata: os.stat_result,
    *,
    effective_uid: int,
    group_ids: set[int],
    permission: int,
) -> bool:
    if effective_uid == 0:
        return True
    if effective_uid == metadata.st_uid:
        shift = 6
    elif metadata.st_gid in group_ids:
        shift = 3
    else:
        shift = 0
    required = 0
    if permission & os.R_OK:
        required |= 0o4
    if permission & os.W_OK:
        required |= 0o2
    if permission & os.X_OK:
        required |= 0o1
    return ((stat.S_IMODE(metadata.st_mode) >> shift) & required) == required


def _key_value_result(payload: str) -> dict[str, str]:
    if not payload.endswith("\n") or len(payload.splitlines()) != 1:
        raise CandidateCommandError
    result: dict[str, str] = {}
    for token in payload[:-1].split(" "):
        if token.count("=") != 1:
            raise CandidateCommandError
        name, value = token.split("=", 1)
        if not _RESULT_NAME.fullmatch(name) or not value or name in result:
            raise CandidateCommandError
        result[name] = value
    return result


def parse_expected_result(expectation: str, payload: str) -> dict[str, str]:
    expected = _EXPECTATIONS.get(expectation)
    if expected is None:
        raise CandidateCommandError
    result = _key_value_result(payload)
    expected_keys = set(expected)
    if expectation.startswith("cleanup-"):
        expected_keys.add("session_id")
    if set(result) != expected_keys or any(result.get(name) != value for name, value in expected.items()):
        raise CandidateCommandError
    if expectation.startswith("cleanup-"):
        try:
            session_id = UUID(result["session_id"])
        except (ValueError, AttributeError):
            raise CandidateCommandError from None
        if str(session_id) != result["session_id"]:
            raise CandidateCommandError
    return {"expectation": expectation, "status": "PASS"}


def _decode_systemd_value(raw: str) -> str:
    if "\x00" in raw or raw.endswith("\\"):
        raise CandidateCommandError
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace = ""
    lexer.commenters = ""
    try:
        return "".join(lexer)
    except ValueError:
        raise CandidateCommandError from None


def _read_environment_file(path: Path) -> dict[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CandidateCommandError
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise CandidateCommandError from None
    if len(payload) > 64 * 1024:
        raise CandidateCommandError
    result: dict[str, str] = {}
    seen: set[str] = set()
    for source_line in payload.splitlines():
        line = source_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise CandidateCommandError
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _NAME.fullmatch(name) or name in seen:
            raise CandidateCommandError
        seen.add(name)
        if name in _IGNORED_REBOUND_KEYS:
            continue
        if name.startswith("HERMES_") and name not in _ENVIRONMENT_FILE_KEYS:
            raise CandidateCommandError
        if name in _ENVIRONMENT_FILE_KEYS:
            result[name] = _decode_systemd_value(raw_value.strip())
    return result


def _absolute_reference(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CandidateCommandError
    return str(path)


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="execute one command with a candidate-bound SQLite environment"
    )
    parser.add_argument("--environment-file", required=True, type=Path)
    parser.add_argument("--candidate-release", required=True, type=Path)
    parser.add_argument("--subject", required=True, choices=tuple(_SUBJECT_USERS))
    parser.add_argument("--purpose", required=True, choices=("validate", "cleanup", "migration"))
    parser.add_argument("--bootstrap-dsn-file")
    parser.add_argument("--migration-dsn-file")
    parser.add_argument("--runtime-dsn-file")
    parser.add_argument("--observer-keyring-file")
    parser.add_argument("--required-executable", action="append", default=[])
    parser.add_argument("--required-readable", action="append", default=[])
    parser.add_argument("--require-seed-selectors", action="store_true")
    parser.add_argument("--expect", choices=tuple(_EXPECTATIONS))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def validate_subject_contract(arguments: argparse.Namespace) -> None:
    if arguments.subject == "runtime":
        if (
            arguments.purpose != "validate"
            or arguments.runtime_dsn_file is None
            or arguments.bootstrap_dsn_file is not None
            or arguments.migration_dsn_file is not None
            or arguments.observer_keyring_file is not None
            or arguments.require_seed_selectors
            or arguments.expect is not None
        ):
            raise CandidateCommandError
    elif arguments.runtime_dsn_file is not None:
        raise CandidateCommandError


def _command_environment(arguments: argparse.Namespace) -> tuple[dict[str, str], tuple[str, ...]]:
    validate_subject_contract(arguments)
    safe_home = require_execution_identity(arguments.subject)
    candidate = arguments.candidate_release
    if (
        not candidate.is_absolute()
        or candidate.is_symlink()
        or not candidate.is_dir()
        or candidate != candidate.resolve(strict=True)
    ):
        raise CandidateCommandError
    values = _read_environment_file(arguments.environment_file)
    if arguments.subject == "runtime":
        values = {}
    if arguments.require_seed_selectors and any(not values.get(name) for name in _SEED_KEYS):
        raise CandidateCommandError
    required_references = {
        "cleanup": ("bootstrap_dsn_file",),
        "migration": ("migration_dsn_file", "observer_keyring_file"),
        "validate": (),
    }[arguments.purpose]
    if any(getattr(arguments, name) is None for name in required_references):
        raise CandidateCommandError
    reference_options = {
        "bootstrap_dsn_file": "HERMES_BOOTSTRAP_DSN_FILE",
        "migration_dsn_file": "HERMES_MIGRATION_DSN_FILE",
        "runtime_dsn_file": "HERMES_RUNTIME_DSN_FILE",
        "observer_keyring_file": "HERMES_OBSERVER_KEYRING_FILE",
    }
    values.update({"HERMES_CURRENT": str(candidate), "HERMES_VENV": str(candidate / ".venv")})
    for option, environment_name in reference_options.items():
        raw_value = getattr(arguments, option)
        if raw_value is not None:
            path = Path(_absolute_reference(raw_value))
            values[environment_name] = require_readable_reference(path)
    for raw_executable in arguments.required_executable:
        require_candidate_executable(candidate, Path(raw_executable))
    for raw_source in arguments.required_readable:
        require_candidate_readable(candidate, Path(raw_source))
    command = tuple(arguments.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command or not Path(command[0]).is_absolute():
        raise CandidateCommandError
    candidate_prefix = f"{candidate}{os.sep}"
    for argument in command:
        if argument.startswith(os.sep) and not argument.startswith(candidate_prefix):
            raise CandidateCommandError
    if not os.access(command[0], os.X_OK):
        raise CandidateCommandError
    environment = {**_SAFE_ENVIRONMENT, "HOME": safe_home}
    environment.update(values)
    return environment, command


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _arguments(argv)
        environment, command = _command_environment(arguments)
        if arguments.expect is None:
            os.execvpe(command[0], command, environment)
        completed = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or completed.stderr:
            raise CandidateCommandError
        result = parse_expected_result(arguments.expect, completed.stdout)
        print(
            f"candidate_expectation={result['expectation']} status={result['status']}"
        )
    except (CandidateCommandError, OSError, ValueError, subprocess.SubprocessError):
        print("candidate command rejected", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
