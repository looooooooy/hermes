#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--toolchains-root", type=Path, required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--uv-version", required=True)
    args = parser.parse_args()

    root = args.toolchains_root.resolve()
    installs = [path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")]
    if len(installs) != 1:
        raise SystemExit(f"expected exactly one installed toolchain, found: {installs}")

    installed_root = installs[0].resolve()
    manifest_path = installed_root / "TOOLCHAIN-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("offline_only") is not True:
        raise SystemExit("installed toolchain manifest does not enforce offline-only schema v1")

    python_path = Path(manifest["python"]["path"])
    uv_path = Path(manifest["uv"]["path"])
    for label, path in (("python", python_path), ("uv", uv_path)):
        require_installed_file(installed_root, path, label)

    evidence_path = installed_root / "LICENSE-EVIDENCE.json"
    source_path = installed_root / "UPSTREAM-SOURCE.json"
    require_installed_file(installed_root, evidence_path, "license evidence")
    require_installed_file(installed_root, source_path, "upstream source evidence")

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != 1:
        raise SystemExit("installed license evidence schema is unsupported")
    if evidence.get("scope") != "engineering_source_and_license_provenance":
        raise SystemExit("installed license evidence scope is invalid")
    if evidence.get("legal_sufficiency_asserted") is not False:
        raise SystemExit("license evidence must not claim legal sufficiency")

    upstream_license_files = evidence.get("upstream_license_files")
    runtime_license_files = evidence.get("runtime_license_files")
    if not isinstance(upstream_license_files, list) or len(upstream_license_files) < 3:
        raise SystemExit("installed toolchain is missing locked upstream license evidence")
    if not isinstance(runtime_license_files, list) or not runtime_license_files:
        raise SystemExit("installed toolchain is missing private Python runtime license evidence")

    for entry in upstream_license_files + runtime_license_files:
        if not isinstance(entry, dict):
            raise SystemExit("installed license evidence contains a non-object entry")
        relative = entry.get("bundle_path")
        expected_sha256 = entry.get("sha256")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise SystemExit(f"invalid license evidence path: {relative!r}")
        if not is_sha256(expected_sha256):
            raise SystemExit(f"invalid license evidence SHA-256: {relative}")
        evidence_file = (installed_root / relative).resolve()
        require_installed_file(installed_root, evidence_file, f"license evidence file {relative}")
        if sha256_file(evidence_file) != expected_sha256:
            raise SystemExit(f"installed license evidence digest mismatch: {relative}")

    upstream_source = json.loads(source_path.read_text(encoding="utf-8"))
    if upstream_source.get("schema_version") != 1:
        raise SystemExit("installed upstream source evidence schema is unsupported")
    if not isinstance(upstream_source.get("python"), dict) or not isinstance(
        upstream_source.get("uv"), dict
    ):
        raise SystemExit("installed upstream source evidence is incomplete")

    python_output = subprocess.check_output(
        [str(python_path), "-I", "-c", "import platform,sys; print(sys.version.split()[0]); print(platform.machine())"],
        text=True,
    ).splitlines()
    if not python_output or python_output[0] != args.python_version:
        raise SystemExit(f"unexpected private Python version: {python_output}")

    uv_output = subprocess.check_output([str(uv_path), "--version"], text=True).strip()
    expected_uv_prefix = f"uv {args.uv_version}"
    if not (
        uv_output == expected_uv_prefix
        or uv_output.startswith(expected_uv_prefix + " ")
    ):
        raise SystemExit(f"unexpected private uv version: {uv_output}")

    print(
        json.dumps(
            {
                "installed_root": str(installed_root),
                "python": python_output[0],
                "uv": uv_output,
                "architecture": manifest["architecture"],
                "platform": manifest["platform"],
                "upstream_license_files": len(upstream_license_files),
                "runtime_license_files": len(runtime_license_files),
                "provenance_verified": True,
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def require_installed_file(installed_root: Path, path: Path, label: str) -> None:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise SystemExit(f"installed {label} path is invalid: {path}")
    if resolved != installed_root and installed_root not in resolved.parents:
        raise SystemExit(f"installed {label} escaped toolchain root: {path}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


if __name__ == "__main__":
    raise SystemExit(main())
