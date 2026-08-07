"""CI entrypoint that preserves safe Git patch diagnostics during rebuilds."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from .apply_and_verify import PatchBundle, PatchBundleError
from .rebuild_locked_artifacts import main as rebuild_main


def _diagnostic_run(command: Sequence[str], **kwargs):
    original = _diagnostic_run.original
    try:
        return original(command, **kwargs)
    except PatchBundleError as error:
        cause = error.__cause__
        if isinstance(cause, subprocess.CalledProcessError):
            command_name = " ".join(str(part) for part in command[:4])
            print(f"--- failed command: {command_name} ---", flush=True)
            for label, value in (
                ("stdout", cause.stdout),
                ("stderr", cause.stderr),
            ):
                if not value:
                    continue
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                print(f"--- {label} ---\n{value}", flush=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    original = PatchBundle._run
    _diagnostic_run.original = original  # type: ignore[attr-defined]
    PatchBundle._run = staticmethod(_diagnostic_run)
    try:
        return rebuild_main(argv)
    finally:
        PatchBundle._run = original


if __name__ == "__main__":
    raise SystemExit(main())
