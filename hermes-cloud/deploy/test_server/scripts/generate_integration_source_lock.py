"""Generate the declared integration source lock and Host SPI provenance binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

INTEGRATION_SOURCE_ROOTS = (
    PurePosixPath("hermes-agent-plugin/src"),
    PurePosixPath("hermes-connector/src"),
)
INTEGRATION_SOURCE_EXACT_FILES = frozenset(
    {
        PurePosixPath("tests/__init__.py"),
        PurePosixPath("tests/e2e/control_pipeline/__init__.py"),
        PurePosixPath("tests/e2e/control_pipeline/harness.py"),
        PurePosixPath("tests/e2e/plugin_test_runtime.py"),
        PurePosixPath("tests/test_support/__init__.py"),
        PurePosixPath("tests/test_support/host_spi_v1.py"),
        PurePosixPath("upstream/hermes-core-host-spi-v1/upstream.lock.json"),
        PurePosixPath(
            "upstream/hermes-core-host-spi-v1/patches/"
            "0001-gateway-extension-host-spi-v1-stage1.patch"
        ),
    }
)
INTEGRATION_SOURCE_LOCK = PurePosixPath(
    "hermes-cloud/deploy/test_server/integration-source-lock.json"
)
HOST_SPI_PROVENANCE = PurePosixPath(
    "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1/PROVENANCE.json"
)
HOST_SPI_FIXTURE_ROOT = PurePosixPath(
    "hermes-cloud/tests/fixtures/hermes_core_host_spi_v1"
)
UPSTREAM_LOCK = PurePosixPath(
    "upstream/hermes-core-host-spi-v1/upstream.lock.json"
)
STAGE1_PATCH = PurePosixPath(
    "upstream/hermes-core-host-spi-v1/patches/"
    "0001-gateway-extension-host-spi-v1-stage1.patch"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_Replace = Callable[[Path, Path], None]


class SourceLockError(RuntimeError):
    """The source declaration, fixture evidence, or update transaction is invalid."""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceLockError("JSON contains duplicate keys")
        result[key] = value
    return result


def _parse_json(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (SourceLockError, UnicodeError, json.JSONDecodeError):
        raise SourceLockError(f"{label} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise SourceLockError(f"{label} must be a JSON object")
    return parsed


def _workspace_path(workspace_root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise SourceLockError("declared source path is unsafe")
    return workspace_root.joinpath(*relative.parts)


def _reject_symlink_chain(workspace_root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(workspace_root)
    except ValueError:
        raise SourceLockError("declared source escapes the workspace") from None
    current = workspace_root
    if current.is_symlink() or not current.is_dir():
        raise SourceLockError("workspace root is not a regular directory")
    for component in relative.parts:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError:
            raise SourceLockError(f"declared source is missing: {relative.as_posix()}") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise SourceLockError(f"declared source is a symlink: {relative.as_posix()}")


def _read_regular_file(workspace_root: Path, relative: PurePosixPath) -> bytes:
    target = _workspace_path(workspace_root, relative)
    _reject_symlink_chain(workspace_root, target)
    before = os.lstat(target)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SourceLockError(f"declared source is not a regular file: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError:
        raise SourceLockError(f"declared source cannot be read: {relative}") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SourceLockError(f"declared source changed while reading: {relative}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise SourceLockError(f"declared source changed while reading: {relative}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _declared_source_paths(workspace_root: Path) -> tuple[PurePosixPath, ...]:
    paths = set(INTEGRATION_SOURCE_EXACT_FILES)
    for relative_root in INTEGRATION_SOURCE_ROOTS:
        source_root = _workspace_path(workspace_root, relative_root)
        _reject_symlink_chain(workspace_root, source_root)
        if not source_root.is_dir():
            raise SourceLockError(f"declared source root is not a directory: {relative_root}")
        for candidate in source_root.rglob("*"):
            relative = PurePosixPath(candidate.relative_to(workspace_root).as_posix())
            if "__pycache__" in relative.parts or candidate.suffix in {".pyc", ".pyo"}:
                continue
            if candidate.is_symlink():
                raise SourceLockError(f"declared source is a symlink: {relative}")
            if candidate.is_file():
                paths.add(relative)
            elif not candidate.is_dir():
                raise SourceLockError(f"declared source has unsupported type: {relative}")
    return tuple(sorted(paths, key=PurePosixPath.as_posix))


def _extract_new_file_from_patch(patch: str, target: str) -> bytes:
    header = f"diff --git a/{target} b/{target}\n"
    try:
        start = patch.index(header)
        end = patch.find("\ndiff --git ", start + len(header))
        section = patch[start:] if end == -1 else patch[start : end + 1]
        if "new file mode 100644\n" not in section:
            raise SourceLockError("stage1 patch fixture is not a new regular file")
        hunk = section.index("@@ -0,0 ")
    except ValueError:
        raise SourceLockError("stage1 patch does not contain the declared fixture") from None
    payload_lines: list[str] = []
    for line in section[hunk:].splitlines()[1:]:
        if line.startswith("+"):
            payload_lines.append(line[1:])
        elif line == "\\ No newline at end of file":
            continue
        elif line.startswith("diff --git "):
            break
        else:
            raise SourceLockError("stage1 patch fixture has an unsupported hunk")
    return ("\n".join(payload_lines) + "\n").encode("utf-8")


def _validated_provenance_update(workspace_root: Path) -> bytes:
    provenance_payload = _read_regular_file(workspace_root, HOST_SPI_PROVENANCE)
    provenance = _parse_json(provenance_payload, label="Host SPI provenance")
    if set(provenance) != {
        "schema_version",
        "source_scope",
        "upstream",
        "stage1_patch",
        "files",
    } or provenance.get("schema_version") != 1 or provenance.get(
        "source_scope"
    ) != "hermes-core-host-spi-stage1-fixture":
        raise SourceLockError("Host SPI provenance contract is invalid")

    upstream = provenance.get("upstream")
    patch_record = provenance.get("stage1_patch")
    files = provenance.get("files")
    if (
        not isinstance(upstream, dict)
        or set(upstream)
        != {"repository", "version", "commit", "lock_path", "lock_sha256"}
        or upstream.get("lock_path") != UPSTREAM_LOCK.as_posix()
        or not isinstance(upstream.get("lock_sha256"), str)
        or _SHA256.fullmatch(upstream["lock_sha256"]) is None
        or not isinstance(patch_record, dict)
        or set(patch_record) != {"path", "sha256"}
        or patch_record.get("path") != STAGE1_PATCH.as_posix()
        or not isinstance(patch_record.get("sha256"), str)
        or _SHA256.fullmatch(patch_record["sha256"]) is None
        or not isinstance(files, list)
        or len(files) != 1
    ):
        raise SourceLockError("Host SPI provenance evidence is invalid")

    file_record = files[0]
    if (
        not isinstance(file_record, dict)
        or set(file_record) != {"path", "extracted_from", "sha256"}
        or file_record.get("path") != "hermes_cli/extension_host_v1.py"
        or file_record.get("extracted_from") != "hermes_cli/extension_host_v1.py"
        or not isinstance(file_record.get("sha256"), str)
        or _SHA256.fullmatch(file_record["sha256"]) is None
    ):
        raise SourceLockError("Host SPI fixture declaration is invalid")

    upstream_lock_payload = _read_regular_file(workspace_root, UPSTREAM_LOCK)
    upstream_lock = _parse_json(upstream_lock_payload, label="upstream lock")
    locked_upstream = upstream_lock.get("upstream")
    patches = upstream_lock.get("patches")
    expected_upstream = {
        "distribution": "hermes-agent",
        "repository": upstream["repository"],
        "version": upstream["version"],
        "commit": upstream["commit"],
    }
    if locked_upstream != expected_upstream or not isinstance(patches, list) or not patches:
        raise SourceLockError("upstream lock does not match Host SPI provenance")
    first_patch = patches[0]
    expected_patch_path = (
        UPSTREAM_LOCK.parent / str(first_patch.get("path", ""))
        if isinstance(first_patch, Mapping)
        else PurePosixPath()
    )
    if (
        not isinstance(first_patch, dict)
        or set(first_patch) != {"path", "sha256"}
        or expected_patch_path != STAGE1_PATCH
        or first_patch.get("sha256") != patch_record["sha256"]
    ):
        raise SourceLockError("upstream lock does not bind the stage1 patch")

    patch_payload = _read_regular_file(workspace_root, STAGE1_PATCH)
    if _sha256(patch_payload) != patch_record["sha256"]:
        raise SourceLockError("stage1 patch digest does not match provenance")
    try:
        extracted = _extract_new_file_from_patch(
            patch_payload.decode("utf-8"),
            file_record["extracted_from"],
        )
    except UnicodeError:
        raise SourceLockError("stage1 patch is not UTF-8") from None
    fixture_relative = HOST_SPI_FIXTURE_ROOT / file_record["path"]
    fixture_payload = _read_regular_file(workspace_root, fixture_relative)
    if extracted != fixture_payload or _sha256(extracted) != file_record["sha256"]:
        raise SourceLockError("Host SPI fixture is not exactly extracted from stage1 patch")

    updated = json.loads(json.dumps(provenance))
    updated["upstream"]["lock_sha256"] = _sha256(upstream_lock_payload)
    return _json_bytes(updated)


def _desired_payloads(workspace_root: Path) -> dict[Path, bytes]:
    provenance_payload = _validated_provenance_update(workspace_root)
    records = []
    for relative in _declared_source_paths(workspace_root):
        payload = _read_regular_file(workspace_root, relative)
        records.append({"path": relative.as_posix(), "sha256": _sha256(payload)})
    source_lock_payload = _json_bytes(
        {
            "schema_version": 2,
            "algorithm": "sha256-declared-integration-snapshot-v2",
            "files": records,
        }
    )
    return {
        _workspace_path(workspace_root, INTEGRATION_SOURCE_LOCK): source_lock_payload,
        _workspace_path(workspace_root, HOST_SPI_PROVENANCE): provenance_payload,
    }


def _write_temporary_payload(target: Path, payload: bytes) -> Path:
    mode = stat.S_IMODE(os.lstat(target).st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        output = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _fsync_directories(targets: Sequence[Path]) -> None:
    for directory in dict.fromkeys(target.parent for target in targets):
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _transactional_replace(
    payloads: Mapping[Path, bytes],
    originals: Mapping[Path, bytes],
    *,
    replace: _Replace,
) -> None:
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for target, payload in payloads.items():
            staged[target] = _write_temporary_payload(target, payload)
            backups[target] = _write_temporary_payload(target, originals[target])
        for target in payloads:
            replace(staged[target], target)
            committed.append(target)
        _fsync_directories(tuple(payloads))
    except BaseException as error:
        rollback_error: BaseException | None = None
        for target in reversed(committed):
            try:
                replace(backups[target], target)
            except BaseException as candidate:  # noqa: BLE001 - rollback all exits
                rollback_error = candidate
        try:
            if committed:
                _fsync_directories(tuple(committed))
        except BaseException as candidate:  # noqa: BLE001 - rollback all exits
            rollback_error = candidate
        if rollback_error is not None:
            raise SourceLockError("source-lock transaction rollback failed") from rollback_error
        raise SourceLockError("source-lock transaction failed; no files were changed") from error
    finally:
        for temporary in (*staged.values(), *backups.values()):
            temporary.unlink(missing_ok=True)


def synchronize_integration_source_lock(
    workspace_root: Path,
    *,
    check: bool = False,
    replace: _Replace = os.replace,
) -> bool:
    """Validate all evidence, then check or synchronize both authority files."""
    workspace_root = workspace_root.absolute()
    desired = _desired_payloads(workspace_root)
    current = {
        _workspace_path(workspace_root, relative): _read_regular_file(
            workspace_root, relative
        )
        for relative in (INTEGRATION_SOURCE_LOCK, HOST_SPI_PROVENANCE)
    }
    changed = {
        target: payload
        for target, payload in desired.items()
        if current[target] != payload
    }
    if check or not changed:
        return bool(changed)
    _transactional_replace(changed, current, replace=replace)
    return True


def main(
    argv: Sequence[str] | None = None,
    *,
    workspace_root: Path | None = None,
    replace: _Replace = os.replace,
) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the declared integration source lock and Host SPI provenance"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report drift without writing either authority file",
    )
    arguments = parser.parse_args(argv)
    root = (
        workspace_root
        if workspace_root is not None
        else Path(__file__).resolve().parents[4]
    )
    try:
        changed = synchronize_integration_source_lock(
            root,
            check=arguments.check,
            replace=replace,
        )
    except SourceLockError as error:
        print(f"integration source lock rejected: {error}", file=sys.stderr)
        return 2
    if arguments.check and changed:
        print("integration source lock is out of date", file=sys.stderr)
        return 1
    if changed:
        print("integration source lock and Host SPI provenance updated")
    else:
        print("integration source lock and Host SPI provenance are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
