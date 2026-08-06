"""Fail closed when the locked Stage 3 stabilization patch drifts."""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path

from .apply_and_verify import PatchBundleError
from .generate_stage3_stabilization_patch import generate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle_root = Path(__file__).resolve().parent.parent
    try:
        generated = generate(bundle_root, args.source.resolve())
        patch_path = bundle_root / "patches/0004-stage3-stabilization.patch"
        if generated.patch != patch_path.read_text(encoding="utf-8"):
            raise PatchBundleError("generated stabilization patch drifted")

        lock = json.loads(
            (bundle_root / "upstream.lock.json").read_text(encoding="utf-8")
        )
        locked_sources = {
            item["path"]: item["sha256"]
            for item in lock["artifact_provenance"]["source_files"]
        }
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
