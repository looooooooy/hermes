#!/usr/bin/env python3
"""Build the trusted Hermes local Managed Release installer as a deterministic zipapp."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "hermes-connector" / "packaging" / "common"
ENTRYPOINT = ROOT / "hermes-desktop" / "managed-release" / "install_managed_release_payload.py"
MODULES = (
    "hermes_local_release.py",
    "hermes_managed_release.py",
    "hermes_offline_wheelhouse.py",
    "hermes_private_toolchain.py",
    "hermes_target_runtime_plan.py",
)
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


class ZipappBuildError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise ZipappBuildError("output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)

    sources: list[tuple[str, Path]] = [("__main__.py", canonical_file(ENTRYPOINT))]
    sources.extend((name, canonical_file(COMMON / name)) for name in MODULES)
    manifest = {
        "schema_version": 1,
        "scope": "hermes_managed_release_installer_zipapp",
        "python_isolation_required": True,
        "network_install_allowed": False,
        "modules": [
            {
                "path": archive_name,
                "sha256": sha256_file(source),
                "size_bytes": source.stat().st_size,
            }
            for archive_name, source in sources
        ],
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")

    with zipfile.ZipFile(
        output,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for archive_name, source in sorted(sources):
            write_entry(archive, archive_name, source.read_bytes(), mode=0o444)
        write_entry(archive, "INSTALLER-MANIFEST.json", manifest_bytes, mode=0o444)

    verify_zipapp(output, manifest)
    if output.is_symlink() or not output.is_file():
        raise ZipappBuildError("zipapp output is not a regular file")
    if output.stat().st_size == 0 or output.stat().st_size > 4 * 1024 * 1024:
        raise ZipappBuildError("zipapp output size is outside the bounded limit")
    print(
        json.dumps(
            {
                "schema_version": 1,
                "path": str(output),
                "sha256": sha256_file(output),
                "size_bytes": output.stat().st_size,
                "module_count": len(sources),
                "deterministic_zip_metadata": True,
            },
            sort_keys=True,
        )
    )
    return 0


def write_entry(archive: zipfile.ZipFile, name: str, payload: bytes, *, mode: int) -> None:
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(
        info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
    )


def verify_zipapp(path: Path, manifest: dict[str, object]) -> None:
    expected = {"__main__.py", *MODULES, "INSTALLER-MANIFEST.json"}
    with zipfile.ZipFile(path, "r") as archive:
        names = set(archive.namelist())
        if names != expected:
            raise ZipappBuildError("zipapp file set is invalid")
        if archive.testzip() is not None:
            raise ZipappBuildError("zipapp CRC verification failed")
        observed_manifest = json.loads(archive.read("INSTALLER-MANIFEST.json"))
        if observed_manifest != manifest:
            raise ZipappBuildError("zipapp manifest changed during packaging")
        declared = {item["path"]: item for item in manifest["modules"]}  # type: ignore[index]
        for name in expected - {"INSTALLER-MANIFEST.json"}:
            payload = archive.read(name)
            item = declared[name]
            if (
                len(payload) != item["size_bytes"]
                or hashlib.sha256(payload).hexdigest() != item["sha256"]
            ):
                raise ZipappBuildError(f"zipapp module integrity failed: {name}")


def canonical_file(path: Path) -> Path:
    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise ZipappBuildError(f"trusted installer source is invalid: {path}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ZipappBuildError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"managed_release_installer_zipapp_error: {exc}") from exc
