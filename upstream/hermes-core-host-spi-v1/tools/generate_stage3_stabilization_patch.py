"""Generate the Stage 3 stabilization patch from the pinned post-Stage-3 tree."""

from __future__ import annotations

import argparse
import ast
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


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def _is_attribute(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == value


def _replace_node(
    source: str,
    node: ast.AST,
    replacement: list[str],
) -> str:
    if node.lineno is None or node.end_lineno is None:
        raise PatchBundleError("stabilization AST location is unavailable")
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1 : node.end_lineno] = replacement
    return "".join(lines)


def _assignment_target(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return node.targets[0]
    if isinstance(node, ast.AnnAssign):
        return node.target
    return None


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return node.value
    return None


def _plugin_shutdown_assignment(tree: ast.AST) -> ast.AST | None:
    shutdown = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "shutdown_extensions"
        ),
        None,
    )
    if shutdown is None:
        return None
    matches: list[ast.AST] = []
    for node in ast.walk(shutdown):
        target = _assignment_target(node)
        value = _assignment_value(node)
        if (
            not isinstance(target, ast.Name)
            or target.id != "keys"
            or value is None
        ):
            continue
        if any(
            _is_attribute(child, "_extension_registrations")
            for child in ast.walk(value)
        ):
            matches.append(node)
    return matches[0] if len(matches) == 1 else None


def _stabilize_plugins(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    node = _plugin_shutdown_assignment(ast.parse(source))
    if node is None:
        raise PatchBundleError("plugin shutdown stabilization assignment is unavailable")
    lines = source.splitlines(keepends=True)
    indent = _indent(lines[node.lineno - 1])
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
    path.write_text(_replace_node(source, node, replacement), encoding="utf-8")


def _handler_names(node: ast.AST | None) -> set[str]:
    if isinstance(node, ast.Tuple):
        return {item.id for item in node.elts if isinstance(item, ast.Name)}
    if isinstance(node, ast.Name):
        return {node.id}
    return set()


def _contains_call(node: ast.AST, function: str, attribute: str) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
            continue
        if (
            child.func.attr == attribute
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == function
        ):
            return True
    return False


def _contains_half_second_deadline(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.BinOp) or not isinstance(child.op, ast.Add):
            continue
        if isinstance(child.right, ast.Constant) and child.right.value == 0.5:
            return True
    return False


def _process_fallback_handler(tree: ast.AST) -> ast.ExceptHandler | None:
    terminate = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_terminate_host_pid"
        ),
        None,
    )
    if terminate is None:
        return None
    matches = [
        node
        for node in ast.walk(terminate)
        if isinstance(node, ast.ExceptHandler)
        and _handler_names(node.type) == {"OSError", "PermissionError"}
        and _contains_call(node, "os", "kill")
        and _contains_half_second_deadline(node)
    ]
    return matches[0] if len(matches) == 1 else None


def _stabilize_process_registry(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    node = _process_fallback_handler(ast.parse(source))
    if node is None:
        raise PatchBundleError("process registry stabilization handler is unavailable")
    lines = source.splitlines(keepends=True)
    indent = _indent(lines[node.lineno - 1])
    replacement = [
        f"{indent}except (OSError, PermissionError):\n",
        f"{indent}    try:\n",
        f"{indent}        os.kill(pid, signal.SIGTERM)\n",
        f"{indent}    except ProcessLookupError:\n",
        f"{indent}        return True\n",
        f"{indent}    except (OSError, PermissionError):\n",
        f"{indent}        return False\n",
        f"{indent}    deadline = time.monotonic() + 0.5\n",
        f"{indent}    while time.monotonic() < deadline:\n",
        f"{indent}        try:\n",
        f"{indent}            os.kill(pid, 0)\n",
        f"{indent}        except ProcessLookupError:\n",
        f"{indent}            return True\n",
        f"{indent}        except (OSError, PermissionError):\n",
        f"{indent}            return False\n",
        f"{indent}        time.sleep(0.05)\n",
        f"{indent}    return False\n",
    ]
    path.write_text(_replace_node(source, node, replacement), encoding="utf-8")


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
