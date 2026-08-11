"""Behavior tests for the fail-closed isolated patch bundle runner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tools import apply_and_verify as apply_module
from tools.apply_and_verify import PatchBundle, PatchBundleError

POSIX_REPLAY_ONLY = pytest.mark.skipif(
    os.name != "posix" or sys.platform not in {"darwin", "linux"},
    reason="the replay executor is supported only on macOS and Linux",
)
pytestmark = POSIX_REPLAY_ONLY


def test_stage3_lock_runs_tui_close_queue_and_process_behavior_gates() -> None:
    """The reproducible gate must execute every Stage 3 concurrency surface."""
    bundle_root = Path(__file__).resolve().parents[1]
    lock = json.loads((bundle_root / "upstream.lock.json").read_text(encoding="utf-8"))
    command = lock["verification"]["command"]

    assert "tests/tools/test_process_registry.py" in command
    assert "tests/test_tui_gateway_server.py" in command
    assert "tests/test_tui_gateway_queue_on_busy.py" in command


def test_stage3_lock_binds_retained_distribution_artifacts() -> None:
    """Published wheel and sdist must remain independently hash-verifiable."""
    bundle_root = Path(__file__).resolve().parents[1]
    lock = json.loads((bundle_root / "upstream.lock.json").read_text(encoding="utf-8"))
    artifacts = lock["artifacts"]

    assert {item["path"] for item in artifacts} == {
        "dist/hermes_agent-0.19.0-py3-none-any.whl",
        "dist/hermes_agent-0.19.0.tar.gz",
    }
    for item in artifacts:
        content = (bundle_root / item["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == item["sha256"]


def test_stage3_artifacts_match_current_patch_source_provenance() -> None:
    """Retained distributions must contain the current Stage 3 source bytes."""
    bundle_root = Path(__file__).resolve().parents[1]
    bundle = PatchBundle(bundle_root)
    patches = bundle._validated_patches()
    artifacts = bundle._validated_artifacts()

    provenance = bundle._validated_artifact_provenance(patches, artifacts)

    assert provenance.stage3_patch_sha256 == patches[-1].sha256
    assert {entry.relative_path for entry in provenance.source_files} == {
        "agent/credential_pool.py",
        "cli.py",
        "gateway/run.py",
        "hermes_cli/auth.py",
        "hermes_cli/config.py",
        "hermes_cli/extension_runtime.py",
        "hermes_cli/managed_provider.py",
        "hermes_cli/plugin_store_v1.py",
        "hermes_cli/plugins.py",
        "hermes_cli/web_server.py",
        "tools/process_registry.py",
    }


def test_final_wheel_locks_authoritative_hermes_console_and_plugin_store() -> None:
    """The retained Core wheel must launch H5 through hermes and carry Store v1."""
    import configparser
    import zipfile

    bundle_root = Path(__file__).resolve().parents[1]
    wheel = bundle_root / "dist" / "hermes_agent-0.19.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        entrypoint_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        ]
        assert len(entrypoint_paths) == 1
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(archive.read(entrypoint_paths[0]).decode("utf-8"))
        assert parser["console_scripts"]["hermes"] == "hermes_cli.main:main"
        assert "hermes_cli/managed_provider.py" in archive.namelist()
        assert "hermes_cli/plugin_store_v1.py" in archive.namelist()


def test_stage3_verification_includes_signed_plugin_store_gate() -> None:
    bundle_root = Path(__file__).resolve().parents[1]
    lock = json.loads((bundle_root / "upstream.lock.json").read_text(encoding="utf-8"))

    assert "tests/hermes_cli/test_plugin_store_v1.py" in lock["verification"]["command"]
    assert "tests/hermes_cli/test_managed_provider.py" in lock["verification"]["command"]


def test_artifact_digest_mismatch_fails_before_creating_target(tmp_path: Path) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    artifact = bundle_root / "dist" / "package.whl"
    artifact.parent.mkdir()
    artifact.write_bytes(b"retained artifact")
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["artifacts"] = [
        {
            "path": "dist/package.whl",
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    ]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    artifact.write_bytes(b"tampered artifact")
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    with pytest.raises(PatchBundleError, match="artifact digest mismatch"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target,
        )

    assert not target.exists()


def _run(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _run("git", "init", "-q", cwd=source)
    _run("git", "config", "user.email", "stage1@example.invalid", cwd=source)
    _run("git", "config", "user.name", "Stage One", cwd=source)
    (source / "value.txt").write_text("before\n", encoding="utf-8")
    _run("git", "add", "value.txt", cwd=source)
    _run("git", "commit", "-q", "-m", "baseline", cwd=source)
    return source, _run("git", "rev-parse", "HEAD", cwd=source)


def _bundle(
    tmp_path: Path,
    commit: str,
    *,
    test_exit: int = 0,
    verify_code: str | None = None,
) -> Path:
    root = tmp_path / "bundle"
    (root / "patches").mkdir(parents=True)
    patch = root / "patches" / "0001-change-value.patch"
    patch.write_text(
        """diff --git a/value.txt b/value.txt
