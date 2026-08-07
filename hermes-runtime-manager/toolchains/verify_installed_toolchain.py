#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

    manifest_path = installs[0] / "TOOLCHAIN-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("offline_only") is not True:
        raise SystemExit("installed toolchain manifest does not enforce offline-only schema v1")

    python_path = Path(manifest["python"]["path"])
    uv_path = Path(manifest["uv"]["path"])
    for label, path in (("python", python_path), ("uv", uv_path)):
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise SystemExit(f"installed {label} path is invalid: {path}")
        if installs[0] not in path.parents:
            raise SystemExit(f"installed {label} escaped toolchain root: {path}")

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
                "installed_root": str(installs[0]),
                "python": python_output[0],
                "uv": uv_output,
                "architecture": manifest["architecture"],
                "platform": manifest["platform"],
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
