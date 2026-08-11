"""Tests for rebuilding locked artifacts when binary files are not committed."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from tools.apply_and_verify import PatchBundleError
from tools.rebuild_locked_artifacts import rebuild_locked_artifacts

pytestmark = pytest.mark.skipif(
    os.name != "posix" or sys.platform not in {"darwin", "linux"},
    reason="artifact replay is supported only on macOS and Linux",
)

_WHEEL = b"deterministic-wheel\n"
_FILE_CONTENT = b"deterministic-sdist-file\n"
_SOURCE_DATE_EPOCH = 1_785_409_311


def _canonical_sdist_bytes() -> bytes:
    tar_output = io.BytesIO()
    with tarfile.open(
        fileobj=tar_output,
        mode="w",
        format=tarfile.PAX_FORMAT,
    ) as archive:
        directory = tarfile.TarInfo("package")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = _SOURCE_DATE_EPOCH
        directory.uid = 0
        directory.gid = 0
        directory.uname = ""
        directory.gname = ""
        directory.pax_headers = {}
        archive.addfile(directory)

        source = tarfile.TarInfo("package/value.txt")
        source.mode = 0o644
        source.mtime = _SOURCE_DATE_EPOCH
        source.uid = 0
        source.gid = 0
        source.uname = ""
        source.gname = ""
        source.pax_headers = {}
        source.size = len(_FILE_CONTENT)
        archive.addfile(source, io.BytesIO(_FILE_CONTENT))

    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=_SOURCE_DATE_EPOCH,
    ) as compressed:
        compressed.write(tar_output.getvalue())
    return output.getvalue()


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
    _run("git", "config", "user.email", "artifact@example.invalid", cwd=source)
    _run("git", "config", "user.name", "Artifact Builder", cwd=source)
    (source / "value.txt").write_text("before\n", encoding="utf-8")
    _run("git", "add", "value.txt", cwd=source)
    _run("git", "commit", "-q", "-m", "baseline", cwd=source)
    return source, _run("git", "rev-parse", "HEAD", cwd=source)


def _bundle(tmp_path: Path, commit: str) -> Path:
    root = tmp_path / "bundle"
    patches = root / "patches"
    patches.mkdir(parents=True)
    patch = patches / "0001-change-value.patch"
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
        "artifacts": [
            {
                "path": "dist/package.whl",
                "sha256": hashlib.sha256(_WHEEL).hexdigest(),
            },
            {
                "path": "dist/package.tar.gz",
                "sha256": hashlib.sha256(_canonical_sdist_bytes()).hexdigest(),
            },
        ],
        "artifact_build": {
            "source_date_epoch": _SOURCE_DATE_EPOCH,
            "environment": {"HERMES_NIX_BUILD": "1"},
            "command": [
                "uv",
                "build",
                "--wheel",
                "--sdist",
                "--out-dir",
                "dist",
                "--clear",
                "--no-create-gitignore",
            ],
        },
    }
    (root / "upstream.lock.json").write_text(
        json.dumps(lock),
        encoding="utf-8",
    )
    return root


def _fake_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "uv"
    binary.write_text(
        """#!/usr/bin/env python3
import gzip
import io
import tarfile
from pathlib import Path

output = Path.cwd() / "dist"
output.mkdir(parents=True, exist_ok=True)
(output / "package.whl").write_bytes(b"deterministic-wheel\\n")
raw_tar = io.BytesIO()
with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
    source = tarfile.TarInfo("package/value.txt")
    source.mode = 0o644
    source.mtime = 91
    source.uid = 501
    source.gid = 20
    source.uname = "runner"
    source.gname = "staff"
    source.pax_headers = {"comment": "volatile"}
    content = b"deterministic-sdist-file\\n"
    source.size = len(content)
    archive.addfile(source, io.BytesIO(content))

    directory = tarfile.TarInfo("package")
    directory.type = tarfile.DIRTYPE
    directory.mode = 0o755
    directory.mtime = 37
    directory.uid = 501
    directory.gid = 20
    directory.uname = "runner"
    directory.gname = "staff"
    directory.pax_headers = {"comment": "volatile"}
    archive.addfile(directory)
