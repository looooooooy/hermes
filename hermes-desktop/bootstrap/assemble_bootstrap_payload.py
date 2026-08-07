#!/usr/bin/env python3
"""Assemble one unsigned Hermes Desktop bootstrap qualification payload.

The payload is a CI-produced input to the future signed installer. It contains the
platform Desktop binary, the standalone Runtime Manager, and one source/license-
qualified Private Python/uv toolchain. It intentionally does not contain the Managed
Release (Core/Plugin/Connector); that is DESKTOP-020B2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 100_000
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9._-]+)?\Z")
TARGETS = {
    "macos-aarch64": ("macos", "aarch64"),
    "macos-x86_64": ("macos", "x86_64"),
    "linux-aarch64": ("linux", "aarch64"),
    "linux-x86_64": ("linux", "x86_64"),
    "windows-x86_64": ("windows", "x86_64"),
}


class BootstrapPayloadError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--desktop-version", required=True)
    parser.add_argument("--desktop-binary", type=Path, required=True)
    parser.add_argument("--runtime-manager", type=Path, required=True)
    parser.add_argument("--qualified-toolchain", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    platform_name, architecture = TARGETS[args.target]
    if not VERSION_RE.fullmatch(args.desktop_version):
        raise BootstrapPayloadError("desktop version is not a bounded semantic version")

    desktop = require_binary(args.desktop_binary.resolve(), "Hermes Desktop", platform_name)
    manager = require_binary(args.runtime_manager.resolve(), "Runtime Manager", platform_name)
    manager_version = executable_version(manager)
    toolchain_root = args.qualified_toolchain.resolve()
    toolchain = validate_qualified_toolchain(toolchain_root, platform_name, architecture)

    output = args.output.resolve()
    if output.exists() or output.is_symlink():
        raise BootstrapPayloadError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".hermes-bootstrap-", dir=output.parent) as tmp:
        stage = Path(tmp) / "payload"
        stage.mkdir(mode=0o700)
        desktop_name = "hermes-desktop.exe" if platform_name == "windows" else "hermes-desktop"
        manager_name = (
            "hermes-runtime-manager.exe" if platform_name == "windows" else "hermes-runtime-manager"
        )
        desktop_out = stage / "desktop" / desktop_name
        manager_out = stage / "runtime-manager" / manager_name
        desktop_out.parent.mkdir(mode=0o700)
        manager_out.parent.mkdir(mode=0o700)
        shutil.copy2(desktop, desktop_out)
        shutil.copy2(manager, manager_out)
        if os.name != "nt":
            desktop_out.chmod(0o500)
            manager_out.chmod(0o500)

        copied_toolchain = stage / "toolchain"
        copy_regular_tree(toolchain_root, copied_toolchain)
        copied = validate_qualified_toolchain(copied_toolchain, platform_name, architecture)

        components = {
            "desktop": {
                "path": desktop_out.relative_to(stage).as_posix(),
                "version": args.desktop_version,
                "sha256": sha256_file(desktop_out),
                "size_bytes": desktop_out.stat().st_size,
            },
            "runtime_manager": {
                "path": manager_out.relative_to(stage).as_posix(),
                "version": manager_version,
                "sha256": sha256_file(manager_out),
                "size_bytes": manager_out.stat().st_size,
            },
            "toolchain": {
                "root": "toolchain",
                "bundle_id": copied["bundle_id"],
                "manifest_path": "toolchain/TOOLCHAIN-BUNDLE.json",
                "manifest_sha256": sha256_file(copied_toolchain / "TOOLCHAIN-BUNDLE.json"),
                "license_evidence_sha256": sha256_file(copied_toolchain / "LICENSE-EVIDENCE.json"),
                "upstream_source_sha256": sha256_file(copied_toolchain / "UPSTREAM-SOURCE.json"),
                "python_version": copied["python_version"],
                "uv_version": copied["uv_version"],
                "python_path": f"toolchain/{copied['python_path']}",
                "uv_path": f"toolchain/{copied['uv_path']}",
                "declared_files": len(copied["files"]),
            },
        }
        binding = {
            "schema_version": 1,
            "target": args.target,
            "platform": platform_name,
            "architecture": architecture,
            "components": components,
        }
        content_sha256 = hashlib.sha256(canonical_json(binding)).hexdigest()
        manifest = {
            **binding,
            "scope": "hermes_desktop_bootstrap_qualification",
            "publication_state": "qualification-only-unsigned",
            "content_sha256": content_sha256,
            "managed_release": {
                "included": False,
                "required_next_gate": "DESKTOP-020B2",
            },
            "signing": {
                "signed": False,
                "required_next_gate": "DESKTOP-020B3",
            },
        }
        write_json(stage / "BOOTSTRAP-PAYLOAD.json", manifest)

        # Re-check copied platform binaries after assembly before publication.
        require_binary(desktop_out, "assembled Hermes Desktop", platform_name)
        require_binary(manager_out, "assembled Runtime Manager", platform_name)
        if executable_version(manager_out) != manager_version:
            raise BootstrapPayloadError("Runtime Manager version changed during assembly")
        os.replace(stage, output)

    summary = {
        "payload_root": str(output),
        "target": args.target,
        "desktop_version": args.desktop_version,
        "runtime_manager_version": manager_version,
        "toolchain_bundle_id": toolchain["bundle_id"],
        "toolchain_files": len(toolchain["files"]),
        "content_sha256": content_sha256,
        "managed_release_included": False,
        "signed": False,
        "assembled": True,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


def validate_qualified_toolchain(root: Path, platform_name: str, architecture: str) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise BootstrapPayloadError("qualified toolchain must be a regular directory")
    manifest = load_json(root / "TOOLCHAIN-BUNDLE.json")
    if manifest.get("schema_version") != 1:
        raise BootstrapPayloadError("unsupported qualified toolchain schema")
    if manifest.get("platform") != platform_name or manifest.get("architecture") != architecture:
        raise BootstrapPayloadError(
            f"toolchain target mismatch: expected {platform_name}/{architecture}, "
            f"got {manifest.get('platform')}/{manifest.get('architecture')}"
        )
    for field in ("bundle_id", "python_version", "uv_version", "python_path", "uv_path"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise BootstrapPayloadError(f"toolchain manifest field is invalid: {field}")

    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise BootstrapPayloadError("qualified toolchain file list is invalid")
    declared: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BootstrapPayloadError("toolchain file entry is not an object")
        relative = safe_relative(str(item.get("path", "")))
        text = relative.as_posix()
        if text in declared:
            raise BootstrapPayloadError(f"duplicate toolchain path: {text}")
        declared.add(text)
        expected = str(item.get("sha256", ""))
        if not SHA256_RE.fullmatch(expected):
            raise BootstrapPayloadError(f"invalid toolchain SHA-256: {text}")
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise BootstrapPayloadError(f"declared toolchain file missing: {text}")
        if sha256_file(path) != expected:
            raise BootstrapPayloadError(f"toolchain digest mismatch: {text}")

    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != "TOOLCHAIN-BUNDLE.json"
    }
    if actual != declared:
        missing = sorted(declared - actual)[:8]
        extra = sorted(actual - declared)[:8]
        raise BootstrapPayloadError(f"toolchain file-set mismatch; missing={missing} extra={extra}")

    required = {"LICENSE-EVIDENCE.json", "UPSTREAM-SOURCE.json"}
    if not required.issubset(declared):
        raise BootstrapPayloadError("toolchain is not source/license qualified")
    evidence = load_json(root / "LICENSE-EVIDENCE.json")
    if evidence.get("scope") != "engineering_source_and_license_provenance":
        raise BootstrapPayloadError("toolchain license evidence scope is invalid")
    if evidence.get("legal_sufficiency_asserted") is not False:
        raise BootstrapPayloadError("toolchain evidence must not claim legal sufficiency")
    if not evidence.get("upstream_license_files") or not evidence.get("runtime_license_files"):
        raise BootstrapPayloadError("toolchain license evidence is incomplete")

    python_path = safe_relative(manifest["python_path"])
    uv_path = safe_relative(manifest["uv_path"])
    require_binary(root / python_path, "Private Python", platform_name)
    require_binary(root / uv_path, "Private uv", platform_name)
    return manifest


def require_binary(path: Path, label: str, platform_name: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BootstrapPayloadError(f"{label} must be an absolute regular non-symlink file")
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise BootstrapPayloadError(f"{label} is not executable")
    magic = path.read_bytes()[:4]
    if platform_name == "windows" and magic[:2] != b"MZ":
        raise BootstrapPayloadError(f"{label} is not a Windows PE executable")
    if platform_name == "linux" and magic != b"\x7fELF":
        raise BootstrapPayloadError(f"{label} is not a Linux ELF executable")
    if platform_name == "macos" and magic not in {
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }:
        raise BootstrapPayloadError(f"{label} is not a Mach-O/FAT executable")
    return path


def executable_version(path: Path) -> str:
    environment = dict(os.environ)
    environment["PATH"] = ""
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PYTHON"):
        environment.pop(key, None)
    completed = subprocess.run(
        [str(path), "version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    version = completed.stdout.strip()
    if not VERSION_RE.fullmatch(version):
        raise BootstrapPayloadError(f"Runtime Manager version output is invalid: {version!r}")
    return version


def copy_regular_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise BootstrapPayloadError(f"qualified toolchain contains symlink: {path}")
    shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapPayloadError(f"JSON file is missing or symlinked: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BootstrapPayloadError(f"JSON file must contain an object: {path}")
    return value


def safe_relative(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BootstrapPayloadError(f"unsafe relative path: {value}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o400)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootstrapPayloadError, OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise SystemExit(f"bootstrap_payload_error: {error}") from error
