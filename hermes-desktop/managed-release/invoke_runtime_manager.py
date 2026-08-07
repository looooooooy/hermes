#!/usr/bin/env python3
"""Invoke the Hermes Runtime Manager without shell parsing.

Qualification workflows use this helper whenever a platform-specific Runtime Manager
binary must be called with paths.  Keeping argv as an explicit subprocess list avoids
PowerShell/Bash quoting differences and mirrors the production no-shell boundary.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


class InvocationError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-manager", type=Path, required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    executable = args.runtime_manager.resolve()
    if executable.is_symlink() or not executable.is_file():
        raise InvocationError("Runtime Manager must be a regular non-symlink file")
    arguments = list(args.arguments)
    if arguments and arguments[0] == "--":
        arguments.pop(0)
    if not arguments:
        raise InvocationError("Runtime Manager command is required")

    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(executable), *arguments],
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        raise InvocationError(
            f"Runtime Manager command failed with exit {completed.returncode}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InvocationError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"runtime_manager_invocation_error: {error}") from error
