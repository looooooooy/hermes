#!/usr/bin/env python3
"""Build one Hermes private Python/uv toolchain bundle from an immutable upstream lock.

This script is CI-only.  Customer machines consume the resulting Hermes artifact and
never download Python or uv from upstream release sites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_DOWNLOAD_BYTES = 1_500_000_000
MAX_FILES = 100_000
ALLOWED_HOSTS = {"github.com"}


class BundleBuildError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-cross-target",
        action="store_true",
        help="Allow packaging a target that does not match the current CI host.",
    )
    args = parser.parse_args()

    lock = load_json(args.lock)
    target = require_target(lock, args.target)
    if not args.allow_cross_target:
        validate_host(target)

    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        raise BundleBuildError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hermes-toolchain-") as temp_value:
        temp = Path(temp_value)
        python_archive = download_locked(target["python"], temp)
        uv_archive = download_locked(target["uv"], temp)

        python_extract = temp / "python-extract"
        uv_extract = temp / "uv-extract"
        extract_archive(python_archive, python_extract)
        extract_archive(uv_archive, uv_extract)

        stage = temp / "bundle"
        stage.mkdir(mode=0o700)
        copy_python_tree(python_extract, stage / "python")
        copy_uv_binary(uv_extract, stage / "uv", target["platform"])

        python_path = Path(target["python_executable"])
        uv_path = Path(target["uv_executable"])
        require_executable(stage / python_path, "private Python")
        require_executable(stage / uv_path, "private uv")

        files = enumerate_bundle_files(stage, {python_path, uv_path})
        bundle_id = (
            f"cpython-{lock['python']['version']}-uv-{lock['uv']['version']}-"
            f"{lock['python']['release']}-{args.target}"
        )
        manifest = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "platform": target["platform"],
            "architecture": target["architecture"],
            "python_version": lock["python"]["version"],
            "uv_version": lock["uv"]["version"],
            "python_path": python_path.as_posix(),
            "uv_path": uv_path.as_posix(),
            "files": files,
        }
        write_json(stage / "TOOLCHAIN-BUNDLE.json", manifest)
        write_json(
            stage / "UPSTREAM-SOURCE.json",
            {
                "schema_version": 1,
                "lock_sha256": sha256_file(args.lock.resolve()),
                "target": args.target,
                "python": target["python"],
                "uv": target["uv"],
            },
        )
        os.replace(stage, output)

    summary = {
        "bundle_root": str(output),
        "bundle_id": manifest["bundle_id"],
        "files": len(files),
        "python_sha256": sha256_file(output / python_path),
        "uv_sha256": sha256_file(output / uv_path),
        "manifest_sha256": sha256_file(output / "TOOLCHAIN-BUNDLE.json"),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def load_json(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if path.is_symlink() or not path.is_file():
        raise BundleBuildError("lock file must be a regular non-symlink file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise BundleBuildError("unsupported upstream lock schema")
    if value.get("policy", {}).get("client_network_install") is not False:
        raise BundleBuildError("upstream lock must forbid client network installation")
    return value


def require_target(lock: dict[str, Any], name: str) -> dict[str, Any]:
    target = lock.get("targets", {}).get(name)
    if not isinstance(target, dict):
        raise BundleBuildError(f"unknown locked target: {name}")
    for component in ("python", "uv"):
        source = target.get(component)
        if not isinstance(source, dict):
            raise BundleBuildError(f"missing {component} source")
        if not is_sha256(source.get("sha256")):
            raise BundleBuildError(f"invalid {component} sha256")
        parsed = urllib.parse.urlparse(str(source.get("url", "")))
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise BundleBuildError(f"unapproved {component} download URL")
    return target


def validate_host(target: dict[str, Any]) -> None:
    host_platform = {
        "Darwin": "macos",
        "Linux": "linux",
        "Windows": "windows",
    }.get(platform.system())
    host_arch = normalize_arch(platform.machine())
    if target["platform"] != host_platform or target["architecture"] != host_arch:
        raise BundleBuildError(
            f"target {target['platform']}/{target['architecture']} does not match "
            f"host {host_platform}/{host_arch}"
        )


def normalize_arch(value: str) -> str:
    lowered = value.lower()
    if lowered in {"arm64", "aarch64"}:
        return "aarch64"
    if lowered in {"amd64", "x86_64"}:
        return "x86_64"
    return lowered


def download_locked(source: dict[str, Any], root: Path) -> Path:
    destination = root / str(source["archive"])
    request = urllib.request.Request(
        str(source["url"]),
        headers={"User-Agent": "Hermes-Toolchain-CI/1"},
    )
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as handle:
        final_url = urllib.parse.urlparse(response.geturl())
        if final_url.scheme != "https":
            raise BundleBuildError("download redirected to a non-HTTPS URL")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise BundleBuildError("upstream archive exceeds size limit")
            digest.update(chunk)
            handle.write(chunk)
    actual = digest.hexdigest()
    if actual != source["sha256"]:
        destination.unlink(missing_ok=True)
        raise BundleBuildError(
            f"upstream digest mismatch for {source['archive']}: {actual}"
        )
    return destination


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as handle:
            members = handle.getmembers()
            if len(members) > MAX_FILES:
                raise BundleBuildError("tar archive contains too many entries")
            for member in members:
                validate_archive_path(member.name)
                if member.isdev() or member.isfifo():
                    raise BundleBuildError("tar archive contains a device/fifo")
                if member.issym() or member.islnk():
                    validate_relative_link(member.name, member.linkname)
            handle.extractall(destination, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as handle:
            infos = handle.infolist()
            if len(infos) > MAX_FILES:
                raise BundleBuildError("zip archive contains too many entries")
            for info in infos:
                validate_archive_path(info.filename)
            handle.extractall(destination)
    else:
        raise BundleBuildError(f"unsupported archive type: {archive.name}")


def validate_archive_path(value: str) -> None:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleBuildError(f"unsafe archive path: {value}")


def validate_relative_link(member_name: str, link_name: str) -> None:
    link = PurePosixPath(link_name.replace("\\", "/"))
    if link.is_absolute():
        raise BundleBuildError(f"absolute archive link: {member_name}")
    base = PurePosixPath(member_name).parent
    parts: list[str] = []
    for part in (base / link).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise BundleBuildError(f"archive link escapes root: {member_name}")
            parts.pop()
        else:
            parts.append(part)


def copy_python_tree(extracted: Path, destination: Path) -> None:
    source = find_python_root(extracted)
    # Dereference safe in-archive symlinks into regular files/dirs.  The customer-side
    # installer therefore never needs to create symlinks from untrusted bundle data.
    shutil.copytree(source, destination, symlinks=False)


def find_python_root(extracted: Path) -> Path:
    direct = extracted / "python"
    if direct.is_dir():
        return direct
    matches = [path for path in extracted.rglob("python") if path.is_dir()]
    candidates = [
        path
        for path in matches
        if (path / "bin/python3").exists() or (path / "python.exe").exists()
    ]
    if len(candidates) != 1:
        raise BundleBuildError(f"unable to identify one Python install root: {candidates}")
    return candidates[0]


def copy_uv_binary(extracted: Path, destination: Path, target_platform: str) -> None:
    name = "uv.exe" if target_platform == "windows" else "uv"
    candidates = [
        path
        for path in extracted.rglob(name)
        if path.is_file() and not path.is_symlink()
    ]
    if len(candidates) != 1:
        raise BundleBuildError(f"unable to identify one uv executable: {candidates}")
    destination.mkdir(mode=0o700)
    target = destination / name
    shutil.copy2(candidates[0], target)
    if target_platform != "windows":
        target.chmod(0o700)


def require_executable(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise BundleBuildError(f"{label} is missing from assembled bundle: {path}")
    if os.name != "nt" and not (path.stat().st_mode & stat.S_IXUSR):
        raise BundleBuildError(f"{label} is not executable: {path}")


def enumerate_bundle_files(root: Path, explicit_executables: set[Path]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BundleBuildError(f"assembled bundle contains symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.name in {"TOOLCHAIN-BUNDLE.json", "UPSTREAM-SOURCE.json"}:
            continue
        executable = relative in explicit_executables
        if os.name != "nt" and path.stat().st_mode & stat.S_IXUSR:
            executable = True
        artifacts.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(path),
                "executable": executable,
            }
        )
        if len(artifacts) > MAX_FILES:
            raise BundleBuildError("assembled bundle contains too many files")
    if not artifacts:
        raise BundleBuildError("assembled bundle is empty")
    return artifacts


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BundleBuildError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"toolchain_bundle_error: {error}") from error
