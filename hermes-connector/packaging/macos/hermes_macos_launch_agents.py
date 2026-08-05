"""Render private, version-pinned macOS service definitions for a built release."""

from __future__ import annotations

import os
import plistlib
import re
from collections.abc import Mapping
from pathlib import Path

_VERSIONED_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESERVED_RELEASE_NAMES = {"current", "latest", "stable", ".", ".."}


class LaunchAgentError(ValueError):
    """A service definition would not pin an immutable release."""


def render_release_launch_agents(release_dir: Path) -> Mapping[str, bytes]:
    release_dir = _validate_release_dir(release_dir)
    logs = release_dir / "receipts" / "logs"
    return {
        "host": _render(
            label=f"com.hermes.host.{release_dir.name}",
            arguments=(str(release_dir / "host" / "venv" / "bin" / "hermes"),),
            stdout=logs / "host.stdout.log",
            stderr=logs / "host.stderr.log",
            environment={
                "HERMES_PLUGIN_STORE_MANIFEST": str(
                    release_dir / "plugin" / "metadata" / "signed-plugin-manifest.json"
                ),
                "HERMES_PLUGIN_STORE_TRUST_STORE": str(
                    release_dir / "plugin" / "metadata" / "trust-store.json"
                ),
            },
        ),
        "connector": _render(
            label=f"com.hermes.connector.{release_dir.name}",
            arguments=(
                str(release_dir / "connector" / "venv" / "bin" / "hermes-connector"),
                "run",
                "--release-id",
                release_dir.name,
            ),
            stdout=logs / "connector.stdout.log",
            stderr=logs / "connector.stderr.log",
            environment=None,
        ),
    }


def write_release_launch_agents(release_dir: Path) -> Mapping[str, Path]:
    release_dir = _validate_release_dir(release_dir)
    services = release_dir / "services"
    if not services.is_dir() or services.is_symlink():
        raise LaunchAgentError("release services directory is missing or unsafe")
    rendered = render_release_launch_agents(release_dir)
    paths: dict[str, Path] = {}
    for name, payload in rendered.items():
        path = services / f"com.hermes.{name}.plist"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        paths[name] = path
    return paths


def _validate_release_dir(release_dir: Path) -> Path:
    release_dir = Path(release_dir)
    if (
        not release_dir.is_absolute()
        or not _VERSIONED_RELEASE.fullmatch(release_dir.name)
        or release_dir.name.lower() in _RESERVED_RELEASE_NAMES
        or not any(character.isdigit() for character in release_dir.name)
    ):
        raise LaunchAgentError("release directory must be a versioned absolute path")
    for candidate in (release_dir, *release_dir.parents):
        if candidate.is_symlink():
            raise LaunchAgentError("release directory must not contain symlinks")
    return release_dir


def _render(
    *,
    label: str,
    arguments: tuple[str, ...],
    stdout: Path,
    stderr: Path,
    environment: Mapping[str, str] | None,
) -> bytes:
    payload = {
        "Label": label,
        "ProgramArguments": list(arguments),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout),
        "StandardErrorPath": str(stderr),
    }
    if environment is not None:
        payload["EnvironmentVariables"] = dict(environment)
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
