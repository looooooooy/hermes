"""Generate the Stage 3 stabilization patch from the pinned post-Stage-3 tree."""

from __future__ import annotations

import argparse
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .apply_and_verify import PatchBundle, PatchBundleError


def _run(command: Sequence[str], *, cwd: Path, input_bytes: bytes | None = None) -> str:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        input=input_bytes,
        text=input_bytes is None,
    )
    if isinstance(completed.stdout, bytes):
        return completed.stdout.decode("utf-8", errors="strict")
    return completed.stdout


def _replace_once(
    source: str,
    pattern: re.Pattern[str],
    replacement,
    *,
    label: str,
) -> str:
    updated, count = pattern.subn(replacement, source)
    if count != 1:
        raise PatchBundleError(f"{label} stabilization replacement count was {count}")
    return updated


def _stabilize_plugins(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    needle = "keys = list(reversed(self._extension_registrations))"
    matches = [index for index, line in enumerate(lines) if line.strip() == needle]
    if len(matches) != 1:
        raise PatchBundleError(
            f"plugin shutdown stabilization replacement count was {len(matches)}"
        )
    index = matches[0]
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip(" "))]
    replacement = [
        f"{indent}registered_keys = set(self._extension_registrations)\n",
        f"{indent}keys = [\n",
        f"{indent}    key\n",
        f"{indent}    for key in reversed(self._plugins)\n",
        f"{indent}    if key in registered_keys\n",
        f"{indent}]\n",
        f"{indent}keys.extend(\n",
        f"{indent}    key\n",
        f"{indent}    for key in reversed(self._extension_registrations)\n",
        f"{indent}    if key not in self._plugins\n",
        f"{indent})\n",
    ]
    lines[index : index + 1] = replacement
    path.write_text("".join(lines), encoding="utf-8")


def _stabilize_process_registry(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"(?m)^(?P<indent>[ ]*)except \(OSError, PermissionError\):\n"
        r"(?P=indent)    try:\n"
        r"(?P=indent)        os\.kill\(pid, signal\.SIGTERM\)\n"
        r"(?P=indent)    except \(OSError, ProcessLookupError, PermissionError\):\n"
        r"(?P=indent)        return False\n"
        r"(?P=indent)    deadline = time\.monotonic\(\) \+ 0\.5\n"
        r"(?P=indent)    while time\.monotonic\(\) < deadline:\n"
        r"(?P=indent)        if cls\._host_pid_identity\(pid, expected_start\) == \"gone_or_reused\":\n"
        r"(?P=indent)            return True\n"
        r"(?P=indent)        time\.sleep\(0\.05\)\n"
        r"(?P=indent)    return cls\._host_pid_identity\(pid, expected_start\) == \"gone_or_reused\"\n"
    )

    def replacement(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return "\n".join(
            (
                f"{indent}except (OSError, PermissionError):",
                f"{indent}    try:",
                f"{indent}        os.kill(pid, signal.SIGTERM)",
                f"{indent}    except ProcessLookupError:",
                f"{indent}        return True",
                f"{indent}    except (OSError, PermissionError):",
                f"{indent}        return False",
                f"{indent}    deadline = time.monotonic() + 0.5",
                f"{indent}    while time.monotonic() < deadline:",
                f"{indent}        try:",
                f"{indent}            os.kill(pid, 0)",
                f"{indent}        except ProcessLookupError:",
                f"{indent}            return True",
                f"{indent}        except (OSError, PermissionError):",
                f"{indent}            return False",
                f"{indent}        time.sleep(0.05)",
                f"{indent}    return False",
                "",
            )
        )

    path.write_text(
        _replace_once(
            source,
            pattern,
            replacement,
            label="process registry",
        ),
        encoding="utf-8",
    )


def generate(bundle_root: Path, source: Path) -> str:
    bundle = PatchBundle(bundle_root)
    upstream = bundle.lock.get("upstream")
    commit = upstream.get("commit") if isinstance(upstream, dict) else None
    if not isinstance(commit, str):
        raise PatchBundleError("bundle upstream commit is invalid")

    with tempfile.TemporaryDirectory(prefix="stage3-stabilization-") as temporary:
        root = Path(temporary)
        archive = root / "upstream.tar"
        tree = root / "source"
        tree.mkdir()
        _run(
            (
                "git",
                "-C",
                str(source),
                "archive",
                "--format=tar",
                f"--output={archive}",
                commit,
            ),
            cwd=root,
        )
        with tarfile.open(archive, mode="r") as source_archive:
            source_archive.extractall(path=tree, filter="data")
        _run(("git", "init", "-q"), cwd=tree)
        _run(("git", "config", "user.email", "stage3@example.invalid"), cwd=tree)
        _run(("git", "config", "user.name", "Stage 3 Generator"), cwd=tree)

        patches = bundle._validated_patches()
        if len(patches) < 4:
            raise PatchBundleError("Stage 3 stabilization patch is unavailable")
        for patch in patches[:3]:
            _run(("git", "apply", "--check", "-"), cwd=tree, input_bytes=patch.content)
            _run(("git", "apply", "-"), cwd=tree, input_bytes=patch.content)

        _run(("git", "add", "-A"), cwd=tree)
        _run(("git", "commit", "-q", "-m", "stage3 baseline"), cwd=tree)
        _stabilize_plugins(tree / "hermes_cli/plugins.py")
        _stabilize_process_registry(tree / "tools/process_registry.py")
        patch = _run(
            (
                "git",
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--",
                "hermes_cli/plugins.py",
                "tools/process_registry.py",
            ),
            cwd=tree,
        )
        if not patch.strip():
            raise PatchBundleError("generated stabilization patch is empty")
        return patch


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        patch = generate(Path(__file__).resolve().parent.parent, args.source.resolve())
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, PatchBundleError) as error:
        print(f"stage3 stabilization generation failed: {error}")
        return 2
    print("--- BEGIN GENERATED STABILIZATION PATCH ---")
    print(patch, end="" if patch.endswith("\n") else "\n")
    print("--- END GENERATED STABILIZATION PATCH ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