index 92e1baf..e019be0 100644
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-before
+after
""",
        encoding="utf-8",
    )
    lock = {
        "schema_version": 1,
        "stage": 1,
        "upstream": {
            "distribution": "hermes-agent",
            "version": "0.19.0",
            "commit": commit,
        },
        "patches": [
            {
                "path": "patches/0001-change-value.patch",
                "sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            }
        ],
        "verification": {
            "command": [
                "python",
                "-c",
                verify_code
                or (
                    "from pathlib import Path; "
                    "assert Path('value.txt').read_text() == 'after\\n'; "
                    f"raise SystemExit({test_exit})"
                ),
            ]
        },
    }
    (root / "upstream.lock.json").write_text(
        json.dumps(lock),
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize(
    "stage",
    (None, True, False, 1.0, "3", {}, [], 0, -1, 4),
    ids=(
        "null",
        "true",
        "false",
        "float",
        "string",
        "object",
        "array",
        "zero",
        "negative",
        "unsupported",
    ),
)
def test_rejects_invalid_stage_before_replay_side_effects(
    tmp_path: Path,
    stage: object,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["stage"] = stage
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    with pytest.raises(PatchBundleError, match="bundle stage is invalid"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target,
        )

    assert not target.exists()
    assert list(workspace_root.iterdir()) == []


def test_rejects_patch_count_that_does_not_match_stage_before_replay(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["stage"] = 2
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    with pytest.raises(PatchBundleError, match="patch set does not match stage"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=workspace_root / "patched",
        )

    assert list(workspace_root.iterdir()) == []


def test_rejects_nonsequential_patch_set_before_running_commands(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["patches"][0]["path"] = "patches/0002-change-value.patch"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    def unexpected_command(*_args, **_kwargs):
        pytest.fail("stage/patch validation must run before any command")

    monkeypatch.setattr(PatchBundle, "_run", unexpected_command)
    with pytest.raises(PatchBundleError, match="patch set does not match stage"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=workspace_root / "patched",
        )

    assert list(workspace_root.iterdir()) == []


def test_cli_normalizes_invalid_stage_to_safe_json_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["stage"] = "invalid"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    fake_tool_path = bundle_root / "tools" / "apply_and_verify.py"
    fake_tool_path.parent.mkdir()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"
    monkeypatch.setattr(apply_module, "__file__", str(fake_tool_path))

    exit_code = apply_module.main(
        [
            "--source",
            str(source),
            "--workspace-root",
            str(workspace_root),
            "--target",
            str(target),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "bundle stage is invalid",
    }
    assert not target.exists()
    assert list(workspace_root.iterdir()) == []


def test_applies_to_new_isolated_target_and_preserves_source(tmp_path: Path) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"
    before_status = _run("git", "status", "--porcelain=v1", cwd=source)

    result = PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=target,
    )

    assert result.upstream_commit == commit
    assert result.target == target.resolve()
    assert (target / "value.txt").read_text(encoding="utf-8") == "after\n"
    evidence = json.loads((target / "APPLIED_BUNDLE.json").read_text())
    assert evidence["upstream_commit"] == commit
    assert evidence["stage"] == 1
    assert _run("git", "status", "--porcelain=v1", cwd=source) == before_status
    assert _run("git", "rev-parse", "HEAD", cwd=source) == commit


def test_success_target_removes_all_python_and_test_cache_artifacts(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(
        tmp_path,
        commit,
        verify_code=(
            "from pathlib import Path; "
            "Path('__pycache__').mkdir(); "
            "Path('__pycache__/module.pyc').write_bytes(b'cache'); "
            "Path('nested/__pycache__').mkdir(parents=True); "
            "Path('nested/__pycache__/other.pyc').write_bytes(b'cache'); "
            "Path('.pytest_cache/v/cache').mkdir(parents=True); "
            "Path('.pytest_cache/v/cache/nodeids').write_text('[]'); "
            "Path('.ruff_cache').mkdir(); "
            "Path('.ruff_cache/data').write_text('cache'); "
            "Path('test_durations.json').write_text('{}')"
        ),
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=target,
    )

    forbidden = {
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.name
        in {
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "test_durations.json",
        }
        or path.suffix in {".pyc", ".pyo"}
    }
    assert forbidden == set()


def test_success_target_removes_hyphenated_pytest_cache_artifact(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(
        tmp_path,
        commit,
        verify_code=(
            "from pathlib import Path; "
            "Path('.pytest-cache/v/cache').mkdir(parents=True); "
            "Path('.pytest-cache/v/cache/nodeids').write_text('[]')"
        ),
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=target,
    )

    assert not (target / ".pytest-cache").exists()


def test_applies_inside_an_ignored_parent_git_worktree(tmp_path: Path) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    outer = tmp_path / "outer"
    outer.mkdir()
    _run("git", "init", "-q", cwd=outer)
    _run("git", "config", "user.email", "stage1@example.invalid", cwd=outer)
    _run("git", "config", "user.name", "Stage One", cwd=outer)
    (outer / ".gitignore").write_text("workspaces/\n", encoding="utf-8")
    _run("git", "add", ".gitignore", cwd=outer)
    _run("git", "commit", "-q", "-m", "outer baseline", cwd=outer)
    workspace_root = outer / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=target,
    )

    assert (target / "value.txt").read_text(encoding="utf-8") == "after\n"
    assert not (target / ".git").exists()
    assert _run("git", "status", "--porcelain=v1", cwd=outer) == ""


def test_preserves_the_operator_supplied_venv_python_path(tmp_path: Path) -> None:
    source, commit = _source_repo(tmp_path)
    python_link = tmp_path / "python-link"
    python_link.symlink_to(Path(sys.executable))
    expected = str(python_link.absolute())
    bundle_root = _bundle(
        tmp_path,
        commit,
        verify_code=(
            "import os; "
            f"assert os.environ['HERMES_PYTHON'] == {expected!r}"
        ),
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=workspace_root / "patched",
        test_python=python_link,
    )


def test_verification_runner_cannot_probe_the_operator_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    operator_home = tmp_path / "operator-home"
    live_venv = operator_home / ".hermes" / "hermes-agent" / "venv"
    (live_venv / "bin").mkdir(parents=True)
    (live_venv / "bin" / "activate").write_text("sentinel\n", encoding="utf-8")
    probe_marker = tmp_path / "live-venv-was-probed"
    fake_python = live_venv / "bin" / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        f"touch {probe_marker}\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "shared-uv-cache"))
    bundle_root = _bundle(
        tmp_path,
        commit,
        verify_code=(
            "import os, subprocess; "
            "from pathlib import Path; "
            f"assert Path.home() != Path({str(operator_home)!r}); "
            "candidate = Path.home() / '.hermes/hermes-agent/venv'; "
            "assert not (candidate / 'bin/activate').exists(); "
            "assert os.environ['HERMES_HOME'] == str(Path.home() / '.hermes'); "
            f"assert os.environ['UV_CACHE_DIR'] == {str(tmp_path / 'shared-uv-cache')!r}; "
            "subprocess.run([str(candidate / 'bin/python'), '-c', 'import pytest']) "
            "if (candidate / 'bin/python').exists() else None"
        ),
    )
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=workspace_root / "patched",
    )

    assert not probe_marker.exists()


def test_declared_uv_locked_environment_rejects_python_missing_required_modules(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["verification"]["environment"] = {
        "kind": "uv-all-dev-locked",
        "command": [
            "uv",
            "sync",
            "--extra",
            "all",
            "--extra",
            "dev",
            "--locked",
            "--check",
            "--no-install-project",
        ],
        "required_imports": ["pytest", "rich"],
    }
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    python_wrapper = tmp_path / "python-without-rich"
    python_wrapper.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *'import rich'*) exit 1 ;;\n"
        "esac\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    with pytest.raises(
        PatchBundleError,
        match="locked verification environment is unavailable",
    ):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target,
            test_python=python_wrapper,
        )

    assert not target.exists()


def test_rejects_wrong_upstream_commit_before_creating_target(tmp_path: Path) -> None:
    source, _commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, "0" * 40)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"

    with pytest.raises(PatchBundleError, match="upstream commit mismatch"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target,
        )

    assert not target.exists()
    assert list(workspace_root.iterdir()) == []


def test_rejects_dirty_source_and_overlapping_or_existing_target(tmp_path: Path) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    bundle = PatchBundle(bundle_root)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    with pytest.raises(PatchBundleError, match="target must be inside workspace root"):
        bundle.apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=source / "patched",
        )

    existing = workspace_root / "existing"
    existing.mkdir()
    with pytest.raises(PatchBundleError, match="target already exists"):
        bundle.apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=existing,
        )

    (source / "value.txt").write_text("dirty\n", encoding="utf-8")
    target = workspace_root / "dirty-target"
    with pytest.raises(PatchBundleError, match="upstream source must be clean"):
        bundle.apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target,
        )
    assert not target.exists()


def test_patch_digest_and_verification_failure_leave_no_partial_target(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    digest_bundle_root = _bundle(tmp_path, commit)
    patch = digest_bundle_root / "patches" / "0001-change-value.patch"
    patch.write_text(patch.read_text() + "\n", encoding="utf-8")
    with pytest.raises(PatchBundleError, match="patch digest mismatch"):
        PatchBundle(digest_bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=workspace_root / "digest-target",
        )
    assert list(workspace_root.iterdir()) == []

    failed_root = tmp_path / "failed-bundle-parent"
    failed_root.mkdir()
    failed_bundle_root = _bundle(failed_root, commit, test_exit=7)
    with pytest.raises(PatchBundleError, match="verification command failed"):
        PatchBundle(failed_bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=workspace_root / "failed-target",
        )
    assert list(workspace_root.iterdir()) == []


def test_rejects_nested_source_instead_of_treating_it_as_git_top_level(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    nested_source = source / "nested"
    nested_source.mkdir()
    bundle_root = _bundle(tmp_path, commit)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    with pytest.raises(PatchBundleError, match="Git top-level"):
        PatchBundle(bundle_root).apply_and_verify(
            source=nested_source,
            workspace_root=workspace_root,
            target=workspace_root / "patched",
        )

    assert list(workspace_root.iterdir()) == []


def test_rejects_symlink_target_and_symlinked_parent_components(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"
    target.symlink_to(workspace_root / "redirect", target_is_directory=True)

    with pytest.raises(PatchBundleError, match="symbolic link"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target,
        )

    assert not (workspace_root / "redirect").exists()
    target.unlink()

    inner = workspace_root / "inner"
    inner.mkdir()
    hop = workspace_root / "hop"
    hop.symlink_to(inner, target_is_directory=True)
    target_through_parent = workspace_root / "hop" / ".." / "patched"
    with pytest.raises(PatchBundleError, match="symbolic link"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=target_through_parent,
        )
    hop.unlink()
    inner.rmdir()

    real_workspace = tmp_path / "real-workspaces"
    real_workspace.mkdir()
    linked_workspace = tmp_path / "linked-workspaces"
    linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    with pytest.raises(PatchBundleError, match="symbolic link"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=linked_workspace,
            target=linked_workspace / "patched",
        )

    assert list(real_workspace.iterdir()) == []


def test_patch_bytes_are_pinned_before_archive_and_evidence_uses_lock_digest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    patch_path = bundle_root / "patches" / "0001-change-value.patch"
    locked_digest = json.loads(
        (bundle_root / "upstream.lock.json").read_text(encoding="utf-8")
    )["patches"][0]["sha256"]
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "patched"
    bundle = PatchBundle(bundle_root)
    original_run = bundle._run
    mutated = False

    def mutate_after_validation(command, **kwargs):
        nonlocal mutated
        if not mutated and "archive" in command:
            mutated = True
            patch_path.write_text(
                patch_path.read_text(encoding="utf-8").replace("+after", "+tampered"),
                encoding="utf-8",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr(bundle, "_run", mutate_after_validation)
    bundle.apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=target,
    )

    evidence = json.loads((target / "APPLIED_BUNDLE.json").read_text())
    assert mutated is True
    assert (target / "value.txt").read_text(encoding="utf-8") == "after\n"
    assert evidence["patches"] == [
        {
            "path": "patches/0001-change-value.patch",
            "sha256": locked_digest,
        }
    ]


def test_archive_allocation_failure_cleans_partial_and_is_operator_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(tmp_path, commit)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    def fail_archive_allocation(*args, **kwargs):
        raise OSError("private allocation detail")

    monkeypatch.setattr(tempfile, "mkstemp", fail_archive_allocation)
    with pytest.raises(PatchBundleError, match="isolated patch preparation failed"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=workspace_root / "patched",
        )

    assert list(workspace_root.iterdir()) == []


def test_multiple_patches_are_locked_and_applied_in_declared_order(
    tmp_path: Path,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle_root = _bundle(
        tmp_path,
        commit,
        verify_code=(
            "from pathlib import Path; "
            "assert Path('value.txt').read_text() == 'final\\n'"
        ),
    )
    second_patch = bundle_root / "patches" / "0002-after-to-final.patch"
    second_patch.write_text(
        """diff --git a/value.txt b/value.txt
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-after
+final
""",
        encoding="utf-8",
    )
    lock_path = bundle_root / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["stage"] = 2
    lock["patches"].append(
        {
            "path": "patches/0002-after-to-final.patch",
            "sha256": hashlib.sha256(second_patch.read_bytes()).hexdigest(),
        }
    )
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    target = workspace_root / "ordered"

    PatchBundle(bundle_root).apply_and_verify(
        source=source,
        workspace_root=workspace_root,
        target=target,
    )

    assert (target / "value.txt").read_text(encoding="utf-8") == "final\n"
    evidence = json.loads((target / "APPLIED_BUNDLE.json").read_text())
    assert [entry["path"] for entry in evidence["patches"]] == [
        "patches/0001-change-value.patch",
        "patches/0002-after-to-final.patch",
    ]

    lock["patches"].reverse()
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(PatchBundleError, match="patch set does not match stage"):
        PatchBundle(bundle_root).apply_and_verify(
            source=source,
            workspace_root=workspace_root,
            target=workspace_root / "reversed",
        )
    assert not (workspace_root / "reversed").exists()
