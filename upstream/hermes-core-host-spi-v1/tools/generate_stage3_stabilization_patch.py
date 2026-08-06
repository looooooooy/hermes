"""Generate the Stage 3 stabilization patch from the pinned post-Stage-3 tree."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .apply_and_verify import PatchBundle, PatchBundleError

_STATE_MODULES = (
    "hermes_state_common",
    "hermes_state_portability",
    "hermes_state_schema",
    "hermes_state_search",
)


@dataclass(frozen=True)
class GeneratedStabilization:
    patch: str
    source_provenance: Mapping[str, str]


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


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


def _entrypoint_scan_loop(tree: ast.AST) -> ast.For | None:
    scan = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_scan_entry_points"
        ),
        None,
    )
    if scan is None:
        return None
    matches = [
        node
        for node in ast.walk(scan)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "ep"
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "group_eps"
    ]
    return matches[0] if len(matches) == 1 else None


def _stabilize_plugins(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    loop = _entrypoint_scan_loop(ast.parse(source))
    if loop is None or loop.lineno is None:
        raise PatchBundleError("entry-point scan stabilization loop is unavailable")
    lines = source.splitlines(keepends=True)
    header_index = loop.lineno - 1
    if lines[header_index].strip() != "for ep in group_eps:":
        raise PatchBundleError("entry-point scan stabilization header is unavailable")
    indent = _indent(lines[header_index])
    lines[header_index : header_index + 1] = [
        f"{indent}for ep in sorted(\n",
        f"{indent}    group_eps,\n",
        f"{indent}    key=lambda entry_point: (\n",
        f"{indent}        entry_point.name,\n",
        f"{indent}        entry_point.value,\n",
        f"{indent}    ),\n",
        f"{indent}):\n",
    ]
    path.write_text("".join(lines), encoding="utf-8")


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


def _stabilize_pyproject(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith("py-modules = [")
    ]
    if len(starts) != 1:
        raise PatchBundleError("setuptools py-modules declaration is unavailable")
    start = starts[0]
    end = start
    while "]" not in lines[end]:
        end += 1
        if end >= len(lines):
            raise PatchBundleError("setuptools py-modules declaration is malformed")

    declaration = "".join(lines[start : end + 1])
    payload = declaration.split("=", 1)[1].strip()
    try:
        modules = list(ast.literal_eval(payload))
    except (SyntaxError, ValueError) as error:
        raise PatchBundleError(
            "setuptools py-modules declaration is malformed"
        ) from error
    if not all(isinstance(module, str) and module for module in modules):
        raise PatchBundleError("setuptools py-modules declaration is malformed")
    if any(module in modules for module in _STATE_MODULES):
        raise PatchBundleError("runtime state modules are already packaged")
    try:
        insertion = modules.index("hermes_state") + 1
    except ValueError as error:
        raise PatchBundleError("hermes_state packaging anchor is unavailable") from error
    modules[insertion:insertion] = _STATE_MODULES
    replacement = ["py-modules = [\n"]
    replacement.extend(f'  "{module}",\n' for module in modules)
    replacement.append("]\n")
    lines[start : end + 1] = replacement
    path.write_text("".join(lines), encoding="utf-8")


def _normalize_patch(patch: str) -> str:
    """Remove whitespace-only additions while preserving diff context lines."""

    normalized: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++") and not line[1:].strip():
            normalized.append("+")
        else:
            normalized.append(line)
    return "\n".join(normalized) + ("\n" if patch.endswith("\n") else "")


def generate(bundle_root: Path, source: Path) -> GeneratedStabilization:
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
        plugins = tree / "hermes_cli/plugins.py"
        process_registry = tree / "tools/process_registry.py"
        pyproject = tree / "pyproject.toml"
        _stabilize_plugins(plugins)
        _stabilize_process_registry(process_registry)
        _stabilize_pyproject(pyproject)
        patch = _normalize_patch(
            _run(
                (
                    "git",
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-ext-diff",
                    "--",
                    "hermes_cli/plugins.py",
                    "pyproject.toml",
                    "tools/process_registry.py",
                ),
                cwd=tree,
            )
        )
        if not patch.strip():
            raise PatchBundleError("generated stabilization patch is empty")
        return GeneratedStabilization(
            patch=patch,
            source_provenance={
                "hermes_cli/plugins.py": _sha256(plugins),
                "pyproject.toml": _sha256(pyproject),
                "tools/process_registry.py": _sha256(process_registry),
            },
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        generated = generate(
            Path(__file__).resolve().parent.parent,
            args.source.resolve(),
        )
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, PatchBundleError) as error:
        print(f"stage3 stabilization generation failed: {error}")
        return 2
    print("--- BEGIN GENERATED STABILIZATION PATCH ---")
    print(generated.patch, end="" if generated.patch.endswith("\n") else "\n")
    print("--- END GENERATED STABILIZATION PATCH ---")
    print(
        json.dumps(
            {"source_provenance": generated.source_provenance},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
