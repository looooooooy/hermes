"""Build and verify the fixed-toolchain Hermes Cloud release set."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

CLOUD_ROOT = Path(__file__).resolve().parents[3]
DIST_ROOT = CLOUD_ROOT / "dist"
DIST_RELEASE_STORE = DIST_ROOT / "releases"
SOURCE_DATE_EPOCH = 1785628800
REQUIRED_PYTHON = "3.12.11"
REQUIRED_UV_VERSION = "0.9.25"
REQUIRED_HATCHLING = "1.31.0"
REQUIRED_BUILD = "1.5.0"
MANIFEST_SCHEMA_VERSION = 3
GATE_EVIDENCE_SCHEMA_VERSION = 5
RAW_AUDIT_SCHEMA_VERSION = 3
RELEASE_DESCRIPTION_SCHEMA = "hermes-cloud-release-description-v4"
RELEASE_IDENTITY_ALGORITHM = "sha256-release-description-v4"

WHEEL_NAME = "hermes_cloud-0.1.0-py3-none-any.whl"
SDIST_NAME = "hermes_cloud-0.1.0.tar.gz"
SDIST_HASH_NAME = "hermes_cloud-0.1.0.tar.gz.sha256"
BUNDLE_NAME = "hermes-cloud-sqlite-release.tar.gz"
MANIFEST_NAME = "RELEASE-MANIFEST.json"
CHECKSUMS_NAME = "SHA256SUMS"
CORE_ARTIFACT_NAMES = (WHEEL_NAME, SDIST_NAME, SDIST_HASH_NAME, BUNDLE_NAME)
CHECKSUMMED_NAMES = (*CORE_ARTIFACT_NAMES, MANIFEST_NAME)
RELEASE_FILE_NAMES = (*CHECKSUMMED_NAMES, CHECKSUMS_NAME)
GATE_EVIDENCE_DIRECTORY = CLOUD_ROOT / "deploy/test_server/release_evidence"
GATE_EVIDENCE_NAME = "GATE-EVIDENCE.json"
INTEGRATION_SOURCE_LOCK = (
    CLOUD_ROOT / "deploy/test_server/integration-source-lock.json"
)
INTEGRATION_SNAPSHOT_STORE = (
    CLOUD_ROOT / "deploy/test_server/integration_snapshots"
)
INTEGRATION_SNAPSHOT_ARCHIVE_ROOT = PurePosixPath(
    "hermes-cloud-integration-snapshot"
)
INTEGRATION_SNAPSHOT_RECORD = "SNAPSHOT.json"
GATE_EVIDENCE_ARCHIVE_ROOT = PurePosixPath(
    "hermes-cloud/deploy/test_server/release_evidence"
)
RAW_GATE_AUDIT_DIRECTORY = CLOUD_ROOT / "deploy/test_server/release_raw_audit"
RAW_GATE_AUDIT_NAME = "RAW-AUDIT.json"
RAW_GATE_AUDIT_CURRENT = "CURRENT"
RAW_GATE_AUDIT_ARCHIVE_ROOT = PurePosixPath("hermes-cloud-release-raw-audit")
_COMPATIBILITY_EXPRESSION = (
    "deployed_rev10_compatibility_catalog or "
    "exact_deployed_20260801t131728z_rev10_upgrades or "
    "deployed_20260801t131728z_rev10_compatibility_fails_closed or "
    "deployed_rev10_compatibility_preserves or "
    "deployed_rev10_upgrade_revalidates or test_v10_ or test_v11_ or "
    "cleanup_uses_the_seed_uuid_contract or cli_is_dry_run_first"
)
GATE_SELECTIONS = (
    {
        "selection_id": "migration",
        "kind": "pytest",
        "expected_count": 176,
        "argv": ("{python}", "-m", "pytest", "-q", "tests/platform/sqlite/test_migrations.py"),
    },
    {
        "selection_id": "compatibility",
        "kind": "pytest",
        "expected_count": 28,
        "argv": (
            "{python}",
            "-m",
            "pytest",
            "-q",
            "tests/platform/sqlite/test_migrations.py",
            "tests/platform/sqlite/test_v10_legacy_wire_repair.py",
            "tests/platform/sqlite/test_v11_session_identity.py",
            "deploy/test_server/tests/test_cleanup_test_seed_session_release.py",
            "-k",
            _COMPATIBILITY_EXPRESSION,
        ),
    },
    {
        "selection_id": "required_integration",
        "kind": "pytest",
        "expected_count": 1,
        "argv": (
            "{python}",
            "-m",
            "pytest",
            "-q",
            "tests/integration/test_web_connector_control_bridge_e2e.py::test_cookie_to_cloud_bridge_real_connector_lane_owner_actions",
        ),
    },
    {
        "selection_id": "release_artifacts",
        "kind": "pytest",
        "expected_count": 62,
        "argv": (
            "{python}",
            "-m",
            "pytest",
            "-q",
            "deploy/test_server/tests/test_sqlite_release_bundle.py",
        ),
    },
    {
        "selection_id": "release_validation",
        "kind": "unittest",
        "expected_count": 92,
        "argv": ("bash", "deploy/test_server/scripts/validate.sh"),
    },
    {
        "selection_id": "architecture_distribution",
        "kind": "pytest",
        "expected_count": 10,
        "argv": (
            "{python}",
            "-m",
            "pytest",
            "-q",
            "tests/entrypoints/test_business_api_architecture.py::test_business_api_surface_has_no_platform_sqlalchemy_or_raw_sql",
            "tests/migration/platform/postgres/test_architecture.py::test_business_and_database_adapter_code_cannot_use_raw_sql_escape_hatches",
            "tests/migration/platform/postgres/test_architecture.py::test_domain_application_and_ports_do_not_import_database_implementations",
            "tests/entrypoints/test_distribution_smoke.py",
            "tests/migration/platform/postgres/test_distribution_smoke.py",
        ),
    },
    {
        "selection_id": "cloud",
        "kind": "pytest",
        "expected_count": 1563,
        "argv": (
            "{python}",
            "-m",
            "pytest",
            "-q",
            "--ignore=tests/integration/test_web_connector_control_bridge_e2e.py",
        ),
    },
    {
        "selection_id": "ruff",
        "kind": "ruff",
        "expected_count": 1,
        "argv": ("{python}", "-m", "ruff", "check", "."),
    },
)

_BUNDLE_BUILDER = CLOUD_ROOT / "deploy/test_server/scripts/build_sqlite_release_bundle.py"


class ReleaseBuildError(RuntimeError):
    """Raised when the release set is incomplete, ambiguous, or non-reproducible."""


def _unsafe_filesystem_path() -> ReleaseBuildError:
    return ReleaseBuildError("unsafe filesystem path")


def _lexical_absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise _unsafe_filesystem_path()
    return path if path.is_absolute() else Path.cwd() / path


def _lstat_chain(path: Path, *, allow_missing_leaf: bool = False) -> None:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for index, component in enumerate(absolute.parts[1:], start=1):
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_leaf and index == len(absolute.parts) - 1:
                return
            raise _unsafe_filesystem_path() from None
        if stat.S_ISLNK(metadata.st_mode):
            raise _unsafe_filesystem_path()


def _verified_regular_metadata(path: Path) -> os.stat_result:
    _lstat_chain(path)
    try:
        metadata = os.lstat(path)
    except OSError:
        raise _unsafe_filesystem_path() from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _unsafe_filesystem_path()
    return metadata


def read_verified_regular_file(path: Path, *, maximum_bytes: int | None = None) -> bytes:
    """Read one regular, single-link file without following any symlink component."""
    before = _verified_regular_metadata(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _unsafe_filesystem_path() from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise _unsafe_filesystem_path()
        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if maximum_bytes is not None and observed > maximum_bytes:
                raise _unsafe_filesystem_path()
            chunks.append(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError:
        raise _unsafe_filesystem_path() from None
    identity = (before.st_dev, before.st_ino, before.st_size)
    if (
        (after_open.st_dev, after_open.st_ino, after_open.st_size) != identity
        or (after_path.st_dev, after_path.st_ino, after_path.st_size) != identity
        or after_path.st_nlink != 1
        or stat.S_ISLNK(after_path.st_mode)
    ):
        raise _unsafe_filesystem_path()
    return b"".join(chunks)


def _write_private_file(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    _lstat_chain(path.parent)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise _unsafe_filesystem_path()
        if path.is_file():
            if os.lstat(path).st_nlink != 1:
                raise _unsafe_filesystem_path()
            os.chmod(path, 0o444)
        elif path.is_dir():
            os.chmod(path, 0o555)
        else:
            raise _unsafe_filesystem_path()
    os.chmod(root, 0o555)


def _thaw_tree(root: Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            os.chmod(path, 0o700)
        elif path.is_file() and not path.is_symlink():
            os.chmod(path, 0o600)


def _directory_payloads(root: Path) -> dict[str, bytes]:
    _lstat_chain(root)
    if not root.is_dir():
        raise _unsafe_filesystem_path()
    payloads: dict[str, bytes] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise _unsafe_filesystem_path()
        if path.is_dir():
            continue
        if relative in payloads:
            raise _unsafe_filesystem_path()
        payloads[relative] = read_verified_regular_file(path)
    return payloads


@contextlib.contextmanager
def exclusive_release_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 5.0,
):
    if timeout_seconds < 0 or timeout_seconds > 60:
        raise ReleaseBuildError("release lock unavailable")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_path.parent, 0o700)
    _lstat_chain(lock_path.parent)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ReleaseBuildError("release lock unavailable")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ReleaseBuildError("release lock unavailable") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def atomic_promote_version(
    stage: Path,
    store_root: Path,
    version_id: str,
    *,
    before_pointer=None,
) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", version_id) is None:
        raise ReleaseBuildError("atomic promotion rejected")
    _lstat_chain(stage)
    if not stage.is_dir() or stage.is_symlink():
        raise ReleaseBuildError("atomic promotion rejected")
    store_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(store_root, 0o700)
    _lstat_chain(store_root)
    destination = store_root / version_id
    promoted = False
    try:
        for path in stage.rglob("*"):
            if path.is_file():
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        _fsync_directory(stage)
        if destination.exists() or destination.is_symlink():
            if _directory_payloads(destination) != _directory_payloads(stage):
                raise ReleaseBuildError("atomic promotion rejected")
            _thaw_tree(stage)
            shutil.rmtree(stage)
        else:
            stage.rename(destination)
            promoted = True
            _freeze_tree(destination)
        _fsync_directory(store_root)
        if before_pointer is not None:
            before_pointer()
        pointer_stage = store_root / f".CURRENT-{os.getpid()}-{time.monotonic_ns()}"
        _write_private_file(pointer_stage, f"{version_id}\n".encode("ascii"))
        os.replace(pointer_stage, store_root / "CURRENT")
        _fsync_directory(store_root)
        return destination
    except BaseException:
        if promoted and destination.exists():
            _thaw_tree(destination)
            shutil.rmtree(destination)
            _fsync_directory(store_root)
        if stage.exists():
            _thaw_tree(stage)
            shutil.rmtree(stage)
        raise


def current_version_directory(store_root: Path) -> Path:
    _lstat_chain(store_root)
    if not store_root.is_dir() or store_root.is_symlink():
        raise ReleaseBuildError("version store rejected")
    try:
        pointer = read_verified_regular_file(
            store_root / "CURRENT",
            maximum_bytes=65,
        ).decode("ascii")
    except (ReleaseBuildError, UnicodeError):
        raise ReleaseBuildError("version store rejected") from None
    if re.fullmatch(r"[0-9a-f]{64}\n", pointer) is None:
        raise ReleaseBuildError("version store rejected")
    directory = store_root / pointer.strip()
    _lstat_chain(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ReleaseBuildError("version store rejected")
    return directory


def snapshot_release_directory(candidate: Path, staging: Path) -> Path:
    _lstat_chain(candidate)
    if not candidate.is_dir() or candidate.is_symlink():
        raise ReleaseBuildError("release directory rejected")
    entries = tuple(candidate.iterdir())
    if {entry.name for entry in entries} != set(RELEASE_FILE_NAMES):
        raise ReleaseBuildError("release directory rejected")
    staging.mkdir(mode=0o700)
    os.chmod(staging, 0o700)
    try:
        for name in RELEASE_FILE_NAMES:
            _write_private_file(
                staging / name,
                read_verified_regular_file(candidate / name),
            )
        return staging
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def snapshot_external_archive(
    source: Path,
    destination: Path,
    *,
    required_mode: int = 0o600,
) -> Path:
    metadata = _verified_regular_metadata(source)
    if metadata.st_mode & 0o777 != required_mode:
        raise _unsafe_filesystem_path()
    payload = read_verified_regular_file(source, maximum_bytes=64 * 1024 * 1024)
    _write_private_file(destination, payload, mode=required_mode)
    return destination


release_lock_probe_script = f'''\
import runpy, sys, time
module = runpy.run_path({str(Path(__file__).absolute())!r})
try:
    with module["exclusive_release_lock"](
        module["Path"](sys.argv[1]), timeout_seconds=float(sys.argv[2])
    ):
        print("LOCKED", flush=True)
        time.sleep(float(sys.argv[3]))
except module["ReleaseBuildError"]:
    print("release lock unavailable", file=sys.stderr)
    raise SystemExit(78)
'''


def _bundle_builder() -> dict[str, object]:
    return runpy.run_path(str(_BUNDLE_BUILDER))


def _sha256(path: Path) -> str:
    return hashlib.sha256(read_verified_regular_file(path)).hexdigest()


def canonical_uv_version(display: object) -> str:
    if not isinstance(display, str):
        raise ReleaseBuildError("fixed release toolchain mismatch")
    matched = re.fullmatch(
        r"uv (?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
        r"(?: \([0-9a-f]{9,40} [0-9]{4}-[0-9]{2}-[0-9]{2}\))?",
        display,
    )
    if matched is None or matched.group("version") != REQUIRED_UV_VERSION:
        raise ReleaseBuildError("fixed release toolchain mismatch")
    return matched.group("version")


def canonical_toolchain_identity(observed: Mapping[str, str]) -> dict[str, str]:
    if set(observed) != {
        "python",
        "build_python",
        "uv_display",
        "hatchling",
        "build",
    }:
        raise ReleaseBuildError("fixed release toolchain mismatch")
    identity = {
        "python": observed["python"],
        "uv": canonical_uv_version(observed["uv_display"]),
        "hatchling": observed["hatchling"],
        "build": observed["build"],
    }
    if identity != {
        "python": f"CPython {REQUIRED_PYTHON}",
        "uv": REQUIRED_UV_VERSION,
        "hatchling": REQUIRED_HATCHLING,
        "build": REQUIRED_BUILD,
    }:
        raise ReleaseBuildError("fixed release toolchain mismatch")
    return identity


def toolchain_audit_observation(observed: Mapping[str, str]) -> dict[str, str]:
    canonical_toolchain_identity(observed)
    return {"uv_display": observed["uv_display"]}


def _observed_toolchain(environment: Mapping[str, str]) -> dict[str, str]:
    expected_environment = CLOUD_ROOT / ".venv"
    uv = shutil.which("uv", path=environment.get("PATH"))
    if uv is None:
        raise ReleaseBuildError("fixed release toolchain is unavailable")
    version = subprocess.run(
        (uv, "--version"),
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    help_result = subprocess.run(
        (uv, "build", "--help"),
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    observed = {
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "build_python": ".venv/bin/python",
        "uv_display": version.stdout.strip(),
        "hatchling": importlib.metadata.version("hatchling"),
        "build": importlib.metadata.version("build"),
    }
    if (
        version.returncode != 0
        or help_result.returncode != 0
        or "--no-build-isolation" not in help_result.stdout
        or "--offline" not in help_result.stdout
        or Path(sys.prefix).absolute() != expected_environment
        or platform.python_implementation() != "CPython"
        or platform.python_version() != REQUIRED_PYTHON
    ):
        raise ReleaseBuildError("fixed release toolchain mismatch")
    canonical_toolchain_identity(observed)
    return observed


def _toolchain(environment: Mapping[str, str]) -> dict[str, str]:
    return canonical_toolchain_identity(_observed_toolchain(environment))


def _toolchain_audit_observation(
    environment: Mapping[str, str],
) -> dict[str, str]:
    return toolchain_audit_observation(_observed_toolchain(environment))


def distribution_build_command(
    uv: Path,
    python: Path,
    output: Path,
) -> tuple[str, ...]:
    return (
        str(uv),
        "build",
        "--offline",
        "--no-build-isolation",
        "--python",
        str(python),
        "--out-dir",
        str(output),
    )


def require_build_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if environment.get("SOURCE_DATE_EPOCH") != str(SOURCE_DATE_EPOCH):
        raise ReleaseBuildError(
            f"SOURCE_DATE_EPOCH must equal {SOURCE_DATE_EPOCH}"
        )
    return _toolchain(environment)


def _release_source_contract(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    _lstat_chain(root)
    if not root.is_dir() or root.is_symlink():
        raise ReleaseBuildError("release source contract rejected")
    project_file = root / "pyproject.toml"
    try:
        project = tomllib.loads(
            read_verified_regular_file(project_file).decode("utf-8")
        )
        sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]
        only_include = tuple(sdist["only-include"])
        exclude = tuple(sdist["exclude"])
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
        raise ReleaseBuildError("release source contract rejected") from None
    if (
        sdist.get("ignore-vcs") is not True
        or not only_include
        or any(
            not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or ".." in PurePosixPath(relative).parts
            or len(PurePosixPath(relative).parts) > 2
            for relative in only_include
        )
        or len(set(only_include)) != len(only_include)
        or any(not isinstance(pattern, str) or not pattern for pattern in exclude)
        or len(set(exclude)) != len(exclude)
    ):
        raise ReleaseBuildError("release source contract rejected")
    return only_include, exclude


def _release_source_is_excluded(relative: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    relative_text = relative.as_posix()
    for pattern in patterns:
        if pattern.startswith("/") and pattern.endswith("/**"):
            prefix = pattern[1:-3].rstrip("/")
            if relative_text == prefix or relative_text.startswith(f"{prefix}/"):
                return True
        elif pattern.startswith("**/") and pattern.endswith("/**"):
            component = pattern[3:-3].strip("/")
            if component in relative.parts:
                return True
        elif pattern == "**/*.pyc":
            if relative.suffix == ".pyc":
                return True
        else:
            raise ReleaseBuildError("release source contract rejected")
    return False


def _release_source_files(root: Path = CLOUD_ROOT) -> tuple[tuple[PurePosixPath, Path], ...]:
    _lstat_chain(root)
    if not root.is_dir() or root.is_symlink():
        raise ReleaseBuildError("release source contract rejected")
    only_include, exclude = _release_source_contract(root)
    selected: dict[PurePosixPath, Path] = {}
    for declared in only_include:
        source = root / declared
        if source.is_symlink() or not source.exists():
            raise ReleaseBuildError("release source contract rejected")
        candidates = (source,) if source.is_file() else tuple(source.rglob("*"))
        for candidate in candidates:
            relative = PurePosixPath(candidate.relative_to(root).as_posix())
            if _release_source_is_excluded(relative, exclude):
                continue
            if candidate.is_symlink():
                raise ReleaseBuildError("release source contains an unsafe link")
            if candidate.is_dir():
                continue
            if not candidate.is_file():
                raise ReleaseBuildError("release source contract rejected")
            selected[relative] = candidate
    if not selected:
        raise ReleaseBuildError("release source is empty")
    return tuple(sorted(selected.items(), key=lambda item: item[0].as_posix()))


def _source_tree_identity(root: Path = CLOUD_ROOT) -> dict[str, object]:
    records: list[bytes] = []
    sources = _release_source_files(root)
    for relative, source in sources:
        digest = _sha256(source)
        records.append(f"{digest}  {relative.as_posix()}\n".encode())
    return {
        "algorithm": "sha256-release-source-allowlist-v1",
        "file_count": len(sources),
        "sha256": hashlib.sha256(b"".join(records)).hexdigest(),
        "git": {
            "head": None,
            "tracked": False,
            "reason": (
                "hermes-cloud is untracked in the enclosing checkout; "
                "Git is not release identity"
            ),
        },
    }


def create_release_source_snapshot(
    staging_parent: Path,
) -> tuple[Path, dict[str, object]]:
    sources = _release_source_files()
    payloads = {
        relative: read_verified_regular_file(source)
        for relative, source in sources
    }
    records = [
        f"{hashlib.sha256(payload).hexdigest()}  {relative.as_posix()}\n".encode()
        for relative, payload in sorted(
            payloads.items(),
            key=lambda item: item[0].as_posix(),
        )
    ]
    identity = {
        "algorithm": "sha256-release-source-allowlist-v1",
        "file_count": len(payloads),
        "sha256": hashlib.sha256(b"".join(records)).hexdigest(),
        "git": {
            "head": None,
            "tracked": False,
            "reason": (
                "hermes-cloud is untracked in the enclosing checkout; "
                "Git is not release identity"
            ),
        },
    }
    if _source_tree_identity() != identity:
        raise ReleaseBuildError("release source changed while snapshotting")
    staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(
        tempfile.mkdtemp(prefix=".release-source-", dir=staging_parent)
    )
    os.chmod(stage, 0o700)
    try:
        for relative, payload in sorted(
            payloads.items(),
            key=lambda item: item[0].as_posix(),
        ):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            _write_private_file(target, payload)
        if _source_tree_identity(stage) != identity:
            raise ReleaseBuildError("release source snapshot rejected")
        return stage, identity
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


_INTEGRATION_SOURCE_ROOTS = (
    PurePosixPath("hermes-agent-plugin/src"),
    PurePosixPath("hermes-connector/src"),
)
_INTEGRATION_SOURCE_EXACT_FILES = frozenset(
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


def _integration_source_lock(lock_path: Path) -> tuple[dict[str, str], ...]:
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ReleaseBuildError("integration source rejected")
    try:
        lock = _json_without_duplicate_keys(
            read_verified_regular_file(lock_path).decode("utf-8")
        )
    except (OSError, UnicodeError, ReleaseBuildError):
        raise ReleaseBuildError("integration source rejected") from None
    records = lock.get("files")
    if (
        set(lock) != {"schema_version", "algorithm", "files"}
        or lock.get("schema_version") != 2
        or lock.get("algorithm") != "sha256-declared-integration-snapshot-v2"
        or not isinstance(records, list)
        or not records
    ):
        raise ReleaseBuildError("integration source rejected")
    normalized: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise ReleaseBuildError("integration source rejected")
        path_text = record.get("path")
        digest = record.get("sha256")
        if not isinstance(path_text, str) or not isinstance(digest, str):
            raise ReleaseBuildError("integration source rejected")
        relative = PurePosixPath(path_text)
        if (
            relative.is_absolute()
            or relative.as_posix() != path_text
            or ".." in relative.parts
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ReleaseBuildError("integration source rejected")
        normalized.append({"path": path_text, "sha256": digest})
    paths = tuple(PurePosixPath(record["path"]) for record in normalized)
    if (
        len(paths) != len(set(paths))
        or not _INTEGRATION_SOURCE_EXACT_FILES.issubset(paths)
        or any(
            path not in _INTEGRATION_SOURCE_EXACT_FILES
            and not any(path.is_relative_to(root) for root in _INTEGRATION_SOURCE_ROOTS)
            for path in paths
        )
        or any(not any(path.is_relative_to(root) for path in paths) for root in _INTEGRATION_SOURCE_ROOTS)
    ):
        raise ReleaseBuildError("integration source rejected")
    return tuple(sorted(normalized, key=lambda record: record["path"]))


def declared_integration_source_identity(
    lock_path: Path = INTEGRATION_SOURCE_LOCK,
) -> dict[str, object]:
    records = _integration_source_lock(lock_path)
    return {
        "algorithm": "sha256-declared-integration-snapshot-v2",
        "file_count": len(records),
        "sha256": hashlib.sha256(_canonical_json(records)).hexdigest(),
    }


def validate_integration_source_inputs(
    lock_path: Path = INTEGRATION_SOURCE_LOCK,
    workspace_root: Path = CLOUD_ROOT.parent,
) -> dict[str, object]:
    records = _integration_source_lock(lock_path)
    _lstat_chain(workspace_root)
    if workspace_root.is_symlink() or not workspace_root.is_dir():
        raise ReleaseBuildError("integration source rejected")
    expected = {PurePosixPath(record["path"]): record["sha256"] for record in records}
    actual_paths: set[PurePosixPath] = set(_INTEGRATION_SOURCE_EXACT_FILES)
    for root in _INTEGRATION_SOURCE_ROOTS:
        source_root = workspace_root / root
        _lstat_chain(source_root)
        if source_root.is_symlink() or not source_root.is_dir():
            raise ReleaseBuildError("integration source rejected")
        for candidate in source_root.rglob("*"):
            relative = PurePosixPath(candidate.relative_to(workspace_root).as_posix())
            if "__pycache__" in relative.parts or candidate.suffix in {".pyc", ".pyo"}:
                continue
            if candidate.is_symlink() or (not candidate.is_dir() and not candidate.is_file()):
                raise ReleaseBuildError("integration source rejected")
            if candidate.is_file():
                actual_paths.add(relative)
    if actual_paths != set(expected):
        raise ReleaseBuildError("integration source rejected")
    for relative, digest in expected.items():
        source = workspace_root / relative
        if source.is_symlink() or not source.is_file() or _sha256(source) != digest:
            raise ReleaseBuildError("integration source rejected")
    return declared_integration_source_identity(lock_path)


def _integration_snapshot_record(
    *,
    declared_identity: Mapping[str, object],
    records: tuple[dict[str, str], ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "archive_root": INTEGRATION_SNAPSHOT_ARCHIVE_ROOT.as_posix(),
        "declared_identity": dict(declared_identity),
        "files": list(records),
    }


def _deterministic_snapshot_archive(payloads: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with (
        gzip.GzipFile(
            fileobj=output,
            mode="wb",
            filename="",
            mtime=SOURCE_DATE_EPOCH,
        ) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name, payload in sorted(payloads.items()):
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ReleaseBuildError("integration snapshot rejected")
            info = tarfile.TarInfo(
                str(INTEGRATION_SNAPSHOT_ARCHIVE_ROOT / relative)
            )
            info.size = len(payload)
            info.mtime = SOURCE_DATE_EPOCH
            info.mode = 0o444
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _integration_snapshot_identity(
    *,
    archive_payload: bytes,
    declared_identity: Mapping[str, object],
) -> dict[str, object]:
    snapshot_id = hashlib.sha256(archive_payload).hexdigest()
    return {
        "algorithm": "sha256-integration-snapshot-archive-v1",
        "snapshot_id": snapshot_id,
        "archive_sha256": snapshot_id,
        "file_count": declared_identity["file_count"],
        "source_sha256": declared_identity["sha256"],
    }


def create_integration_source_snapshot(
    *,
    lock_path: Path = INTEGRATION_SOURCE_LOCK,
    workspace_root: Path = CLOUD_ROOT.parent,
    store_root: Path = INTEGRATION_SNAPSHOT_STORE,
) -> dict[str, object]:
    records = _integration_source_lock(lock_path)
    declared_identity = validate_integration_source_inputs(lock_path, workspace_root)
    payloads: dict[str, bytes] = {}
    for record in records:
        relative = record["path"]
        payload = read_verified_regular_file(workspace_root / relative)
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise ReleaseBuildError("integration snapshot rejected")
        payloads[relative] = payload
    record = _integration_snapshot_record(
        declared_identity=declared_identity,
        records=records,
    )
    record_payload = (
        json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("ascii")
    archive_payloads = {**payloads, INTEGRATION_SNAPSHOT_RECORD: record_payload}
    archive_payload = _deterministic_snapshot_archive(archive_payloads)
    identity = _integration_snapshot_identity(
        archive_payload=archive_payload,
        declared_identity=declared_identity,
    )
    # Revalidate live inputs after all bytes are captured. The gate will use only
    # the private snapshot from this point onward.
    if validate_integration_source_inputs(lock_path, workspace_root) != declared_identity:
        raise ReleaseBuildError("integration snapshot rejected")

    store_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_release_lock(
        store_root.parent / ".integration-snapshot.lock",
        timeout_seconds=5.0,
    ):
        stage = Path(
            tempfile.mkdtemp(
                prefix=".integration-snapshot-",
                dir=store_root.parent,
            )
        )
        os.chmod(stage, 0o700)
        try:
            for name, payload in sorted(payloads.items()):
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(target.parent, 0o700)
                _write_private_file(target, payload)
            _write_private_file(stage / INTEGRATION_SNAPSHOT_RECORD, record_payload)
            archive_name = f"{identity['snapshot_id']}.tar.gz"
            _write_private_file(stage / archive_name, archive_payload)
            directory = atomic_promote_version(
                stage,
                store_root,
                str(identity["snapshot_id"]),
            )
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage)
            raise
    archive = directory / f"{identity['snapshot_id']}.tar.gz"
    if validate_integration_snapshot_archive(archive) != identity:
        raise ReleaseBuildError("integration snapshot rejected")
    return {
        "identity": identity,
        "declared_identity": declared_identity,
        "directory": directory,
        "archive": archive,
    }


def validate_integration_snapshot_archive(archive_path: Path) -> dict[str, object]:
    try:
        archive_payload = read_verified_regular_file(
            archive_path,
            maximum_bytes=64 * 1024 * 1024,
        )
    except ReleaseBuildError:
        raise ReleaseBuildError("integration snapshot rejected") from None
    archive_sha256 = hashlib.sha256(archive_payload).hexdigest()
    if archive_path.name != f"{archive_sha256}.tar.gz":
        raise ReleaseBuildError("integration snapshot rejected")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_payload), mode="r:gz") as opened:
            members = opened.getmembers()
            names = tuple(member.name for member in members)
            if (
                len(names) != len(set(names))
                or any(
                    not member.isfile()
                    or member.mode != 0o444
                    or member.mtime != SOURCE_DATE_EPOCH
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != ""
                    or member.gname != ""
                    or PurePosixPath(member.name).is_absolute()
                    or ".." in PurePosixPath(member.name).parts
                    or PurePosixPath(member.name).parent
                    == PurePosixPath(".")
                    or not PurePosixPath(member.name).is_relative_to(
                        INTEGRATION_SNAPSHOT_ARCHIVE_ROOT
                    )
                    for member in members
                )
            ):
                raise ReleaseBuildError("integration snapshot rejected")
            payloads = {
                PurePosixPath(member.name).relative_to(
                    INTEGRATION_SNAPSHOT_ARCHIVE_ROOT
                ).as_posix(): opened.extractfile(member).read()  # type: ignore[union-attr]
                for member in members
            }
    except (OSError, tarfile.TarError, ReleaseBuildError):
        raise ReleaseBuildError("integration snapshot rejected") from None
    try:
        record = _json_without_duplicate_keys(
            payloads.pop(INTEGRATION_SNAPSHOT_RECORD).decode("ascii")
        )
    except (KeyError, UnicodeError, ReleaseBuildError):
        raise ReleaseBuildError("integration snapshot rejected") from None
    records = record.get("files")
    declared_identity = record.get("declared_identity")
    if (
        set(record) != {"schema_version", "archive_root", "declared_identity", "files"}
        or record.get("schema_version") != 1
        or record.get("archive_root") != INTEGRATION_SNAPSHOT_ARCHIVE_ROOT.as_posix()
        or not isinstance(records, list)
        or not isinstance(declared_identity, dict)
    ):
        raise ReleaseBuildError("integration snapshot rejected")
    normalized_records = tuple(records)
    if (
        tuple(sorted(records, key=lambda item: item.get("path", "") if isinstance(item, dict) else ""))
        != normalized_records
        or set(payloads) != {
            item.get("path") for item in records if isinstance(item, dict)
        }
    ):
        raise ReleaseBuildError("integration snapshot rejected")
    for item in records:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256"))) is None
            or hashlib.sha256(payloads[str(item.get("path"))]).hexdigest()
            != item.get("sha256")
        ):
            raise ReleaseBuildError("integration snapshot rejected")
    expected_declared = {
        "algorithm": "sha256-declared-integration-snapshot-v2",
        "file_count": len(records),
        "sha256": hashlib.sha256(_canonical_json(records)).hexdigest(),
    }
    if declared_identity != expected_declared:
        raise ReleaseBuildError("integration snapshot rejected")
    if archive_payload != _deterministic_snapshot_archive(
        {**payloads, INTEGRATION_SNAPSHOT_RECORD: (
            json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        ).encode("ascii")},
    ):
        raise ReleaseBuildError("integration snapshot rejected")
    return _integration_snapshot_identity(
        archive_payload=archive_payload,
        declared_identity=expected_declared,
    )


def verify_integration_snapshot_binding(
    expected_identity: Mapping[str, object],
    archive_path: Path,
    *,
    live_lock_path: Path | None = INTEGRATION_SOURCE_LOCK,
    live_workspace_root: Path | None = CLOUD_ROOT.parent,
) -> dict[str, object]:
    identity = validate_integration_snapshot_archive(archive_path)
    if dict(expected_identity) != identity:
        raise ReleaseBuildError("integration snapshot rejected")
    if (live_lock_path is None) != (live_workspace_root is None):
        raise ReleaseBuildError("integration snapshot rejected")
    if live_lock_path is not None and live_workspace_root is not None:
        try:
            declared = validate_integration_source_inputs(
                live_lock_path,
                live_workspace_root,
            )
        except ReleaseBuildError:
            raise ReleaseBuildError("integration snapshot rejected") from None
        if (
            identity.get("file_count") != declared.get("file_count")
            or identity.get("source_sha256") != declared.get("sha256")
        ):
            raise ReleaseBuildError("integration snapshot rejected")
    return identity


def _artifact_record(path: Path) -> dict[str, object]:
    try:
        payload = read_verified_regular_file(path)
    except ReleaseBuildError:
        raise ReleaseBuildError("release artifact rejected") from None
    return {
        "filename": path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _validate_release_archive_members(
    files: Mapping[str, Path],
    *,
    source_root: Path = CLOUD_ROOT,
) -> None:
    raw_markers = ("release_raw_audit", "hermes-cloud-release-raw-audit")
    try:
        with ZipFile(files[WHEEL_NAME]) as wheel:
            wheel_names = tuple(wheel.namelist())
            if len(wheel_names) != len(set(wheel_names)):
                raise ReleaseBuildError("release artifact rejected")
        tar_names: dict[str, tuple[str, ...]] = {}
        for artifact_name in (SDIST_NAME, BUNDLE_NAME):
            with tarfile.open(files[artifact_name], "r:gz") as archive:
                names = tuple(
                    member.name for member in archive.getmembers() if member.isfile()
                )
                if len(names) != len(set(names)):
                    raise ReleaseBuildError("release artifact rejected")
                tar_names[artifact_name] = names
    except (BadZipFile, OSError, tarfile.TarError):
        raise ReleaseBuildError("release artifact rejected") from None
    if any(
        marker in name
        for name in (*wheel_names, *tar_names[SDIST_NAME], *tar_names[BUNDLE_NAME])
        for marker in raw_markers
    ):
        raise ReleaseBuildError("release artifact rejected")

    sdist_root = PurePosixPath("hermes_cloud-0.1.0")
    expected_sources = {
        str(sdist_root / relative)
        for relative, _source in _release_source_files(source_root)
    }
    if set(tar_names[SDIST_NAME]) != expected_sources | {
        str(sdist_root / "PKG-INFO")
    }:
        raise ReleaseBuildError("release artifact rejected")
    try:
        with tarfile.open(files[SDIST_NAME], "r:gz") as archive:
            members = {member.name: member for member in archive.getmembers()}
            for relative, source in _release_source_files(source_root):
                extracted = archive.extractfile(members[str(sdist_root / relative)])
                if extracted is None or extracted.read() != source.read_bytes():
                    raise ReleaseBuildError("release artifact rejected")
    except (KeyError, OSError, tarfile.TarError):
        raise ReleaseBuildError("release artifact rejected") from None


def _release_timestamp() -> str:
    return datetime.fromtimestamp(SOURCE_DATE_EPOCH, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def release_id_from_description(description: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_canonical_json(description)).hexdigest()
    timestamp = datetime.fromtimestamp(SOURCE_DATE_EPOCH, tz=UTC).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return f"{timestamp}-{digest[:32]}"


def _nested_wheel_record(bundle: Path, wheel: Path) -> dict[str, object]:
    nested_path = f"hermes-cloud/artifacts/{wheel.name}"
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        if any(
            PurePosixPath(member.name).is_absolute()
            or ".." in PurePosixPath(member.name).parts
            or not member.isfile()
            for member in members
        ):
            raise ReleaseBuildError("release artifact rejected")
        matching = [member for member in members if member.name == nested_path]
        if len(matching) != 1:
            raise ReleaseBuildError("release artifact rejected")
        extracted = archive.extractfile(matching[0])
        if extracted is None:
            raise ReleaseBuildError("release artifact rejected")
        payload = extracted.read()
    if payload != wheel.read_bytes():
        raise ReleaseBuildError("release artifact rejected")
    return {"path": nested_path, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _manifest(
    output: Path,
    *,
    toolchain: dict[str, str],
    source_tree: dict[str, object],
    integration_source: dict[str, object],
    integration_snapshot_record: dict[str, object],
    raw_gate_audit: dict[str, object],
) -> dict[str, object]:
    artifacts = [_artifact_record(output / name) for name in CORE_ARTIFACT_NAMES]
    gate_evidence = _gate_evidence_record(
        output / BUNDLE_NAME,
        source_tree=source_tree,
        integration_source=integration_source,
        toolchain=toolchain,
    )
    release_description = {
        "schema": RELEASE_DESCRIPTION_SCHEMA,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "package": {"name": "hermes-cloud", "version": "0.1.0"},
        "artifacts": artifacts,
        "source_tree": source_tree,
        "integration_source": integration_source,
        "integration_snapshot": integration_snapshot_record,
        "toolchain": toolchain,
        "gate_evidence": gate_evidence,
    }
    description_digest = hashlib.sha256(
        _canonical_json(release_description)
    ).hexdigest()
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_id": release_id_from_description(release_description),
        "release_identity": {
            "algorithm": RELEASE_IDENTITY_ALGORITHM,
            "description_sha256": description_digest,
            "suffix_length": 32,
        },
        "release_timestamp_utc": _release_timestamp(),
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "package": {"name": "hermes-cloud", "version": "0.1.0"},
        "artifacts": artifacts,
        "manifest_integrity": {"provided_by": CHECKSUMS_NAME, "self_digest": False},
        "nested_artifacts": {
            "sqlite_release_bundle": {
                "wheel": _nested_wheel_record(
                    output / BUNDLE_NAME,
                    output / WHEEL_NAME,
                )
            }
        },
        "source_tree": source_tree,
        "integration_source": integration_source,
        "integration_snapshot": integration_snapshot_record,
        "toolchain": toolchain,
        "gate_evidence": gate_evidence,
        "raw_gate_audit": raw_gate_audit,
        "reproducibility_scope": (
            "bit-for-bit only for the fixed toolchain and SOURCE_DATE_EPOCH"
        ),
    }


def _json_without_duplicate_keys(payload: str) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise ReleaseBuildError("release manifest rejected")
            result[name] = value
        return result

    try:
        parsed = json.loads(payload, object_pairs_hook=object_pairs)
    except (json.JSONDecodeError, UnicodeError):
        raise ReleaseBuildError("release manifest rejected") from None
    if not isinstance(parsed, dict):
        raise ReleaseBuildError("release manifest rejected")
    return parsed


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def gate_selection_contract_sha256() -> str:
    return hashlib.sha256(_canonical_json(GATE_SELECTIONS)).hexdigest()


def normalize_gate_output(payload: str) -> str:
    result: list[str] = []
    pytest_summary = re.compile(
        r"^(?P<prefix>\d+ passed(?:, \d+ [a-z]+)* in )"
        r"\d+(?:\.\d+)?s(?P<clock> \(\d+:\d{2}:\d{2}\))?$"
    )
    unittest_summary = re.compile(
        r"^(?P<prefix>Ran \d+ tests? in )\d+(?:\.\d+)?s$"
    )
    for line in payload.splitlines(keepends=True):
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        pytest_match = pytest_summary.fullmatch(body)
        unittest_match = unittest_summary.fullmatch(body)
        if pytest_match:
            body = pytest_match.group("prefix") + "<DURATION>"
        elif unittest_match:
            body = unittest_match.group("prefix") + "<DURATION>"
        result.append(body + ending)
    return "".join(result)


def parse_gate_result(
    selection: Mapping[str, object],
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> dict[str, object]:
    if exit_code != 0:
        raise ReleaseBuildError("gate evidence rejected")
    kind = selection["kind"]
    if kind == "pytest":
        matches = re.findall(r"(?:^|\s)(\d+) passed(?:,|\s)", stdout)
        if len(matches) != 1:
            raise ReleaseBuildError("gate evidence rejected")
        selected = int(matches[0])
    elif kind == "unittest":
        combined = f"{stdout}\n{stderr}"
        matches = re.findall(r"Ran (\d+) tests? in <DURATION>", combined)
        if (
            len(matches) != 1
            or "\nOK\n" not in f"\n{combined}"
            or "deployment_artifacts=PASS" not in combined
        ):
            raise ReleaseBuildError("gate evidence rejected")
        selected = int(matches[0])
    elif kind == "ruff":
        if stdout.strip() != "All checks passed!":
            raise ReleaseBuildError("gate evidence rejected")
        selected = 1
    else:
        raise ReleaseBuildError("gate evidence rejected")
    if selected <= 0 or selected != selection.get("expected_count"):
        raise ReleaseBuildError("gate evidence rejected")
    return {
        "exit_code": 0,
        "failed": 0,
        "passed": selected,
        "selected": selected,
        "status": "PASS",
    }


def gate_evidence_set_sha256(evidence: Mapping[str, object]) -> str:
    unsigned = dict(evidence)
    unsigned.pop("evidence_set_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def validate_raw_gate_output(payload: bytes) -> None:
    if len(payload) > 16 * 1024 * 1024 or b"\x00" in payload:
        raise ReleaseBuildError("raw gate audit rejected")
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        raise ReleaseBuildError("raw gate audit rejected") from None
    secret = re.compile(
        r"(?i)\b(?:token|secret|password|api[_-]?key|private[_-]?key|authorization)"
        r"\b\s*[:=]\s*\S+"
    )
    authenticated_proxy = re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@")
    local_path = re.compile(
        r"(?<![A-Za-z0-9:])/(?:Users|home|root|private|tmp|opt|var/folders)/\S*"
    )
    if secret.search(text) or authenticated_proxy.search(text) or local_path.search(text):
        raise ReleaseBuildError("raw gate audit rejected")


def raw_gate_audit_set_sha256(audit: Mapping[str, object]) -> str:
    unsigned = dict(audit)
    unsigned.pop("raw_audit_set_sha256", None)
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def validate_raw_gate_audit_payloads(
    payloads: Mapping[str, bytes],
    *,
    source_tree: Mapping[str, object],
    integration_source: Mapping[str, object],
    toolchain: Mapping[str, str],
) -> dict[str, object]:
    expected_names = {RAW_GATE_AUDIT_NAME}
    for selection in GATE_SELECTIONS:
        selection_id = str(selection["selection_id"])
        expected_names.update(
            {f"{selection_id}.raw.stdout", f"{selection_id}.raw.stderr"}
        )
    if set(payloads) != expected_names:
        raise ReleaseBuildError("raw gate audit rejected")
    try:
        audit = _json_without_duplicate_keys(
            payloads[RAW_GATE_AUDIT_NAME].decode("utf-8")
        )
    except (ReleaseBuildError, KeyError, UnicodeError):
        raise ReleaseBuildError("raw gate audit rejected") from None
    expected_top_level = {
        "schema_version",
        "attestation",
        "trust_scope",
        "generated_at_utc",
        "source_date_epoch",
        "source_tree",
        "integration_source",
        "toolchain",
        "toolchain_observation",
        "selection_contract_sha256",
        "selections",
        "raw_audit_set_sha256",
    }
    selections = audit.get("selections")
    observation = audit.get("toolchain_observation")
    if (
        set(audit) != expected_top_level
        or audit.get("schema_version") != RAW_AUDIT_SCHEMA_VERSION
        or audit.get("attestation") != "untrusted/self-recorded"
        or audit.get("trust_scope")
        != "diagnostic-capture-only/non-stable/non-release-identity"
        or audit.get("generated_at_utc") != _release_timestamp()
        or audit.get("source_date_epoch") != SOURCE_DATE_EPOCH
        or audit.get("source_tree") != source_tree
        or audit.get("integration_source") != integration_source
        or audit.get("toolchain") != toolchain
        or not isinstance(observation, dict)
        or set(observation) != {"uv_display"}
        or canonical_uv_version(observation.get("uv_display"))
        != toolchain.get("uv")
        or audit.get("selection_contract_sha256")
        != gate_selection_contract_sha256()
        or not isinstance(selections, list)
        or len(selections) != len(GATE_SELECTIONS)
        or audit.get("raw_audit_set_sha256")
        != raw_gate_audit_set_sha256(audit)
    ):
        raise ReleaseBuildError("raw gate audit rejected")
    record_keys = {
        "selection_id",
        "stdout_file",
        "stdout_sha256",
        "stdout_normalized_sha256",
        "stderr_file",
        "stderr_sha256",
        "stderr_normalized_sha256",
    }
    for selection, record in zip(GATE_SELECTIONS, selections, strict=True):
        selection_id = str(selection["selection_id"])
        stdout_file = f"{selection_id}.raw.stdout"
        stderr_file = f"{selection_id}.raw.stderr"
        if (
            not isinstance(record, dict)
            or set(record) != record_keys
            or record.get("selection_id") != selection_id
            or record.get("stdout_file") != stdout_file
            or record.get("stderr_file") != stderr_file
        ):
            raise ReleaseBuildError("raw gate audit rejected")
        stdout = payloads[stdout_file]
        stderr = payloads[stderr_file]
        validate_raw_gate_output(stdout)
        validate_raw_gate_output(stderr)
        try:
            normalized_stdout = normalize_gate_output(stdout.decode("utf-8")).encode()
            normalized_stderr = normalize_gate_output(stderr.decode("utf-8")).encode()
        except UnicodeError:
            raise ReleaseBuildError("raw gate audit rejected") from None
        if (
            record.get("stdout_sha256") != hashlib.sha256(stdout).hexdigest()
            or record.get("stderr_sha256") != hashlib.sha256(stderr).hexdigest()
            or record.get("stdout_normalized_sha256")
            != hashlib.sha256(normalized_stdout).hexdigest()
            or record.get("stderr_normalized_sha256")
            != hashlib.sha256(normalized_stderr).hexdigest()
        ):
            raise ReleaseBuildError("raw gate audit rejected")
    return audit


def _gate_payload_name(filename: str) -> str:
    return str(GATE_EVIDENCE_ARCHIVE_ROOT / filename)


def validate_gate_evidence_payloads(
    payloads: Mapping[str, bytes],
    *,
    source_tree: Mapping[str, object],
    integration_source: Mapping[str, object],
    toolchain: Mapping[str, str],
) -> dict[str, object]:
    expected_names = {_gate_payload_name(GATE_EVIDENCE_NAME)}
    for selection in GATE_SELECTIONS:
        selection_id = str(selection["selection_id"])
        expected_names.update(
            {
                _gate_payload_name(f"{selection_id}.stdout"),
                _gate_payload_name(f"{selection_id}.stderr"),
            }
        )
    if set(payloads) != expected_names:
        raise ReleaseBuildError("gate evidence rejected")
    try:
        evidence = _json_without_duplicate_keys(
            payloads[_gate_payload_name(GATE_EVIDENCE_NAME)].decode("utf-8")
        )
    except (KeyError, UnicodeError):
        raise ReleaseBuildError("gate evidence rejected") from None
    expected_top_level = {
        "schema_version",
        "deterministic",
        "attestation",
        "trust_scope",
        "normalized_output_scope",
        "generated_at_utc",
        "source_date_epoch",
        "source_tree",
        "integration_source",
        "toolchain",
        "selection_contract_sha256",
        "selections",
        "evidence_set_sha256",
    }
    selections = evidence.get("selections")
    if (
        set(evidence) != expected_top_level
        or evidence.get("schema_version") != GATE_EVIDENCE_SCHEMA_VERSION
        or evidence.get("deterministic") is not True
        or evidence.get("attestation") != "untrusted/self-recorded"
        or evidence.get("trust_scope") != "integrity-and-replay-only"
        or evidence.get("normalized_output_scope")
        != "exact-pytest-unittest-summary-lines-only"
        or evidence.get("generated_at_utc") != _release_timestamp()
        or evidence.get("source_date_epoch") != SOURCE_DATE_EPOCH
        or evidence.get("source_tree") != source_tree
        or evidence.get("integration_source") != integration_source
        or evidence.get("toolchain") != toolchain
        or evidence.get("selection_contract_sha256")
        != gate_selection_contract_sha256()
        or not isinstance(selections, list)
        or len(selections) != len(GATE_SELECTIONS)
        or evidence.get("evidence_set_sha256")
        != gate_evidence_set_sha256(evidence)
    ):
        raise ReleaseBuildError("gate evidence rejected")
    record_keys = {
        "selection_id",
        "kind",
        "expected_count",
        "argv",
        "argv_sha256",
        "exit_code",
        "status",
        "selected",
        "passed",
        "failed",
        "stdout_file",
        "stdout_normalized_sha256",
        "stderr_file",
        "stderr_normalized_sha256",
    }
    for selection, record in zip(GATE_SELECTIONS, selections, strict=True):
        if not isinstance(record, dict) or set(record) != record_keys:
            raise ReleaseBuildError("gate evidence rejected")
        selection_id = str(selection["selection_id"])
        stdout_file = f"{selection_id}.stdout"
        stderr_file = f"{selection_id}.stderr"
        argv = list(selection["argv"])  # type: ignore[arg-type]
        if (
            record.get("selection_id") != selection_id
            or record.get("kind") != selection["kind"]
            or record.get("expected_count") != selection["expected_count"]
            or record.get("argv") != argv
            or record.get("argv_sha256")
            != hashlib.sha256(_canonical_json(argv)).hexdigest()
            or record.get("stdout_file") != stdout_file
            or record.get("stderr_file") != stderr_file
        ):
            raise ReleaseBuildError("gate evidence rejected")
        stdout_payload = payloads[_gate_payload_name(stdout_file)]
        stderr_payload = payloads[_gate_payload_name(stderr_file)]
        if (
            record.get("stdout_normalized_sha256")
            != hashlib.sha256(stdout_payload).hexdigest()
            or record.get("stderr_normalized_sha256")
            != hashlib.sha256(stderr_payload).hexdigest()
        ):
            raise ReleaseBuildError("gate evidence rejected")
        try:
            stdout = stdout_payload.decode("utf-8")
            stderr = stderr_payload.decode("utf-8")
        except UnicodeError:
            raise ReleaseBuildError("gate evidence rejected") from None
        if normalize_gate_output(stdout) != stdout or normalize_gate_output(stderr) != stderr:
            raise ReleaseBuildError("gate evidence rejected")
        parsed = parse_gate_result(
            selection,
            stdout=stdout,
            stderr=stderr,
            exit_code=int(record.get("exit_code", -1)),
        )
        if any(record.get(name) != value for name, value in parsed.items()):
            raise ReleaseBuildError("gate evidence rejected")
    return evidence


def _gate_evidence_record(
    bundle: Path,
    *,
    source_tree: Mapping[str, object],
    integration_source: Mapping[str, object],
    toolchain: Mapping[str, str],
) -> dict[str, object]:
    try:
        with tarfile.open(bundle, "r:gz") as archive:
            payloads = {
                member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
                for member in archive.getmembers()
                if member.isfile()
                and member.name.startswith(f"{GATE_EVIDENCE_ARCHIVE_ROOT}/")
            }
    except (OSError, tarfile.TarError):
        raise ReleaseBuildError("gate evidence rejected") from None
    evidence = validate_gate_evidence_payloads(
        payloads,
        source_tree=source_tree,
        integration_source=integration_source,
        toolchain=toolchain,
    )
    evidence_name = _gate_payload_name(GATE_EVIDENCE_NAME)
    evidence_payload = payloads[evidence_name]
    return {
        "attestation": evidence["attestation"],
        "trust_scope": evidence["trust_scope"],
        "path": evidence_name,
        "bytes": len(evidence_payload),
        "sha256": hashlib.sha256(evidence_payload).hexdigest(),
        "evidence_set_sha256": evidence["evidence_set_sha256"],
        "selection_count": len(evidence["selections"]),  # type: ignore[arg-type]
        "files": [
            {
                "path": name,
                "bytes": len(payloads[name]),
                "sha256": hashlib.sha256(payloads[name]).hexdigest(),
            }
            for name in sorted(payloads)
        ],
    }


def _raw_gate_audit_archive(payloads: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with (
        gzip.GzipFile(
            fileobj=output,
            mode="wb",
            filename="",
            mtime=SOURCE_DATE_EPOCH,
        ) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for name, payload in sorted(payloads.items()):
            info = tarfile.TarInfo(str(RAW_GATE_AUDIT_ARCHIVE_ROOT / name))
            info.size = len(payload)
            info.mtime = SOURCE_DATE_EPOCH
            info.mode = 0o600
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _raw_gate_audit_payloads(raw_audit_archive: Path) -> dict[str, bytes]:
    if not raw_audit_archive.is_absolute() or re.fullmatch(
        r"[0-9a-f]{64}\.tar\.gz",
        raw_audit_archive.name,
    ) is None:
        raise ReleaseBuildError("raw gate audit rejected")
    try:
        metadata = _verified_regular_metadata(raw_audit_archive)
        archive_payload = read_verified_regular_file(
            raw_audit_archive,
            maximum_bytes=64 * 1024 * 1024,
        )
    except ReleaseBuildError:
        raise ReleaseBuildError("raw gate audit rejected") from None
    if metadata.st_mode & 0o777 != 0o600:
        raise ReleaseBuildError("raw gate audit rejected")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_payload), mode="r:gz") as opened:
            members = opened.getmembers()
            if (
                len(members) != len({member.name for member in members})
                or any(
                    not member.isfile()
                    or PurePosixPath(member.name).parent
                    != RAW_GATE_AUDIT_ARCHIVE_ROOT
                    for member in members
                )
            ):
                raise ReleaseBuildError("raw gate audit rejected")
            payloads = {
                PurePosixPath(member.name).name: opened.extractfile(member).read()  # type: ignore[union-attr]
                for member in members
            }
    except (OSError, tarfile.TarError):
        raise ReleaseBuildError("raw gate audit rejected") from None
    if archive_payload != _raw_gate_audit_archive(payloads):
        raise ReleaseBuildError("raw gate audit rejected")
    return payloads


def _raw_gate_audit_record(
    *,
    raw_audit_archive: Path,
    source_tree: Mapping[str, object],
    integration_source: Mapping[str, object],
    toolchain: Mapping[str, str],
) -> dict[str, object]:
    payloads = _raw_gate_audit_payloads(raw_audit_archive)
    audit = validate_raw_gate_audit_payloads(
        payloads,
        source_tree=source_tree,
        integration_source=integration_source,
        toolchain=toolchain,
    )
    audit_id = str(audit.get("raw_audit_set_sha256", ""))
    if audit.get("raw_audit_set_sha256") != audit_id:
        raise ReleaseBuildError("raw gate audit rejected")
    if raw_audit_archive.name != f"{audit_id}.tar.gz":
        raise ReleaseBuildError("raw gate audit rejected")
    return {
        "audit_id": audit_id,
        "attestation": audit["attestation"],
        "trust_scope": audit["trust_scope"],
        "release_identity": False,
        "stable_evidence_set": False,
        "raw_audit_set_sha256": audit_id,
        "archive": {
            "filename": raw_audit_archive.name,
            "bytes": raw_audit_archive.stat().st_size,
            "sha256": _sha256(raw_audit_archive),
        },
        "files": [
            {
                "path": str(RAW_GATE_AUDIT_ARCHIVE_ROOT / name),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            for name, payload in sorted(payloads.items())
        ],
    }


def _integration_snapshot_declared_identity(
    archive_path: Path,
) -> dict[str, object]:
    """Read the declared source identity from an already-validated snapshot."""
    try:
        payload = read_verified_regular_file(archive_path)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as opened:
            member = next(
                entry
                for entry in opened.getmembers()
                if PurePosixPath(entry.name).name == INTEGRATION_SNAPSHOT_RECORD
            )
            record = _json_without_duplicate_keys(
                opened.extractfile(member).read().decode("ascii")  # type: ignore[union-attr]
            )
    except (StopIteration, KeyError, OSError, tarfile.TarError, UnicodeError, ReleaseBuildError):
        raise ReleaseBuildError("integration snapshot rejected") from None
    declared = record.get("declared_identity")
    if not isinstance(declared, dict):
        raise ReleaseBuildError("integration snapshot rejected")
    return declared


def _integration_snapshot_archive_record(
    archive_path: Path,
    identity: Mapping[str, object],
) -> dict[str, object]:
    verified = verify_integration_snapshot_binding(
        identity,
        archive_path,
        live_lock_path=None,
        live_workspace_root=None,
    )
    payload = read_verified_regular_file(archive_path)
    return {
        "snapshot_id": verified["snapshot_id"],
        "trust_scope": "immutable-required-integration-input",
        "archive": {
            "filename": archive_path.name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def _checksum_rows(payload: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in payload.splitlines():
        parts = line.split("  ", 1)
        if (
            len(parts) != 2
            or len(parts[0]) != 64
            or any(character not in "0123456789abcdef" for character in parts[0])
            or PurePosixPath(parts[1]).name != parts[1]
        ):
            raise ReleaseBuildError("release checksums rejected")
        rows.append((parts[0], parts[1]))
    if [name for _digest, name in rows] != list(CHECKSUMMED_NAMES):
        raise ReleaseBuildError("release checksums rejected")
    return rows


def _local_gate_evidence_payloads() -> dict[str, bytes]:
    directory = current_version_directory(GATE_EVIDENCE_DIRECTORY)
    expected_names = {GATE_EVIDENCE_NAME}
    for selection in GATE_SELECTIONS:
        selection_id = str(selection["selection_id"])
        expected_names.update(
            {f"{selection_id}.stdout", f"{selection_id}.stderr"}
        )
    entries = tuple(directory.iterdir())
    if {entry.name for entry in entries} != expected_names or any(
        entry.is_symlink()
        or not entry.is_file()
        or os.lstat(entry).st_nlink != 1
        for entry in entries
    ):
        raise ReleaseBuildError("gate evidence rejected")
    return {
        _gate_payload_name(entry.name): read_verified_regular_file(entry)
        for entry in entries
    }


def validate_bootstrap_gate_evidence_directory(directory: Path) -> None:
    """Allow only the immediately preceding evidence schema as bounded input."""
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseBuildError("gate evidence rejected")
    entries = tuple(directory.iterdir())
    if len(entries) != 17 or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise ReleaseBuildError("gate evidence rejected")
    payloads = {entry.name: entry.read_bytes() for entry in entries}
    try:
        for name, payload in payloads.items():
            if name != GATE_EVIDENCE_NAME:
                validate_raw_gate_output(payload)
        if len(payloads[GATE_EVIDENCE_NAME]) > 1024 * 1024:
            raise ReleaseBuildError("gate evidence rejected")
        evidence = _json_without_duplicate_keys(
            payloads[GATE_EVIDENCE_NAME].decode("utf-8")
        )
    except (KeyError, UnicodeError, ReleaseBuildError):
        raise ReleaseBuildError("gate evidence rejected") from None
    selections = evidence.get("selections")
    if (
        evidence.get("schema_version")
        not in {GATE_EVIDENCE_SCHEMA_VERSION - 1, GATE_EVIDENCE_SCHEMA_VERSION}
        or evidence.get("deterministic") is not True
        or evidence.get("attestation") != "untrusted/self-recorded"
        or evidence.get("trust_scope") != "integrity-and-replay-only"
        or evidence.get("normalized_output_scope")
        != "exact-pytest-unittest-summary-lines-only"
        or not isinstance(selections, list)
        or len(selections) != len(GATE_SELECTIONS)
    ):
        raise ReleaseBuildError("gate evidence rejected")
    expected_names = {GATE_EVIDENCE_NAME}
    seen: set[str] = set()
    for record in selections:
        selection_id = record.get("selection_id") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or not isinstance(selection_id, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", selection_id) is None
            or selection_id in seen
            or record.get("selection_id") != selection_id
            or record.get("stdout_file") != f"{selection_id}.stdout"
            or record.get("stderr_file") != f"{selection_id}.stderr"
        ):
            raise ReleaseBuildError("gate evidence rejected")
        seen.add(selection_id)
        expected_names.update({f"{selection_id}.stdout", f"{selection_id}.stderr"})
    if set(payloads) != expected_names:
        raise ReleaseBuildError("gate evidence rejected")


def require_release_manifest_schema(manifest: Mapping[str, object]) -> None:
    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise ReleaseBuildError("release manifest schema rejected")


def verify_release_directory(
    output: Path,
    *,
    raw_audit_archive: Path | None = None,
    integration_snapshot_archive: Path | None = None,
) -> dict[str, object]:
    if output.is_symlink():
        raise ReleaseBuildError("release directory rejected")
    output = output.resolve()
    _lstat_chain(output)
    if not output.is_dir() or output.is_symlink():
        raise ReleaseBuildError("release directory rejected")
    actual_entries = {entry.name for entry in output.iterdir()}
    if output.resolve() == DIST_ROOT.resolve() and ".gitignore" in actual_entries:
        actual_entries.remove(".gitignore")
    if actual_entries != set(RELEASE_FILE_NAMES):
        raise ReleaseBuildError("release directory rejected")
    if raw_audit_archive is None:
        raise ReleaseBuildError("raw gate audit archive required")
    private_parent = Path(
        tempfile.mkdtemp(
            prefix="hermes-release-verify-",
            dir=Path(tempfile.gettempdir()).resolve(),
        )
    )
    private = private_parent / "candidate"
    try:
        output = snapshot_release_directory(output, private)
        source_snapshot, _source_identity = create_release_source_snapshot(
            private_parent
        )
        private_raw = snapshot_external_archive(
            raw_audit_archive,
            private_parent / raw_audit_archive.name,
        )
        if integration_snapshot_archive is not None:
            private_integration: Path | None = snapshot_external_archive(
                integration_snapshot_archive,
                private_parent / integration_snapshot_archive.name,
            )
        else:
            private_integration = None
        return _verify_release_snapshot(
            output,
            raw_audit_archive=private_raw,
            integration_snapshot_archive=private_integration,
            source_root=source_snapshot,
        )
    finally:
        if private_parent.exists():
            shutil.rmtree(private_parent)


def _verify_release_snapshot(
    output: Path,
    *,
    raw_audit_archive: Path | None,
    integration_snapshot_archive: Path | None,
    source_root: Path,
) -> dict[str, object]:
    expected_entries = set(RELEASE_FILE_NAMES)
    actual_entries = {entry.name for entry in output.iterdir()}
    if output == DIST_ROOT.resolve() and ".gitignore" in actual_entries:
        actual_entries.remove(".gitignore")
    if actual_entries != expected_entries:
        raise ReleaseBuildError("release directory rejected")
    files = {name: output / name for name in RELEASE_FILE_NAMES}
    if any(path.is_symlink() or not path.is_file() for path in files.values()):
        raise ReleaseBuildError("release artifact rejected")

    checksum_rows = _checksum_rows(files[CHECKSUMS_NAME].read_text(encoding="utf-8"))
    for expected_digest, name in checksum_rows:
        if _sha256(files[name]) != expected_digest:
            raise ReleaseBuildError("release artifact rejected")
    _validate_release_archive_members(files, source_root=source_root)

    manifest = _json_without_duplicate_keys(files[MANIFEST_NAME].read_text(encoding="utf-8"))
    require_release_manifest_schema(manifest)
    manifest_raw_audit = manifest.get("raw_gate_audit")
    if not isinstance(manifest_raw_audit, dict):
        raise ReleaseBuildError("release manifest rejected")
    if raw_audit_archive is None:
        raise ReleaseBuildError("raw gate audit archive required")
    source_tree = _source_tree_identity(source_root)
    manifest_integration_source = manifest.get("integration_source")
    if not isinstance(manifest_integration_source, dict):
        raise ReleaseBuildError("release manifest rejected")
    live_workspace = CLOUD_ROOT.parent
    live_available = all(
        (live_workspace / root).is_dir() and not (live_workspace / root).is_symlink()
        for root in _INTEGRATION_SOURCE_ROOTS
    )
    integration_snapshot_record = manifest.get("integration_snapshot")
    if not isinstance(integration_snapshot_record, dict):
        raise ReleaseBuildError("release manifest rejected")
    if integration_snapshot_archive is not None:
        snapshot_identity = verify_integration_snapshot_binding(
            validate_integration_snapshot_archive(integration_snapshot_archive),
            integration_snapshot_archive,
            live_lock_path=INTEGRATION_SOURCE_LOCK if live_available else None,
            live_workspace_root=live_workspace if live_available else None,
        )
        declared = _integration_snapshot_declared_identity(
            integration_snapshot_archive
        )
        if (
            snapshot_identity.get("file_count") != declared.get("file_count")
            or snapshot_identity.get("source_sha256") != declared.get("sha256")
        ):
            raise ReleaseBuildError("integration snapshot rejected")
        if integration_snapshot_record != _integration_snapshot_archive_record(
            integration_snapshot_archive,
            snapshot_identity,
        ):
            raise ReleaseBuildError("release manifest rejected")
    else:
        if not live_available:
            raise ReleaseBuildError("integration snapshot archive required")
        try:
            declared = validate_integration_source_inputs(
                INTEGRATION_SOURCE_LOCK,
                live_workspace,
            )
        except ReleaseBuildError:
            raise ReleaseBuildError("integration snapshot rejected") from None
        archive_record = integration_snapshot_record.get("archive")
        snapshot_id = integration_snapshot_record.get("snapshot_id")
        if (
            not isinstance(snapshot_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", snapshot_id) is None
            or integration_snapshot_record.get("trust_scope")
            != "immutable-required-integration-input"
            or not isinstance(archive_record, dict)
            or archive_record.get("filename") != f"{snapshot_id}.tar.gz"
            or not isinstance(archive_record.get("bytes"), int)
            or archive_record.get("bytes", 0) <= 0
            or not isinstance(archive_record.get("sha256"), str)
            or archive_record.get("sha256") != snapshot_id
        ):
            raise ReleaseBuildError("release manifest rejected")
    if dict(manifest_integration_source) != declared:
        raise ReleaseBuildError("integration snapshot rejected")
    integration_source = declared
    toolchain = _toolchain(os.environ)
    raw_gate_audit = _raw_gate_audit_record(
        raw_audit_archive=raw_audit_archive,
        source_tree=source_tree,
        integration_source=integration_source,
        toolchain=toolchain,
    )
    expected_manifest = _manifest(
        output,
        toolchain=toolchain,
        source_tree=source_tree,
        integration_source=integration_source,
        integration_snapshot_record=integration_snapshot_record,
        raw_gate_audit=raw_gate_audit,
    )
    if manifest != expected_manifest:
        raise ReleaseBuildError("release manifest rejected")
    if files[SDIST_HASH_NAME].read_text(encoding="ascii").strip() != _sha256(files[SDIST_NAME]):
        raise ReleaseBuildError("release artifact rejected")
    try:
        _bundle_builder()["_validate_wheel"](  # type: ignore[operator]
            files[WHEEL_NAME],
            source_root,
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise ReleaseBuildError("release artifact rejected") from None
    return manifest


def build_release(
    environment: Mapping[str, str] = os.environ,
    *,
    raw_audit_archive: Path | None = None,
    integration_snapshot_archive: Path | None = None,
) -> dict[str, object]:
    DIST_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_release_lock(
        DIST_ROOT / ".release-state.lock",
        timeout_seconds=5.0,
    ):
        return _build_release_locked(
            environment,
            raw_audit_archive=raw_audit_archive,
            integration_snapshot_archive=integration_snapshot_archive,
        )


def _build_release_locked(
    environment: Mapping[str, str],
    *,
    raw_audit_archive: Path | None,
    integration_snapshot_archive: Path | None,
) -> dict[str, object]:
    toolchain = require_build_environment(environment)
    if raw_audit_archive is None:
        raise ReleaseBuildError("raw gate audit archive required")
    work = Path(tempfile.mkdtemp(prefix=".release-build-", dir=DIST_ROOT))
    os.chmod(work, 0o700)
    try:
        source_root, source_tree = create_release_source_snapshot(work)
        private_raw = snapshot_external_archive(
            raw_audit_archive,
            work / raw_audit_archive.name,
        )
        if integration_snapshot_archive is None:
            live_workspace = CLOUD_ROOT.parent
            live_available = all(
                (live_workspace / root).is_dir()
                and not (live_workspace / root).is_symlink()
                for root in _INTEGRATION_SOURCE_ROOTS
            )
            if live_available:
                generated_snapshot = create_integration_source_snapshot()
                generated_archive = Path(generated_snapshot["archive"])
            else:
                snapshot_store = current_version_directory(
                    INTEGRATION_SNAPSHOT_STORE
                )
                generated_archive = snapshot_store / f"{snapshot_store.name}.tar.gz"
            private_integration = work / generated_archive.name
            _write_private_file(
                private_integration,
                read_verified_regular_file(generated_archive),
            )
        else:
            live_workspace = CLOUD_ROOT.parent
            live_available = all(
                (live_workspace / root).is_dir()
                and not (live_workspace / root).is_symlink()
                for root in _INTEGRATION_SOURCE_ROOTS
            )
            private_integration = snapshot_external_archive(
                integration_snapshot_archive,
                work / integration_snapshot_archive.name,
            )
        integration_source = verify_integration_snapshot_binding(
            validate_integration_snapshot_archive(private_integration),
            private_integration,
            live_lock_path=INTEGRATION_SOURCE_LOCK if live_available else None,
            live_workspace_root=live_workspace if live_available else None,
        )
        if live_available:
            declared_integration = validate_integration_source_inputs()
        else:
            declared_integration = _integration_snapshot_declared_identity(
                private_integration
            )
        if (
            integration_source.get("file_count")
            != declared_integration.get("file_count")
            or integration_source.get("source_sha256")
            != declared_integration.get("sha256")
        ):
            raise ReleaseBuildError("integration snapshot rejected")
        integration_snapshot_record = _integration_snapshot_archive_record(
            private_integration,
            integration_source,
        )
        evidence_directory = current_version_directory(GATE_EVIDENCE_DIRECTORY)
        validate_gate_evidence_payloads(
            _local_gate_evidence_payloads(),
            source_tree=source_tree,
            integration_source=declared_integration,
            toolchain=toolchain,
        )
        raw_gate_audit = _raw_gate_audit_record(
            raw_audit_archive=private_raw,
            source_tree=source_tree,
            integration_source=declared_integration,
            toolchain=toolchain,
        )
        output = work / "candidate"
        output.mkdir(mode=0o700)

        build_environment = dict(environment)
        for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
            build_environment.pop(name, None)
        build_environment["PYTHONNOUSERSITE"] = "1"
        uv_name = shutil.which("uv", path=build_environment.get("PATH"))
        assert uv_name is not None
        completed = subprocess.run(
            distribution_build_command(
                Path(uv_name),
                Path(sys.executable),
                output,
            ),
            cwd=source_root,
            env=build_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ReleaseBuildError("release distribution build failed")

        uv_outdir_marker = output / ".gitignore"
        if uv_outdir_marker.exists():
            if (
                not uv_outdir_marker.is_file()
                or uv_outdir_marker.is_symlink()
                or uv_outdir_marker.read_bytes() != b"*"
            ):
                raise ReleaseBuildError("release distribution build failed")
            uv_outdir_marker.unlink()

        wheel = output / WHEEL_NAME
        sdist = output / SDIST_NAME
        try:
            _verified_regular_metadata(wheel)
            _verified_regular_metadata(sdist)
        except ReleaseBuildError:
            raise ReleaseBuildError("release distribution build failed") from None
        _write_private_file(
            output / SDIST_HASH_NAME,
            f"{_sha256(sdist)}\n".encode("ascii"),
        )
        try:
            _bundle_builder()["build_bundle"](  # type: ignore[operator]
                wheel,
                output / BUNDLE_NAME,
                project_root=source_root,
                evidence_directory=evidence_directory,
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReleaseBuildError("release bundle build failed") from None

        manifest = _manifest(
            output,
            toolchain=toolchain,
            source_tree=source_tree,
            integration_source=declared_integration,
            integration_snapshot_record=integration_snapshot_record,
            raw_gate_audit=raw_gate_audit,
        )
        _write_private_file(
            output / MANIFEST_NAME,
            (
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True)
                + "\n"
            ).encode("utf-8"),
        )
        _write_private_file(
            output / CHECKSUMS_NAME,
            "".join(
                f"{_sha256(output / name)}  {name}\n"
                for name in CHECKSUMMED_NAMES
            ).encode("ascii"),
        )
        verified = _verify_release_snapshot(
            output,
            raw_audit_archive=private_raw,
            integration_snapshot_archive=private_integration,
            source_root=source_root,
        )
        release_identity = verified.get("release_identity")
        if not isinstance(release_identity, dict):
            raise ReleaseBuildError("release manifest rejected")
        version_id = release_identity.get("description_sha256")
        if not isinstance(version_id, str):
            raise ReleaseBuildError("release manifest rejected")
        promoted = atomic_promote_version(
            output,
            DIST_RELEASE_STORE,
            version_id,
        )
        for name in RELEASE_FILE_NAMES:
            flat = DIST_ROOT / name
            if flat.exists() or flat.is_symlink():
                if flat.is_symlink() or not flat.is_file():
                    raise ReleaseBuildError("release artifact rejected")
                flat.unlink()
            _write_private_file(
                flat,
                read_verified_regular_file(promoted / name),
            )
        return verified
    finally:
        if work.exists():
            _thaw_tree(work)
            shutil.rmtree(work)


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or verify the fixed Hermes Cloud release set",
    )
    parser.add_argument(
        "--verify-only",
        type=Path,
        help="verify one already-uploaded release directory without unpacking",
    )
    parser.add_argument(
        "--integration-snapshot-archive",
        required=False,
        type=Path,
        default=None,
        help="absolute path to the content-addressed immutable integration snapshot archive; when omitted, build generates and persists the snapshot and verify binds against the live declared lock",
    )
    parser.add_argument(
        "--raw-audit-archive",
        required=True,
        type=Path,
        help="absolute path to the independently transferred content-addressed raw audit archive",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        if arguments.verify_only is not None:
            manifest = verify_release_directory(
                arguments.verify_only,
                raw_audit_archive=arguments.raw_audit_archive,
                integration_snapshot_archive=arguments.integration_snapshot_archive,
            )
        else:
            manifest = build_release(
                raw_audit_archive=arguments.raw_audit_archive,
                integration_snapshot_archive=arguments.integration_snapshot_archive,
            )
    except ReleaseBuildError as error:
        print(str(error), file=sys.stderr)
        return 78
    except (OSError, subprocess.SubprocessError, ValueError):
        print("release build failed", file=sys.stderr)
        return 78
    print(f"release_artifacts=PASS release_id={manifest['release_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
