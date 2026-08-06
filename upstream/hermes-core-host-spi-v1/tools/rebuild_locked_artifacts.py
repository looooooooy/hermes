"""Rebuild locked Core distributions from the pinned patched upstream source."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .apply_and_verify import PatchBundle, PatchBundleError

_EXPECTED_BUILD_COMMAND = (
    "uv",
    "build",
    "--wheel",
    "--sdist",
    "--out-dir",
    "dist",
    "--clear",
    "--no-create-gitignore",
)
_EXPECTED_BUILD_ENVIRONMENT = {"HERMES_NIX_BUILD": "1"}


@dataclass(frozen=True)
class _ArtifactSpec:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class _SourceSpec:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class RebuildResult:
    output_directory: Path
    artifacts: tuple[_ArtifactSpec, ...]
    upstream_commit: str


def _canonical_digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PatchBundleError(f"{label} digest is invalid")
    return value


def _artifact_specs(lock: Mapping[str, object]) -> tuple[_ArtifactSpec, ...]:
    raw = lock.get("artifacts")
    if not isinstance(raw, list) or not raw:
        raise PatchBundleError("bundle artifact list is invalid")
    specs: list[_ArtifactSpec] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise PatchBundleError("bundle artifact entry is invalid")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or not relative.startswith("dist/")
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
        ):
            raise PatchBundleError("bundle artifact path is invalid")
        seen.add(relative)
        specs.append(
            _ArtifactSpec(
                relative_path=relative,
                sha256=_canonical_digest(
                    entry.get("sha256"),
                    label="artifact",
                ),
            )
        )
    if sum(spec.relative_path.endswith(".whl") for spec in specs) != 1 or sum(
        spec.relative_path.endswith(".tar.gz") for spec in specs
    ) != 1:
        raise PatchBundleError("bundle artifact distributions are invalid")
    return tuple(specs)


def _build_configuration(
    lock: Mapping[str, object],
) -> tuple[int, tuple[str, ...], dict[str, str]]:
    raw = lock.get("artifact_build")
    if not isinstance(raw, Mapping) or set(raw) != {
        "source_date_epoch",
        "environment",
        "command",
    }:
        raise PatchBundleError("artifact build configuration is invalid")
    source_date_epoch = raw.get("source_date_epoch")
    command = raw.get("command")
    environment = raw.get("environment")
    normalized_command = tuple(command) if isinstance(command, list) else ()
    if type(source_date_epoch) is not int or source_date_epoch <= 0:
        raise PatchBundleError("artifact build epoch is invalid")
    if normalized_command != _EXPECTED_BUILD_COMMAND:
        raise PatchBundleError("artifact build command is invalid")
    if not isinstance(environment, Mapping) or dict(environment) != (
        _EXPECTED_BUILD_ENVIRONMENT
    ):
        raise PatchBundleError("artifact build environment is invalid")
    return source_date_epoch, _EXPECTED_BUILD_COMMAND, dict(_EXPECTED_BUILD_ENVIRONMENT)


def _source_specs(
    lock: Mapping[str, object],
    *,
    stage: int,
    final_patch_digest: str,
) -> tuple[_SourceSpec, ...]:
    raw = lock.get("artifact_provenance")
    if raw is None and stage < 3:
        return ()
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {
            "schema_version",
            "stage3_patch_sha256",
            "source_files",
        }
        or raw.get("schema_version") != 1
        or raw.get("stage3_patch_sha256") != final_patch_digest
    ):
        raise PatchBundleError("artifact provenance is invalid")
    entries = raw.get("source_files")
    if not isinstance(entries, list) or not entries:
        raise PatchBundleError("artifact provenance is invalid")
    specs: list[_SourceSpec] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256"}:
            raise PatchBundleError("artifact provenance is invalid")
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or "\\" in relative
            or ".." in Path(relative).parts
            or relative in seen
        ):
            raise PatchBundleError("artifact provenance source path is invalid")
        seen.add(relative)
        specs.append(
            _SourceSpec(
                relative_path=relative,
                sha256=_canonical_digest(
                    entry.get("sha256"),
                    label="artifact provenance source",
                ),
            )
        )
    return tuple(specs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_source(
    bundle: PatchBundle,
    source: Path,
) -> str:
    upstream = bundle.lock.get("upstream")
    expected_commit = upstream.get("commit") if isinstance(upstream, Mapping) else None
    if (
        not isinstance(expected_commit, str)
        or len(expected_commit) != 40
        or any(character not in "0123456789abcdef" for character in expected_commit)
    ):
        raise PatchBundleError("bundle upstream commit is invalid")
    if not source.is_dir():
        raise PatchBundleError("upstream source must be a directory")
    top_level = Path(
        bundle._git_output(source, "rev-parse", "--show-toplevel")
    ).resolve(strict=True)
    if top_level != source:
        raise PatchBundleError("upstream source must equal its Git top-level")
    if bundle._git_output(source, "rev-parse", "HEAD") != expected_commit:
        raise PatchBundleError("upstream commit mismatch")
    if bundle._git_output(
        source,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ):
        raise PatchBundleError("upstream source must be clean")
    return expected_commit


def _prepare_build_tree(
    bundle: PatchBundle,
    *,
    source: Path,
    commit: str,
    root: Path,
) -> Path:
    build_root = root / "source"
    build_root.mkdir()
    archive = root / "upstream.tar"
    bundle._run(
        [
            "git",
            "-C",
            str(source),
            "archive",
            "--format=tar",
            f"--output={archive}",
            commit,
        ],
        cwd=root,
        error_message="upstream archive failed",
        git_read_only=True,
    )
    try:
        with tarfile.open(archive, mode="r") as source_archive:
            source_archive.extractall(path=build_root, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise PatchBundleError("upstream archive extraction failed") from error
    bundle._run(
        ["git", "init", "-q"],
        cwd=build_root,
        error_message="isolated git initialization failed",
    )
    for patch in bundle._validated_patches():
        bundle._run(
            ["git", "apply", "--check", "-"],
            cwd=build_root,
            error_message="patch preflight failed",
            input_bytes=patch.content,
        )
        bundle._run(
            ["git", "apply", "-"],
            cwd=build_root,
            error_message="patch apply failed",
            input_bytes=patch.content,
        )
        bundle._run(
            ["git", "apply", "--reverse", "--check", "-"],
            cwd=build_root,
            error_message="patch post-apply verification failed",
            input_bytes=patch.content,
        )
    return build_root


def _verify_source_provenance(
    build_root: Path,
    sources: Sequence[_SourceSpec],
) -> None:
    for source in sources:
        try:
            path = (build_root / source.relative_path).resolve(strict=True)
        except OSError as error:
            raise PatchBundleError("patched source provenance mismatch") from error
        if build_root not in path.parents or not path.is_file():
            raise PatchBundleError("patched source provenance mismatch")
        if _sha256(path) != source.sha256:
            raise PatchBundleError("patched source provenance mismatch")


def _canonicalize_sdist(generated: Path, *, source_date_epoch: int) -> None:
    candidates = tuple(sorted(generated.glob("*.tar.gz")))
    if len(candidates) != 1:
        raise PatchBundleError("artifact build sdist set is invalid")
    source = candidates[0]
    temporary = source.with_name(f".{source.name}.canonical")
    try:
        with gzip.open(source, "rb") as compressed:
            tar_payload = compressed.read()
        with temporary.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw_output,
                mtime=source_date_epoch,
            ) as canonical:
                canonical.write(tar_payload)
        temporary.replace(source)
    except (EOFError, gzip.BadGzipFile, OSError) as error:
        raise PatchBundleError("artifact sdist canonicalization failed") from error
    finally:
        temporary.unlink(missing_ok=True)


def _build_artifacts(
    bundle: PatchBundle,
    *,
    build_root: Path,
    source_date_epoch: int,
    command: Sequence[str],
    environment: Mapping[str, str],
) -> Path:
    uv = shutil.which(command[0])
    if uv is None:
        raise PatchBundleError("artifact builder is unavailable")
    child_environment = os.environ.copy()
    child_environment.update(environment)
    child_environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
    child_environment["UV_NO_PROGRESS"] = "1"
    bundle._run(
        [uv, *command[1:]],
        cwd=build_root,
        env=child_environment,
        error_message="artifact build failed",
    )
    generated = build_root / "dist"
    _canonicalize_sdist(generated, source_date_epoch=source_date_epoch)
    return generated


def _verify_generated_artifacts(
    generated: Path,
    specs: Sequence[_ArtifactSpec],
) -> None:
    if not generated.is_dir():
        raise PatchBundleError("artifact build output is unavailable")
    expected = {
        Path(spec.relative_path).relative_to("dist").as_posix() for spec in specs
    }
    actual = {
        path.relative_to(generated).as_posix()
        for path in generated.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise PatchBundleError("artifact build output set is invalid")
    for spec in specs:
        path = generated / Path(spec.relative_path).relative_to("dist")
        actual_digest = _sha256(path)
        if actual_digest != spec.sha256:
            raise PatchBundleError(
                "rebuilt artifact digest mismatch: "
                f"{spec.relative_path} expected={spec.sha256} "
                f"actual={actual_digest}"
            )


def rebuild_locked_artifacts(
    bundle_root: Path | str,
    *,
    source: Path | str,
) -> RebuildResult:
    bundle = PatchBundle(bundle_root)
    source_path = Path(source).expanduser().resolve(strict=True)
    expected_commit = _validate_source(bundle, source_path)
    artifacts = _artifact_specs(bundle.lock)
    source_date_epoch, build_command, build_environment = _build_configuration(
        bundle.lock
    )
    patches = bundle._validated_patches()
    sources = _source_specs(
        bundle.lock,
        stage=bundle.stage,
        final_patch_digest=patches[-1].sha256,
    )
    output = bundle.root / "dist"
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise PatchBundleError("artifact output directory must be absent or empty")
        output.rmdir()

    temporary_root = Path(
        tempfile.mkdtemp(prefix=".artifact-rebuild-", dir=bundle.root.parent)
    )
    published = False
    try:
        build_root = _prepare_build_tree(
            bundle,
            source=source_path,
            commit=expected_commit,
            root=temporary_root,
        )
        _verify_source_provenance(build_root, sources)
        generated = _build_artifacts(
            bundle,
            build_root=build_root,
            source_date_epoch=source_date_epoch,
            command=build_command,
            environment=build_environment,
        )
        _verify_generated_artifacts(generated, artifacts)
        generated.replace(output)
        published = True
        validated = bundle._validated_artifacts()
        bundle._validated_artifact_provenance(patches, validated)
    except PatchBundleError:
        if published:
            shutil.rmtree(output, ignore_errors=True)
        raise
    except OSError as error:
        if published:
            shutil.rmtree(output, ignore_errors=True)
        raise PatchBundleError("artifact rebuild failed") from error
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)

    return RebuildResult(
        output_directory=output,
        artifacts=artifacts,
        upstream_commit=expected_commit,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild the locked Host SPI wheel and sdist from pinned source.",
    )
    parser.add_argument("--source", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = rebuild_locked_artifacts(
            Path(__file__).resolve().parent.parent,
            source=args.source,
        )
    except PatchBundleError as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "artifacts": [
                    {
                        "path": artifact.relative_path,
                        "sha256": artifact.sha256,
                    }
                    for artifact in result.artifacts
                ],
                "output_directory": str(result.output_directory),
                "upstream_commit": result.upstream_commit,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
