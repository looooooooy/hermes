"""Tests for rebuilding locked artifacts when binary files are not committed."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tools.apply_and_verify import PatchBundleError
from tools.rebuild_locked_artifacts import rebuild_locked_artifacts

pytestmark = pytest.mark.skipif(
    os.name != "posix" or sys.platform not in {"darwin", "linux"},
    reason="artifact replay is supported only on macOS and Linux",
)

_WHEEL = b"deterministic-wheel\n"
_SDIST_PAYLOAD = b"deterministic-sdist-tar-payload\n"
_SOURCE_DATE_EPOCH = 1_785_409_311


def _canonical_sdist_bytes() -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=_SOURCE_DATE_EPOCH,
    ) as compressed:
        compressed.write(_SDIST_PAYLOAD)
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
from pathlib import Path

output = Path.cwd() / "dist"
output.mkdir(parents=True, exist_ok=True)
(output / "package.whl").write_bytes(b"deterministic-wheel\\n")
with (output / "package.tar.gz").open("wb") as raw:
    with gzip.GzipFile(
        filename="temporary-build-name.tar",
        mode="wb",
        compresslevel=9,
        fileobj=raw,
        mtime=7,
    ) as compressed:
        compressed.write(b"deterministic-sdist-tar-payload\\n")
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
    with gzip.open(bundle / "dist/package.tar.gz", "rb") as compressed:
        assert compressed.read() == _SDIST_PAYLOAD
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


def test_canonicalizes_gzip_header_metadata(
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
