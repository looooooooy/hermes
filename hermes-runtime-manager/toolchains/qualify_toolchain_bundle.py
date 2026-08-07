#!/usr/bin/env python3
"""Promote a technically built Hermes toolchain into a source/license-qualified bundle.

The input bundle is immutable-by-contract but not publishable. This qualification step
re-verifies every declared file, copies license texts from immutable upstream commits,
records runtime-carried license/notice files, and emits a new bundle manifest whose
per-file SHA list covers the provenance evidence too.

This is engineering provenance evidence. It intentionally does not make a legal
sufficiency determination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

MAX_LICENSE_BYTES = 4 * 1024 * 1024
MAX_FILES = 100_000
RAW_HOST = "raw.githubusercontent.com"
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
LICENSE_MARKERS = ("license", "copying", "notice", "copyright")


class QualificationError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--license-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source = args.bundle.resolve()
    output = args.output.resolve()
    license_lock_path = args.license_lock.resolve()
    if source.is_symlink() or not source.is_dir():
        raise QualificationError("input bundle must be a regular directory")
    if output.exists() or output.is_symlink():
        raise QualificationError("qualified output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)

    manifest = load_json(source / "TOOLCHAIN-BUNDLE.json")
    validate_bundle_manifest(source, manifest)
    license_lock = load_license_lock(license_lock_path)

    with tempfile.TemporaryDirectory(
        prefix=".hermes-qualified-", dir=output.parent
    ) as temp_value:
        temp = Path(temp_value)
        stage = temp / "bundle"
        copy_bundle_tree(source, stage)

        upstream_evidence = install_locked_upstream_licenses(
            license_lock, stage / "licenses" / "upstream"
        )
        runtime_evidence = scan_runtime_license_files(stage / "python", stage)
        if not runtime_evidence:
            raise QualificationError(
                "private Python runtime contains no LICENSE/COPYING/NOTICE/COPYRIGHT evidence"
            )

        evidence = {
            "schema_version": 1,
            "scope": "engineering_source_and_license_provenance",
            "legal_sufficiency_asserted": False,
            "license_source_lock_sha256": sha256_file(license_lock_path),
            "upstream_license_files": upstream_evidence,
            "runtime_license_files": runtime_evidence,
        }
        write_json(stage / "LICENSE-EVIDENCE.json", evidence)

        qualified = dict(manifest)
        qualified["files"] = enumerate_files(stage, manifest)
        write_json(stage / "TOOLCHAIN-BUNDLE.json", qualified, compact=True)
        os.replace(stage, output)

    print(
        json.dumps(
            {
                "qualified_bundle": str(output),
                "manifest_sha256": sha256_file(output / "TOOLCHAIN-BUNDLE.json"),
                "license_evidence_sha256": sha256_file(output / "LICENSE-EVIDENCE.json"),
                "upstream_license_files": len(upstream_evidence),
                "runtime_license_files": len(runtime_evidence),
                "files": len(qualified["files"]),
                "qualified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QualificationError(f"JSON input is missing or symlinked: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualificationError(f"JSON input must be an object: {path}")
    return value


def validate_bundle_manifest(root: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise QualificationError("unsupported toolchain bundle schema")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise QualificationError("toolchain file manifest is invalid")
    declared: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise QualificationError("toolchain file entry is invalid")
        relative = safe_relative(str(item.get("path", "")))
        path_text = relative.as_posix()
        if path_text in declared:
            raise QualificationError(f"duplicate toolchain file: {path_text}")
        declared.add(path_text)
        expected = item.get("sha256")
        if not is_sha256(expected):
            raise QualificationError(f"invalid file SHA: {path_text}")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"declared toolchain file missing: {path_text}")
        if sha256_file(path) != expected:
            raise QualificationError(f"toolchain input digest mismatch: {path_text}")

    # The unqualified builder intentionally leaves these metadata files outside the
    # first manifest. Qualification permits only those known metadata extras.
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    allowed_extra = {"TOOLCHAIN-BUNDLE.json", "UPSTREAM-SOURCE.json"}
    extras = actual - declared - allowed_extra
    if extras:
        raise QualificationError(f"unqualified bundle contains undeclared files: {sorted(extras)}")


def load_license_lock(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if value.get("schema_version") != 1:
        raise QualificationError("unsupported license/source lock schema")
    policy = value.get("policy")
    if not isinstance(policy, dict) or policy.get("qualification_required_before_publish") is not True:
        raise QualificationError("license/source lock must require pre-publish qualification")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise QualificationError("license/source lock contains no sources")
    return value


def copy_bundle_tree(source: Path, destination: Path) -> None:
    def reject_symlink(path: str, names: list[str]) -> set[str]:
        current = Path(path)
        ignored: set[str] = set()
        for name in names:
            candidate = current / name
            if candidate.is_symlink():
                raise QualificationError(f"bundle contains symlink: {candidate}")
        return ignored

    shutil.copytree(source, destination, symlinks=False, ignore=reject_symlink)


def install_locked_upstream_licenses(
    lock: dict[str, Any], destination: Path
) -> list[dict[str, Any]]:
    destination.mkdir(parents=True, exist_ok=True)
    evidence: list[dict[str, Any]] = []
    for source in lock["sources"]:
        component = str(source.get("component", ""))
        repository = str(source.get("repository", ""))
        commit = str(source.get("commit", ""))
        licenses = source.get("licenses")
        if not SAFE_COMPONENT.fullmatch(component):
            raise QualificationError(f"unsafe source component: {component}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise QualificationError(f"unsafe source repository: {repository}")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise QualificationError(f"source commit is not immutable SHA-1: {component}")
        if not isinstance(licenses, list) or not licenses:
            raise QualificationError(f"source contains no locked license files: {component}")

        component_dir = destination / component
        component_dir.mkdir(mode=0o700)
        for item in licenses:
            source_path = safe_relative(str(item.get("path", "")))
            blob_sha = str(item.get("git_blob_sha1", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
                raise QualificationError(f"invalid Git blob SHA for {component}/{source_path}")
            payload = download_raw(repository, commit, source_path)
            if git_blob_sha1(payload) != blob_sha:
                raise QualificationError(
                    f"Git blob mismatch for {component}/{source_path.as_posix()}"
                )
            output_name = source_path.name
            output = component_dir / output_name
            if output.exists():
                raise QualificationError(f"duplicate license output: {output}")
            output.write_bytes(payload)
            output.chmod(0o400)
            evidence.append(
                {
                    "component": component,
                    "repository": repository,
                    "commit": commit,
                    "source_path": source_path.as_posix(),
                    "bundle_path": output.relative_to(destination.parent.parent).as_posix(),
                    "git_blob_sha1": blob_sha,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
    return evidence


def download_raw(repository: str, commit: str, path: PurePosixPath) -> bytes:
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in path.parts)
    url = f"https://{RAW_HOST}/{repository}/{commit}/{quoted}"
    request = urllib.request.Request(url, headers={"User-Agent": "Hermes-License-CI/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        parsed = urllib.parse.urlparse(response.geturl())
        if parsed.scheme != "https" or parsed.hostname != RAW_HOST:
            raise QualificationError("license request redirected outside raw.githubusercontent.com")
        payload = response.read(MAX_LICENSE_BYTES + 1)
    if not payload or len(payload) > MAX_LICENSE_BYTES:
        raise QualificationError(f"license payload size is invalid: {url}")
    return payload


def scan_runtime_license_files(
    python_root: Path, bundle_root: Path
) -> list[dict[str, Any]]:
    if python_root.is_symlink() or not python_root.is_dir():
        raise QualificationError("qualified bundle has no private Python directory")
    evidence: list[dict[str, Any]] = []
    for path in sorted(python_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        lowered = path.name.lower()
        if not any(marker in lowered for marker in LICENSE_MARKERS):
            continue
        size = path.stat().st_size
        if size > MAX_LICENSE_BYTES:
            continue
        evidence.append(
            {
                "bundle_path": path.relative_to(bundle_root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": size,
            }
        )
    return evidence


def enumerate_files(root: Path, original_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    executable_by_path = {
        str(item["path"]): bool(item.get("executable"))
        for item in original_manifest["files"]
    }
    output: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise QualificationError(f"qualified bundle contains symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "TOOLCHAIN-BUNDLE.json":
            continue
        executable = executable_by_path.get(relative, False)
        output.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "executable": executable,
            }
        )
        if len(output) > MAX_FILES:
            raise QualificationError("qualified bundle contains too many files")
    if not output:
        raise QualificationError("qualified bundle is empty")
    return output


def safe_relative(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise QualificationError(f"unsafe relative path: {value}")
    return path


def git_blob_sha1(payload: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {len(payload)}\0".encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


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


def write_json(path: Path, value: Any, *, compact: bool = False) -> None:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":"))
        if compact
        else json.dumps(value, indent=2, sort_keys=True)
    )
    path.write_text(payload + "\n", encoding="utf-8")
    path.chmod(0o400)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (QualificationError, OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"toolchain_qualification_error: {error}") from error
