from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIRECTORY = PROJECT_ROOT / "dist"
ARTIFACT_PATTERNS = (
    "hermes_connector-*.whl",
    "hermes_connector-*.tar.gz",
)


def main() -> int:
    DIST_DIRECTORY.mkdir(exist_ok=True)
    for pattern in ARTIFACT_PATTERNS:
        for artifact in DIST_DIRECTORY.glob(pattern):
            if artifact.is_symlink() or not artifact.is_file():
                raise RuntimeError("dist contains a non-regular Connector artifact")
            artifact.unlink()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--outdir",
            str(DIST_DIRECTORY),
            str(PROJECT_ROOT),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/packaging/test_dist_artifacts.py",
            "-q",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
