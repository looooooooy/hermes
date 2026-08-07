"""Fail closed when the locked Stage 3 stabilization patch drifts."""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .apply_and_verify import PatchBundleError
from .generate_stage3_stabilization_patch import GeneratedStabilization, generate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    return parser


def _print_generated(generated: GeneratedStabilization) -> None:
    print("--- BEGIN GENERATED STABILIZATION PATCH ---")
    print(generated.patch, end="" if generated.patch.endswith("\n") else "\n")
    print("--- END GENERATED STABILIZATION PATCH ---")
    print(
        json.dumps(
            {"source_provenance": generated.source_provenance},
            sort_keys=True,
        )
    )


def _locked_provenance(lock: Mapping[str, object]) -> dict[str, str]:
    artifact = lock.get("artifact_provenance")
    if not isinstance(artifact, Mapping):
        raise PatchBundleError("artifact provenance is invalid")
    raw_sources = artifact.get("source_files")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise PatchBundleError("artifact provenance is invalid")

    build = lock.get("artifact_build_provenance")
    if (
        not isinstance(build, Mapping)
        or set(build) != {"schema_version", "source_files"}
        or build.get("schema_version") != 1
    ):
        raise PatchBundleError("artifact build provenance is invalid")
    raw_build_sources = build.get("source_files")
    if not isinstance(raw_build_sources, list) or not raw_build_sources:
        raise PatchBundleError("artifact build provenance is invalid")

    locked: dict[str, str] = {}
    for label, entries in (
        ("artifact provenance", raw_sources),
        ("artifact build provenance", raw_build_sources),
    ):
        for item in entries:
            if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
                raise PatchBundleError(f"{label} is invalid")
            path = item.get("path")
            digest = item.get("sha256")
            if (
                not isinstance(path, str)
                or not path
                or path in locked
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise PatchBundleError(f"{label} is invalid")
            locked[path] = digest
    return locked


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle_root = Path(__file__).resolve().parent.parent
    generated: GeneratedStabilization | None = None
    try:
        generated = generate(bundle_root, args.source.resolve())
        patch_path = bundle_root / "patches/0004-stage3-stabilization.patch"
        if generated.patch != patch_path.read_text(encoding="utf-8"):
            raise PatchBundleError("generated stabilization patch drifted")

        lock = json.loads(
            (bundle_root / "upstream.lock.json").read_text(encoding="utf-8")
        )
        locked_sources = _locked_provenance(lock)
        for path, digest in generated.source_provenance.items():
            if locked_sources.get(path) != digest:
                raise PatchBundleError(
                    f"generated stabilization provenance drifted: {path}"
                )
    except (
        KeyError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        tarfile.TarError,
        PatchBundleError,
    ) as error:
        if generated is not None:
            _print_generated(generated)
        print(json.dumps({"error": str(error), "ok": False}, sort_keys=True))
        return 2

    print(
        json.dumps(
            {
                "ok": True,
                "patch": "patches/0004-stage3-stabilization.patch",
                "source_provenance": generated.source_provenance,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
