"""Target-specific runtime dependency plan derived from the authoritative uv locks.

The universal uv lock remains the audit source. Qualification CI exports the exact
runtime requirements for one OS/architecture and binds those hash-pinned requirement
files to the verified wheelhouse. Customer assembly consumes this plan offline instead
of re-solving the universal lock on a single target machine.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.txt\Z")
_PLAN_FIELDS = {
    "schema_version",
    "target",
    "platform",
    "architecture",
    "python_tag",
    "wheelhouse_manifest_sha256",
    "locks",
    "requirements",
}
_REQUIREMENT_FIELDS = {"filename", "sha256", "size_bytes"}
_MAX_PLAN_BYTES = 128 * 1024
_MAX_REQUIREMENTS_BYTES = 4 * 1024 * 1024


class TargetRuntimePlanError(RuntimeError):
    """The per-target dependency installation plan is not trustworthy."""


@dataclass(frozen=True)
class TargetRequirementsV1:
    filename: str
    sha256: str
    size_bytes: int
    path: Path


@dataclass(frozen=True)
class VerifiedTargetRuntimePlanV1:
    root: Path
    plan_path: Path
    plan_sha256: str
    target: str
    platform: str
    architecture: str
    python_tag: str
    wheelhouse_manifest_sha256: str
    locks: Mapping[str, str]
    requirements: Mapping[str, TargetRequirementsV1]

    def require_lock(self, name: str, sha256: str) -> None:
        if self.locks.get(name) != sha256:
            raise TargetRuntimePlanError(f"target runtime plan lock mismatch: {name}")

    def requirement(self, name: str) -> TargetRequirementsV1:
        value = self.requirements.get(name)
        if value is None:
            raise TargetRuntimePlanError(f"target runtime requirements are missing: {name}")
        return value


def load_verified_target_runtime_plan(
    root: Path,
    *,
    expected_wheelhouse_manifest_sha256: str,
    plan_name: str = "RUNTIME-INSTALL-PLAN.json",
) -> VerifiedTargetRuntimePlanV1:
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise TargetRuntimePlanError("target runtime plan root is invalid")
    if not _SHA256.fullmatch(expected_wheelhouse_manifest_sha256):
        raise TargetRuntimePlanError("expected wheelhouse manifest SHA-256 is invalid")

    plan_path = root / plan_name
    raw_bytes = _read_regular(plan_path, _MAX_PLAN_BYTES)
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetRuntimePlanError("target runtime plan is invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != _PLAN_FIELDS or raw.get("schema_version") != 1:
        raise TargetRuntimePlanError("target runtime plan schema is invalid")

    for field in ("target", "platform", "architecture", "python_tag"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 64:
            raise TargetRuntimePlanError(f"target runtime plan {field} is invalid")

    manifest_sha = raw.get("wheelhouse_manifest_sha256")
    if manifest_sha != expected_wheelhouse_manifest_sha256:
        raise TargetRuntimePlanError("target runtime plan wheelhouse binding mismatch")

    locks_raw = raw.get("locks")
    if not isinstance(locks_raw, dict) or set(locks_raw) != {"core", "connector"}:
        raise TargetRuntimePlanError("target runtime plan locks are invalid")
    locks: dict[str, str] = {}
    for name, digest in locks_raw.items():
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise TargetRuntimePlanError(f"target runtime plan lock digest is invalid: {name}")
        locks[name] = digest

    requirements_raw = raw.get("requirements")
    if not isinstance(requirements_raw, dict) or set(requirements_raw) != {"core", "connector"}:
        raise TargetRuntimePlanError("target runtime requirements map is invalid")
    requirements: dict[str, TargetRequirementsV1] = {}
    for name, item in requirements_raw.items():
        if not isinstance(item, dict) or set(item) != _REQUIREMENT_FIELDS:
            raise TargetRuntimePlanError(f"target runtime requirements entry is invalid: {name}")
        filename = item.get("filename")
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if not isinstance(filename, str) or _SAFE_FILE.fullmatch(filename) is None:
            raise TargetRuntimePlanError(f"target runtime requirements filename is invalid: {name}")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise TargetRuntimePlanError(f"target runtime requirements digest is invalid: {name}")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= _MAX_REQUIREMENTS_BYTES:
            raise TargetRuntimePlanError(f"target runtime requirements size is invalid: {name}")
        path = root / filename
        payload = _read_regular(path, _MAX_REQUIREMENTS_BYTES)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise TargetRuntimePlanError(f"target runtime requirements integrity failed: {name}")
        requirements[name] = TargetRequirementsV1(filename, digest, size, path)

    return VerifiedTargetRuntimePlanV1(
        root=root,
        plan_path=plan_path,
        plan_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        target=raw["target"],
        platform=raw["platform"],
        architecture=raw["architecture"],
        python_tag=raw["python_tag"],
        wheelhouse_manifest_sha256=manifest_sha,
        locks=MappingProxyType(locks),
        requirements=MappingProxyType(requirements),
    )


def _read_regular(path: Path, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise TargetRuntimePlanError(f"target runtime plan file is missing or symlinked: {path.name}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum_bytes:
        raise TargetRuntimePlanError(f"target runtime plan file is not bounded: {path.name}")
    payload = path.read_bytes()
    after = path.stat()
    if (
        after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
        or after.st_ino != metadata.st_ino
        or len(payload) != metadata.st_size
    ):
        raise TargetRuntimePlanError(f"target runtime plan file changed during verification: {path.name}")
    return payload
