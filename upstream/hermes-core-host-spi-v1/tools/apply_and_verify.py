"""Apply the pinned Host SPI patch to a new isolated archive and verify it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SUPPORTED_PATCH_STAGES = frozenset({1, 2, 3})


class PatchBundleError(RuntimeError):
    """Fail-closed patch preparation error with an operator-safe message."""


@dataclass(frozen=True)
class ApplyResult:
    upstream_commit: str
    target: Path
    stage: int
    verification_command: tuple[str, ...]


@dataclass(frozen=True)
class _ValidatedPatch:
    relative_path: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class _ValidatedArtifact:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class _ArtifactSourceFile:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class _ArtifactProvenance:
    stage3_patch_sha256: str
    source_files: tuple[_ArtifactSourceFile, ...]


class PatchBundle:
    def __init__(self, bundle_root: Path | str) -> None:
        self.root = Path(bundle_root).resolve(strict=True)
        self.lock_path = self.root / "upstream.lock.json"
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise PatchBundleError("bundle lock is unavailable or invalid") from error
        if not isinstance(lock, dict) or lock.get("schema_version") != 1:
            raise PatchBundleError("bundle lock schema is unsupported")
        self.lock = lock
        self.stage = self._validated_stage(lock)

    def apply_and_verify(
        self,
        *,
        source: Path | str,
        workspace_root: Path | str,
        target: Path | str,
        test_python: Path | str | None = None,
    ) -> ApplyResult:
        source_input = self._absolute_without_resolving(source)
        workspace_input = self._absolute_without_resolving(workspace_root)
        target_input = self._absolute_without_resolving(target)
        self._reject_symlink_components(workspace_input, "workspace root")
        self._reject_symlink_components(target_input, "target")
        try:
            source_path = source_input.resolve(strict=True)
            workspace_path = workspace_input.resolve(strict=True)
            target_path = target_input.resolve(strict=False)
        except OSError as error:
            raise PatchBundleError("source or workspace path is unavailable") from error
        if not source_path.is_dir():
            raise PatchBundleError("upstream source must be a directory")
        if not workspace_path.is_dir():
            raise PatchBundleError("workspace root must be a directory")
        if target_path == workspace_path or workspace_path not in target_path.parents:
            raise PatchBundleError("target must be inside workspace root")
        if (
            source_path == workspace_path
            or source_path in workspace_path.parents
            or source_path == target_path
            or source_path in target_path.parents
            or target_path in source_path.parents
        ):
            raise PatchBundleError("upstream source and target must be disjoint")
        if os.path.lexists(target_input):
            raise PatchBundleError("target already exists")
        if target_path.parent != workspace_path:
            raise PatchBundleError("target must be a direct child of workspace root")

        upstream = self.lock.get("upstream")
        if not isinstance(upstream, dict):
            raise PatchBundleError("bundle upstream lock is invalid")
        expected_commit = upstream.get("commit")
        if (
            not isinstance(expected_commit, str)
            or len(expected_commit) != 40
            or any(character not in "0123456789abcdef" for character in expected_commit)
        ):
            raise PatchBundleError("bundle upstream commit is invalid")

        patches = self._validated_patches()
        artifacts = self._validated_artifacts()
        artifact_provenance = self._validated_artifact_provenance(
            patches,
            artifacts,
        )
        verification_command = self._verification_command()
        verification_environment = self._verification_environment()
        git_top_level = Path(
            self._git_output(source_path, "rev-parse", "--show-toplevel")
        ).resolve(strict=True)
        if source_path != git_top_level:
            raise PatchBundleError("upstream source must equal its Git top-level")
        current_commit = self._git_output(
            source_path,
            "rev-parse",
            "HEAD",
        )
        if current_commit != expected_commit:
            raise PatchBundleError("upstream commit mismatch")
        if self._git_output(
            source_path,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ):
            raise PatchBundleError("upstream source must be clean")

        python_path: Path | None = None
        if test_python is not None:
            # Preserve the venv launcher path. Resolving the symlink to the
            # base interpreter drops the venv's site-packages when the Core
            # test wrapper re-executes it in a clean environment.
            python_path = Path(test_python).expanduser().absolute()
            if not python_path.is_file() or not os.access(python_path, os.X_OK):
                raise PatchBundleError("test Python must be an executable file")

        partial: Path | None = None
        archive_fd: int | None = None
        archive_path: Path | None = None
        verification_home: Path | None = None
        try:
            partial = Path(
                tempfile.mkdtemp(
                    prefix=f".{target_path.name}.partial-",
                    dir=workspace_path,
                )
            )
            archive_fd, archive_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.archive-",
                suffix=".tar",
                dir=workspace_path,
            )
            archive_path = Path(archive_name)
            os.close(archive_fd)
            archive_fd = None
            self._run(
                [
                    "git",
                    "-C",
                    str(source_path),
                    "archive",
                    "--format=tar",
                    f"--output={archive_path}",
                    expected_commit,
                ],
                cwd=workspace_path,
                error_message="upstream archive failed",
                git_read_only=True,
            )
            with tarfile.open(archive_path, mode="r") as archive:
                archive.extractall(path=partial, filter="data")
            archive_path.unlink()
            archive_path = None

            # The workspace may itself sit inside the caller's Git worktree
            # and may be ignored there. Without an inner repository, git apply
            # can silently report ignored patch paths as "Skipped" with exit 0.
            # A disposable inner repository pins path resolution to the archive.
            self._run(
                ["git", "init", "-q"],
                cwd=partial,
                error_message="isolated git initialization failed",
            )

            for patch in patches:
                self._run(
                    ["git", "apply", "--check", "-"],
                    cwd=partial,
                    error_message="patch preflight failed",
                    input_bytes=patch.content,
                )
                self._run(
                    ["git", "apply", "-"],
                    cwd=partial,
                    error_message="patch apply failed",
                    input_bytes=patch.content,
                )
                self._run(
                    ["git", "apply", "--reverse", "--check", "-"],
                    cwd=partial,
                    error_message="patch post-apply verification failed",
                    input_bytes=patch.content,
                )

            if artifact_provenance is not None:
                self._verify_patched_source_provenance(
                    partial,
                    artifact_provenance,
                )

            self._verify_locked_environment(
                partial,
                python_path=python_path,
                environment=verification_environment,
            )

            shutil.rmtree(partial / ".git")

            evidence = {
                "schema_version": 1,
                "stage": self.stage,
                "upstream_commit": expected_commit,
                "patches": [
                    {
                        "path": patch.relative_path,
                        "sha256": patch.sha256,
                    }
                    for patch in patches
                ],
                "artifacts": [
                    {
                        "path": artifact.relative_path,
                        "sha256": artifact.sha256,
                    }
                    for artifact in artifacts
                ],
            }
            if artifact_provenance is not None:
                evidence["artifact_provenance"] = {
                    "stage3_patch_sha256": (
                        artifact_provenance.stage3_patch_sha256
                    ),
                    "source_files": [
                        {
                            "path": source.relative_path,
                            "sha256": source.sha256,
                        }
                        for source in artifact_provenance.source_files
                    ],
                }
            (partial / "APPLIED_BUNDLE.json").write_text(
                json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            command = list(verification_command)
            if command[0] == "python":
                command[0] = str(python_path or Path(sys.executable).resolve())
            env = os.environ.copy()
            if python_path is not None:
                env["HERMES_PYTHON"] = str(python_path)
            verification_home = Path(
                tempfile.mkdtemp(
                    prefix=f".{target_path.name}.verification-home-",
                    dir=workspace_path,
                )
            )
            env["HOME"] = str(verification_home)
            env["HERMES_HOME"] = str(verification_home / ".hermes")
            env["XDG_CONFIG_HOME"] = str(verification_home / ".config")
            env["XDG_STATE_HOME"] = str(verification_home / ".local" / "state")
            env["XDG_DATA_HOME"] = str(verification_home / ".local" / "share")
            self._run(
                command,
                cwd=partial,
                error_message="verification command failed",
                env=env,
            )
            self._remove_cache_artifacts(partial)
            partial.replace(target_path)
        except PatchBundleError:
            raise
        except (OSError, tarfile.TarError) as error:
            raise PatchBundleError("isolated patch preparation failed") from error
        finally:
            if archive_fd is not None:
                try:
                    os.close(archive_fd)
                except OSError:
                    pass
            if archive_path is not None:
                try:
                    archive_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if verification_home is not None:
                shutil.rmtree(verification_home, ignore_errors=True)
            if partial is not None and partial.exists():
                shutil.rmtree(partial, ignore_errors=True)

        return ApplyResult(
            upstream_commit=expected_commit,
            target=target_path,
            stage=self.stage,
            verification_command=verification_command,
        )

    @staticmethod
    def _validated_stage(lock: dict[str, Any]) -> int:
        stage = lock.get("stage")
        if type(stage) is not int or stage not in _SUPPORTED_PATCH_STAGES:
            raise PatchBundleError("bundle stage is invalid")
        patches = lock.get("patches")
allowed_patch_counts = {
    1: frozenset({1}),
    2: frozenset({2}),
    3: frozenset({3, 4}),
}
if (
    not isinstance(patches, list)
    or len(patches) not in allowed_patch_counts[stage]
):
    raise PatchBundleError("bundle patch set does not match stage")
        for ordinal, entry in enumerate(patches, start=1):
            relative = entry.get("path") if isinstance(entry, dict) else None
            prefix = f"patches/{ordinal:04d}-"
            if (
                not isinstance(relative, str)
                or not relative.startswith(prefix)
                or not relative.endswith(".patch")
                or "/" in relative[len(prefix) :]
                or relative == f"{prefix}.patch"
            ):
                raise PatchBundleError("bundle patch set does not match stage")
        return stage

    @staticmethod
    def _remove_cache_artifacts(root: Path) -> None:
        cache_directories = {
            "__pycache__",
            ".pytest-cache",
            ".pytest_cache",
            ".ruff_cache",
        }
        cache_suffixes = {".pyc", ".pyo"}
        cache_files = {"test_durations.json"}
        for current_root, directory_names, file_names in os.walk(
            root,
            topdown=False,
            followlinks=False,
        ):
            current = Path(current_root)
            for file_name in file_names:
                path = current / file_name
                if path.name in cache_files or path.suffix in cache_suffixes:
                    path.unlink()
            for directory_name in directory_names:
                path = current / directory_name
                if directory_name not in cache_directories:
                    continue
                if path.is_symlink():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        forbidden = [
            path
            for path in root.rglob("*")
            if path.name in cache_directories or path.suffix in cache_suffixes
        ]
        if forbidden:
            raise PatchBundleError("verification cache cleanup failed")

    def _validated_patches(self) -> tuple[_ValidatedPatch, ...]:
        raw_patches = self.lock.get("patches")
        if not isinstance(raw_patches, list) or not raw_patches:
            raise PatchBundleError("bundle patch list is invalid")
        patches: list[_ValidatedPatch] = []
        for entry in raw_patches:
            if not isinstance(entry, dict):
                raise PatchBundleError("bundle patch entry is invalid")
            relative = entry.get("path")
            expected_digest = entry.get("sha256")
            if not isinstance(relative, str) or not relative:
                raise PatchBundleError("bundle patch path is invalid")
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(character not in "0123456789abcdef" for character in expected_digest)
            ):
                raise PatchBundleError("bundle patch digest is invalid")
            patch_path = (self.root / relative).resolve(strict=True)
            if self.root not in patch_path.parents:
                raise PatchBundleError("bundle patch path escapes bundle root")
            if not patch_path.is_file():
                raise PatchBundleError("bundle patch is not a file")
            try:
                content = patch_path.read_bytes()
            except OSError as error:
                raise PatchBundleError("bundle patch is unavailable") from error
            actual_digest = hashlib.sha256(content).hexdigest()
            if actual_digest != expected_digest:
                raise PatchBundleError("patch digest mismatch")
            patches.append(
                _ValidatedPatch(
                    relative_path=relative,
                    sha256=expected_digest,
                    content=content,
                )
            )
        return tuple(patches)

    def _validated_artifacts(self) -> tuple[_ValidatedArtifact, ...]:
        raw_artifacts = self.lock.get("artifacts", [])
        if not isinstance(raw_artifacts, list):
            raise PatchBundleError("bundle artifact list is invalid")
        artifacts: list[_ValidatedArtifact] = []
        for entry in raw_artifacts:
            if not isinstance(entry, dict):
                raise PatchBundleError("bundle artifact entry is invalid")
            relative = entry.get("path")
            expected_digest = entry.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative.startswith("dist/")
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
            ):
                raise PatchBundleError("bundle artifact path is invalid")
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(character not in "0123456789abcdef" for character in expected_digest)
            ):
                raise PatchBundleError("bundle artifact digest is invalid")
            try:
                artifact_path = (self.root / relative).resolve(strict=True)
            except OSError as error:
                raise PatchBundleError("bundle artifact is unavailable") from error
            if self.root not in artifact_path.parents or not artifact_path.is_file():
                raise PatchBundleError("bundle artifact path escapes bundle root")
            try:
                actual_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            except OSError as error:
                raise PatchBundleError("bundle artifact is unavailable") from error
            if actual_digest != expected_digest:
                raise PatchBundleError("artifact digest mismatch")
            artifacts.append(
                _ValidatedArtifact(
                    relative_path=relative,
                    sha256=expected_digest,
                )
            )
        return tuple(artifacts)

    def _validated_artifact_provenance(
        self,
        patches: Sequence[_ValidatedPatch],
        artifacts: Sequence[_ValidatedArtifact],
    ) -> _ArtifactProvenance | None:
        raw = self.lock.get("artifact_provenance")
        if raw is None and self.stage < 3:
            return None
        if (
            not isinstance(raw, dict)
            or set(raw) != {
                "schema_version",
                "stage3_patch_sha256",
                "source_files",
            }
            or raw.get("schema_version") != 1
        ):
            raise PatchBundleError("artifact provenance is invalid")
        stage3_digest = raw.get("stage3_patch_sha256")
        if (
            not isinstance(stage3_digest, str)
            or not patches
            or stage3_digest != patches[-1].sha256
        ):
            raise PatchBundleError("artifact provenance patch mismatch")
        raw_sources = raw.get("source_files")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise PatchBundleError("artifact provenance is invalid")
        source_files: list[_ArtifactSourceFile] = []
        seen_paths: set[str] = set()
        for entry in raw_sources:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise PatchBundleError("artifact provenance is invalid")
            relative = entry.get("path")
            expected_digest = entry.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or relative.startswith("/")
                or "\\" in relative
                or ".." in Path(relative).parts
                or relative in seen_paths
            ):
                raise PatchBundleError("artifact provenance source path is invalid")
            if (
                not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_digest
                )
            ):
                raise PatchBundleError("artifact provenance source digest is invalid")
            seen_paths.add(relative)
            source_files.append(_ArtifactSourceFile(relative, expected_digest))

        wheel_paths = [
            self.root / artifact.relative_path
            for artifact in artifacts
            if artifact.relative_path.endswith(".whl")
        ]
        sdist_paths = [
            self.root / artifact.relative_path
            for artifact in artifacts
            if artifact.relative_path.endswith(".tar.gz")
        ]
        if len(wheel_paths) != 1 or len(sdist_paths) != 1:
            raise PatchBundleError("artifact provenance distributions are invalid")
        try:
            with zipfile.ZipFile(wheel_paths[0]) as wheel:
                wheel_names = wheel.namelist()
                for source in source_files:
                    if wheel_names.count(source.relative_path) != 1:
                        raise PatchBundleError("artifact provenance mismatch")
                    content = wheel.read(source.relative_path)
                    if hashlib.sha256(content).hexdigest() != source.sha256:
                        raise PatchBundleError("artifact provenance mismatch")
            with tarfile.open(sdist_paths[0], mode="r:gz") as sdist:
                members = sdist.getmembers()
                for source in source_files:
                    matches = [
                        member
                        for member in members
                        if member.name == source.relative_path
                        or member.name.endswith(f"/{source.relative_path}")
                    ]
                    if len(matches) != 1 or not matches[0].isfile():
                        raise PatchBundleError("artifact provenance mismatch")
                    extracted = sdist.extractfile(matches[0])
                    if extracted is None:
                        raise PatchBundleError("artifact provenance mismatch")
                    if hashlib.sha256(extracted.read()).hexdigest() != source.sha256:
                        raise PatchBundleError("artifact provenance mismatch")
        except PatchBundleError:
            raise
        except (OSError, KeyError, tarfile.TarError, zipfile.BadZipFile) as error:
            raise PatchBundleError("artifact provenance mismatch") from error
        return _ArtifactProvenance(stage3_digest, tuple(source_files))

    @staticmethod
    def _verify_patched_source_provenance(
        partial: Path,
        provenance: _ArtifactProvenance,
    ) -> None:
        for source in provenance.source_files:
            try:
                source_path = (partial / source.relative_path).resolve(strict=True)
                if partial not in source_path.parents or not source_path.is_file():
                    raise PatchBundleError("patched source provenance mismatch")
                digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
            except PatchBundleError:
                raise
            except OSError as error:
                raise PatchBundleError("patched source provenance mismatch") from error
            if digest != source.sha256:
                raise PatchBundleError("patched source provenance mismatch")

    @staticmethod
    def _absolute_without_resolving(path: Path | str) -> Path:
        expanded = Path(path).expanduser()
        return expanded if expanded.is_absolute() else Path.cwd() / expanded

    @staticmethod
    def _reject_symlink_components(path: Path, label: str) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as error:
                raise PatchBundleError(f"{label} path inspection failed") from error
            if stat.S_ISLNK(mode):
                raise PatchBundleError(
                    f"{label} path must not contain a symbolic link"
                )

    def _verification_command(self) -> tuple[str, ...]:
        verification = self.lock.get("verification")
        command = verification.get("command") if isinstance(verification, dict) else None
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise PatchBundleError("bundle verification command is invalid")
        return tuple(command)

    def _verification_environment(self) -> dict[str, tuple[str, ...]] | None:
        verification = self.lock.get("verification")
        raw = (
            verification.get("environment")
            if isinstance(verification, dict)
            else None
        )
        if raw is None:
            return None
        expected_command = (
            "uv",
            "sync",
            "--extra",
            "all",
            "--extra",
            "dev",
            "--locked",
            "--check",
            "--no-install-project",
        )
        if (
            not isinstance(raw, dict)
            or set(raw) != {"kind", "command", "required_imports"}
            or raw.get("kind") != "uv-all-dev-locked"
            or tuple(raw.get("command", ())) != expected_command
            or raw.get("required_imports") != ["pytest", "rich"]
        ):
            raise PatchBundleError("bundle verification environment is invalid")
        return {
            "command": expected_command,
            "required_imports": ("pytest", "rich"),
        }

    @staticmethod
    def _verify_locked_environment(
        partial: Path,
        *,
        python_path: Path | None,
        environment: dict[str, tuple[str, ...]] | None,
    ) -> None:
        if environment is None:
            return
        if python_path is None:
            raise PatchBundleError(
                "locked verification environment is unavailable"
            )
        import_script = "\n".join(
            f"import {module}" for module in environment["required_imports"]
        )
        PatchBundle._run(
            [str(python_path), "-c", import_script],
            cwd=partial,
            error_message="locked verification environment is unavailable",
        )
        uv_path = shutil.which("uv")
        if uv_path is None:
            raise PatchBundleError(
                "locked verification environment is unavailable"
            )
        command = [uv_path, *environment["command"][1:]]
        child_env = os.environ.copy()
        child_env["UV_PROJECT_ENVIRONMENT"] = str(python_path.parent.parent)
        PatchBundle._run(
            command,
            cwd=partial,
            env=child_env,
            error_message="locked verification environment is unavailable",
        )

    @staticmethod
    def _git_output(source: Path, *args: str) -> str:
        completed = PatchBundle._run(
            ["git", "-C", str(source), *args],
            cwd=source,
            error_message="upstream git inspection failed",
            git_read_only=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        cwd: Path,
        error_message: str,
        env: dict[str, str] | None = None,
        git_read_only: bool = False,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        child_env = dict(env or os.environ)
        if git_read_only:
            child_env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            return subprocess.run(
                list(command),
                cwd=cwd,
                env=child_env,
                check=True,
                capture_output=True,
                input=input_bytes,
                text=input_bytes is None,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise PatchBundleError(error_message) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply the pinned Host SPI v1 patch to a new isolated target.",
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--test-python", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = PatchBundle(Path(__file__).resolve().parent.parent).apply_and_verify(
            source=args.source,
            workspace_root=args.workspace_root,
            target=args.target,
            test_python=args.test_python,
        )
    except PatchBundleError as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "stage": result.stage,
                "target": str(result.target),
                "upstream_commit": result.upstream_commit,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