with (output / "package.tar.gz").open("wb") as raw:
    with gzip.GzipFile(
        filename="temporary-build-name.tar",
        mode="wb",
        compresslevel=6,
        fileobj=raw,
        mtime=7,
    ) as compressed:
        compressed.write(raw_tar.getvalue())
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_rebuilds_missing_locked_artifacts_without_mutating_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle = _bundle(tmp_path, commit)
    _fake_uv(tmp_path, monkeypatch)
    source_status = _run("git", "status", "--porcelain=v1", cwd=source)

    result = rebuild_locked_artifacts(bundle, source=source)

    assert result.upstream_commit == commit
    assert result.output_directory == bundle / "dist"
    assert (bundle / "dist/package.whl").read_bytes() == _WHEEL
    assert (bundle / "dist/package.tar.gz").read_bytes() == _canonical_sdist_bytes()
    assert _run("git", "status", "--porcelain=v1", cwd=source) == source_status
    assert _run("git", "rev-parse", "HEAD", cwd=source) == commit


def test_digest_mismatch_removes_partially_published_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle = _bundle(tmp_path, commit)
    lock_path = bundle / "upstream.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["artifacts"][0]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    _fake_uv(tmp_path, monkeypatch)

    with pytest.raises(PatchBundleError, match="rebuilt artifact digest mismatch"):
        rebuild_locked_artifacts(bundle, source=source)

    assert not (bundle / "dist").exists()


def test_refuses_to_replace_nonempty_artifact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle = _bundle(tmp_path, commit)
    output = bundle / "dist"
    output.mkdir()
    (output / "operator-file.txt").write_text("preserve", encoding="utf-8")
    _fake_uv(tmp_path, monkeypatch)

    with pytest.raises(
        PatchBundleError,
        match="artifact output directory must be absent or empty",
    ):
        rebuild_locked_artifacts(bundle, source=source)

    assert (output / "operator-file.txt").read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize(
    "workflow_name",
    ("hermes-core-host-spi.yml", "hermes-desktop-managed-release-payload.yml"),
)
def test_ci_workflows_rebuild_away_from_tracked_artifacts(
    workflow_name: str,
) -> None:
    workflow = (
        Path(__file__).parents[3]
        / ".github"
        / "workflows"
        / workflow_name
    ).read_text(encoding="utf-8")
    marker = (
        "Verify and rebuild locked patched Core artifacts"
        if workflow_name.startswith("hermes-desktop")
        else "Rebuild locked Core artifacts"
    )
    step = workflow.split(marker, 1)[1].split("- name:", 1)[0]

    assert 'verification_bundle="$RUNNER_TEMP/hermes-core-host-spi-v1"' in step
    assert 'cp -R "$bundle_root/patches" "$bundle_root/tools"' in step
    assert 'cmp "dist/hermes_agent-0.19.0-py3-none-any.whl"' in step
    assert 'cmp "dist/hermes_agent-0.19.0.tar.gz"' in step
    assert 'cd "$BUNDLE_ROOT"' not in step


def test_canonicalizes_tar_and_gzip_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, commit = _source_repo(tmp_path)
    bundle = _bundle(tmp_path, commit)
    _fake_uv(tmp_path, monkeypatch)

    rebuild_locked_artifacts(bundle, source=source)

    value = (bundle / "dist/package.tar.gz").read_bytes()
    assert value == _canonical_sdist_bytes()
    assert int.from_bytes(value[4:8], "little") == _SOURCE_DATE_EPOCH
    assert value[3] & 0x08 == 0
    with tarfile.open(bundle / "dist/package.tar.gz", mode="r:gz") as archive:
        assert [member.name for member in archive.getmembers()] == [
            "package",
            "package/value.txt",
        ]
        for member in archive.getmembers():
            assert member.mtime == _SOURCE_DATE_EPOCH
            assert member.uid == member.gid == 0
            assert member.uname == member.gname == ""
            assert member.pax_headers == {}
