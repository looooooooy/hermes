from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

WINDOWS_PACKAGING = Path(__file__).parents[2] / "packaging" / "windows"
sys.path.insert(0, str(WINDOWS_PACKAGING))

from hermes_windows_activation import WindowsActivationController, WindowsActivationError
from hermes_windows_activation_platform import (
    SubprocessWindowsActivationPlatform,
    WindowsPrivateActivationStore,
)
from hermes_windows_release import render_windows_runtime_evidence
from hermes_windows_tasks import build_connector_task, delete_task_command, query_task_command

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows activation required")


class ControlledHealthPlatform(SubprocessWindowsActivationPlatform):
    def __init__(self) -> None:
        self.health: dict[str, bool] = {}

    def healthy(self, task, *, timeout_seconds: float) -> bool:
        assert timeout_seconds > 0
        return self.health.get(task.release_id, False)


def _release(root: Path, release_id: str, marker: bytes) -> Path:
    release = (root / "releases" / release_id).resolve()
    (release / "manifest").mkdir(parents=True)
    (release / "connector").mkdir()
    (release / "receipts").mkdir()
    release_digest = (marker.hex() * 64)[:64]
    (release / "manifest" / "release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": release_id,
                "release_digest": release_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    (release / "connector" / "hermes-connector.exe").write_bytes(
        b"not-a-real-exe:" + marker
    )
    (release / "receipts" / "windows-runtime.json").write_bytes(
        render_windows_runtime_evidence(
            release_dir=release,
            expected_release_id=release_id,
        )
    )
    return release


def test_real_task_projection_rolls_back_to_previous_exact_release(
    tmp_path: Path,
) -> None:
    profile = f"ci-{uuid4().hex[:10]}"
    home = (tmp_path / "HermesHome").resolve()
    config = (home / "connector" / "profiles" / profile / "config.json").resolve()
    first = _release(tmp_path, "2026.08.08+1", b"a")
    second = _release(tmp_path, "2026.08.08+2", b"b")
    store = WindowsPrivateActivationStore(hermes_home=home, profile=profile)
    platform = ControlledHealthPlatform()
    platform.health["2026.08.08+1"] = True
    controller = WindowsActivationController(
        profile=profile,
        hermes_home=home,
        config_file=config,
        store=store,
        platform=platform,
        health_timeout_seconds=2.0,
    )
    cleanup_task = build_connector_task(
        release_dir=first,
        release_id="2026.08.08+1",
        profile=profile,
        hermes_home=home,
        config_file=config,
    )
    try:
        first_result = controller.activate(
            release_dir=first,
            release_id="2026.08.08+1",
        )
        assert first_result.active.release_id == "2026.08.08+1"

        queried = subprocess.run(
            query_task_command(cleanup_task),
            check=True,
            capture_output=True,
            text=True,
        )
        assert "run-connector.cmd" in queried.stdout
        assert "2026.08.08+1" not in queried.stdout

        platform.health["2026.08.08+2"] = False
        with pytest.raises(WindowsActivationError, match="activation failed"):
            controller.activate(
                release_dir=second,
                release_id="2026.08.08+2",
            )

        active = json.loads((store.read("active.json") or b"").decode("utf-8"))
        assert active["active"]["release_id"] == "2026.08.08+1"
        launcher = store.launcher_path.read_text(encoding="utf-8")
        assert "2026.08.08+1" in launcher
        assert "2026.08.08+2" not in launcher
        assert store.read("pending.json") is None
        assert store.read("blocked.json") is None
    finally:
        subprocess.run(
            delete_task_command(cleanup_task),
            check=False,
            capture_output=True,
            text=True,
        )
