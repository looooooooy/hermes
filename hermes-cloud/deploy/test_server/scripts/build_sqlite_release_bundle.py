"""Build one allowlisted, reproducible SQLite release-candidate archive."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from base64 import urlsafe_b64encode
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

CLOUD_ROOT = Path(__file__).resolve().parents[3]
ARCHIVE_ROOT = PurePosixPath("hermes-cloud")
_GATE_SELECTION_IDS = (
    "migration",
    "compatibility",
    "required_integration",
    "release_artifacts",
    "release_validation",
    "architecture_distribution",
    "cloud",
    "ruff",
)
_GATE_EVIDENCE_FILES = (
    "deploy/test_server/release_evidence/GATE-EVIDENCE.json",
    *(
        f"deploy/test_server/release_evidence/{selection_id}.{stream}"
        for selection_id in _GATE_SELECTION_IDS
        for stream in ("stdout", "stderr")
    ),
)

_REQUIRED_RELEASE_FILE_METADATA = (
    ("pyproject.toml", 0o644),
    ("uv.lock", 0o644),
    ("deploy/test_server/scripts/build_release.py", 0o644),
    ("deploy/test_server/scripts/run_release_gates.py", 0o755),
    ("deploy/test_server/scripts/cleanup_test_seed_session.py", 0o644),
    ("deploy/test_server/scripts/migrate_sqlite.py", 0o644),
    ("deploy/test_server/scripts/mint_connector_token.py", 0o644),
    ("deploy/test_server/scripts/rollback.sh", 0o755),
    ("deploy/test_server/scripts/run_asgi.sh", 0o755),
    ("deploy/test_server/scripts/seed_test_data.py", 0o644),
    ("deploy/test_server/scripts/validate.sh", 0o755),
    ("deploy/test_server/sqlite/README.md", 0o644),
    ("deploy/test_server/sqlite/env/test-server.env.example", 0o644),
    ("deploy/test_server/sqlite/nginx/hermes-test-server.conf", 0o644),
    ("deploy/test_server/sqlite/scripts/preflight.sh", 0o755),
    ("deploy/test_server/sqlite/scripts/run_candidate_command.py", 0o644),
    (
        "deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-business-api.service",
        0o644,
    ),
    (
        "deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-connector-gateway.service",
        0o644,
    ),
    ("deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-migrate.service", 0o644),
    (
        "deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-mint-connector-token.service",
        0o644,
    ),
    (
        "deploy/test_server/sqlite/systemd/hermes-cloud-sqlite-seed-test-data.service",
        0o644,
    ),
    ("deploy/test_server/tests/test_cleanup_test_seed_session_release.py", 0o644),
    ("deploy/test_server/tests/test_mint_connector_token.py", 0o644),
    ("deploy/test_server/tests/test_seed_test_data.py", 0o644),
    ("deploy/test_server/tests/test_sqlite_deploy_artifacts.py", 0o644),
    *((relative, 0o644) for relative in _GATE_EVIDENCE_FILES),
)
REQUIRED_RELEASE_FILES = tuple(name for name, _mode in _REQUIRED_RELEASE_FILE_METADATA)
REQUIRED_RELEASE_FILE_MODES = dict(_REQUIRED_RELEASE_FILE_METADATA)

_FORBIDDEN_COMPONENTS = frozenset({".git", ".venv", "__pycache__", "secrets"})
_FORBIDDEN_FILENAMES = frozenset({".ds_store", "connector.token", "test-server.env"})
_FORBIDDEN_SUFFIXES = frozenset({".db", ".key", ".pem", ".pyc", ".sqlite", ".sqlite3"})
_WHEEL_NAME = "hermes_cloud-0.1.0-py3-none-any.whl"
_DIST_INFO = "hermes_cloud-0.1.0.dist-info"
_DIST_INFO_NAMES = frozenset(
    {
        f"{_DIST_INFO}/METADATA",
        f"{_DIST_INFO}/RECORD",
        f"{_DIST_INFO}/WHEEL",
    }
)
_WHEEL_TIMESTAMP = (2026, 8, 2, 0, 0, 0)


class CandidateBundleError(RuntimeError):
    """Raised when a release candidate is outside the reviewed allowlist."""


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the reviewed Hermes Cloud SQLite release bundle",
    )
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def _unsafe_archive_path(name: str) -> bool:
    path = PurePosixPath(name)
    lowered_parts = tuple(part.casefold() for part in path.parts)
    filename = path.name
    lowered_filename = filename.casefold()
    return (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "tests" in lowered_parts
        or any(part in _FORBIDDEN_COMPONENTS for part in lowered_parts)
        or filename.startswith("._")
        or lowered_filename in _FORBIDDEN_FILENAMES
        or Path(lowered_filename).suffix in _FORBIDDEN_SUFFIXES
    )


def _verified_payload(path: Path) -> bytes:
    absolute = path if path.is_absolute() else Path.cwd() / path
    if ".." in absolute.parts:
        raise CandidateBundleError("candidate bundle source rejected")
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError:
            raise CandidateBundleError("candidate bundle source rejected") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise CandidateBundleError("candidate bundle source rejected")
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CandidateBundleError("candidate bundle source rejected")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise CandidateBundleError("candidate bundle source rejected")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (after.st_dev, after.st_ino, after.st_size) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
    ):
        raise CandidateBundleError("candidate bundle source rejected")
    return b"".join(chunks)


def _expected_wheel_payloads(project_root: Path = CLOUD_ROOT) -> dict[str, bytes]:
    source_root = project_root / "src" / "hermes_cloud"
    payloads: dict[str, bytes] = {}
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(project_root / "src")
        if "__pycache__" in relative.parts or source.suffix == ".pyc":
            continue
        if source.is_symlink():
            raise CandidateBundleError("candidate wheel source rejected")
        if source.is_dir():
            continue
        if not source.is_file():
            raise CandidateBundleError("candidate wheel source rejected")
        name = relative.as_posix()
        if _unsafe_archive_path(name):
            raise CandidateBundleError("candidate wheel source rejected")
        payloads[name] = _verified_payload(source)
    if not payloads:
        raise CandidateBundleError("candidate wheel source rejected")
    return payloads


def _normalized_metadata(
    payload: bytes,
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], str]:
    try:
        message = BytesParser(policy=policy.default).parsebytes(payload)
    except (UnicodeError, ValueError):
        raise CandidateBundleError("candidate wheel rejected") from None
    if message.defects or message.is_multipart():
        raise CandidateBundleError("candidate wheel rejected")
    headers: dict[str, list[str]] = {}
    for name, value in message.items():
        headers.setdefault(name.casefold(), []).append(" ".join(str(value).split()))
    body = message.get_payload()
    if not isinstance(body, str):
        raise CandidateBundleError("candidate wheel rejected")
    normalized_headers = tuple(
        sorted((name, tuple(sorted(values))) for name, values in headers.items())
    )
    return normalized_headers, body.replace("\r\n", "\n").replace("\r", "\n")


def _clean_build_metadata(project_root: Path = CLOUD_ROOT) -> bytes:
    with tempfile.TemporaryDirectory(prefix="hermes-cloud-metadata-") as temporary:
        environment = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
            environment.pop(name, None)
        environment["PYTHONNOUSERSITE"] = "1"
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                temporary,
            ),
            cwd=project_root,
            env=environment,
            capture_output=True,
            check=False,
        )
        wheel = Path(temporary) / _WHEEL_NAME
        if completed.returncode != 0 or wheel.is_symlink() or not wheel.is_file():
            raise CandidateBundleError("candidate wheel rejected")
        try:
            with ZipFile(wheel) as archive:
                return archive.read(f"{_DIST_INFO}/METADATA")
        except (BadZipFile, KeyError, OSError):
            raise CandidateBundleError("candidate wheel rejected") from None


def _require_controlled_metadata(
    payloads: dict[str, bytes],
    project_root: Path = CLOUD_ROOT,
) -> None:
    try:
        wheel_metadata = BytesParser(policy=policy.default).parsebytes(
            payloads[f"{_DIST_INFO}/WHEEL"]
        )
    except (KeyError, UnicodeError, ValueError):
        raise CandidateBundleError("candidate wheel rejected") from None
    if (
        _normalized_metadata(payloads[f"{_DIST_INFO}/METADATA"])
        != _normalized_metadata(_clean_build_metadata(project_root))
        or wheel_metadata.get_all("Wheel-Version") != ["1.0"]
        or wheel_metadata.get_all("Generator") != ["hatchling 1.31.0"]
        or wheel_metadata.get_all("Root-Is-Purelib") != ["true"]
        or wheel_metadata.get_all("Tag") != ["py3-none-any"]
    ):
        raise CandidateBundleError("candidate wheel rejected")


def _require_complete_record(payloads: dict[str, bytes]) -> None:
    record_name = f"{_DIST_INFO}/RECORD"
    try:
        rows = tuple(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"))))
    except (KeyError, UnicodeError, csv.Error):
        raise CandidateBundleError("candidate wheel rejected") from None
    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in records:
            raise CandidateBundleError("candidate wheel rejected")
        records[row[0]] = (row[1], row[2])
    if set(records) != set(payloads) or records.get(record_name) != ("", ""):
        raise CandidateBundleError("candidate wheel rejected")
    for name, payload in payloads.items():
        if name == record_name:
            continue
        digest = urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        if records[name] != (f"sha256={digest.decode()}", str(len(payload))):
            raise CandidateBundleError("candidate wheel rejected")


def _validate_wheel(wheel: Path, project_root: Path = CLOUD_ROOT) -> None:
    if wheel.name != _WHEEL_NAME:
        raise CandidateBundleError("candidate wheel rejected")
    try:
        wheel_payload = _verified_payload(wheel)
        with ZipFile(io.BytesIO(wheel_payload)) as archive:
            infos = tuple(archive.infolist())
            names = tuple(info.filename for info in infos)
            bad_member = archive.testzip()
            payloads = {name: archive.read(name) for name in names}
            archive_comment = archive.comment
    except (BadZipFile, OSError):
        raise CandidateBundleError("candidate wheel rejected") from None
    expected_payloads = _expected_wheel_payloads(project_root)
    if (
        not names
        or len(names) != len(set(names))
        or bad_member is not None
        or archive_comment != b""
        or any(_unsafe_archive_path(name) for name in names)
        or any(
            info.date_time != _WHEEL_TIMESTAMP
            or info.compress_type != ZIP_DEFLATED
            or info.create_system != 3
            or info.create_version != 20
            or info.extract_version != 20
            or info.flag_bits != 0
            or info.volume != 0
            or info.internal_attr != 0
            or info.external_attr
            != ((0o100644 if info.filename.startswith("hermes_cloud/") else 0o644) << 16)
            or info.extra != b""
            or info.comment != b""
            for info in infos
        )
        or set(names) != set(expected_payloads) | _DIST_INFO_NAMES
        or any(payloads[name] != payload for name, payload in expected_payloads.items())
    ):
        raise CandidateBundleError("candidate wheel rejected")
    _require_controlled_metadata(payloads, project_root)
    _require_complete_record(payloads)


def _reviewed_sources(
    wheel: Path,
    *,
    project_root: Path = CLOUD_ROOT,
    evidence_directory: Path | None = None,
) -> tuple[tuple[PurePosixPath, bytes, int], ...]:
    if evidence_directory is None:
        evidence_directory = CLOUD_ROOT / "deploy/test_server/release_evidence"
    sources = [
        (ARCHIVE_ROOT / "artifacts" / wheel.name, _verified_payload(wheel), 0o644),
        *(
            (
                ARCHIVE_ROOT / relative,
                _verified_payload(
                    evidence_directory / Path(relative).name
                    if relative in _GATE_EVIDENCE_FILES
                    else project_root / relative
                ),
                REQUIRED_RELEASE_FILE_MODES[relative],
            )
            for relative in REQUIRED_RELEASE_FILES
        ),
    ]
    return tuple(sorted(sources, key=lambda item: item[0].as_posix()))


def _add_file(
    archive: tarfile.TarFile,
    archive_path: PurePosixPath,
    payload: bytes,
    mode: int,
) -> None:
    info = tarfile.TarInfo(archive_path.as_posix())
    info.size = len(payload)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    archive.addfile(info, io.BytesIO(payload))


def build_bundle(
    wheel: Path,
    output: Path,
    *,
    project_root: Path = CLOUD_ROOT,
    evidence_directory: Path | None = None,
) -> tuple[int, str]:
    if output.exists() or output.suffixes[-2:] != [".tar", ".gz"]:
        raise CandidateBundleError("candidate output rejected")
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_wheel(wheel, project_root)
    sources = _reviewed_sources(
        wheel,
        project_root=project_root,
        evidence_directory=evidence_directory,
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        with (
            os.fdopen(descriptor, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
            tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as archive,
        ):
            for archive_path, payload, mode in sources:
                _add_file(archive, archive_path, payload, mode)
        temporary.chmod(0o644)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return len(sources), digest


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        file_count, digest = build_bundle(arguments.wheel, arguments.output)
    except CandidateBundleError as error:
        print(str(error), file=sys.stderr)
        return 78
    except (OSError, ValueError):
        print("candidate bundle build failed", file=sys.stderr)
        return 78
    print(f"sqlite_release_bundle=PASS file_count={file_count} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
