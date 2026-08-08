from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

WINDOWS_PACKAGING = Path(__file__).parents[2] / "packaging" / "windows"
sys.path.insert(0, str(WINDOWS_PACKAGING))

from hermes_windows_tasks import (
    build_connector_task,
    create_task_command,
    delete_task_command,
    query_task_command,
    render_connector_launcher,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Scheduled Tasks required")


def test_windows_scheduled_task_registers_private_launcher_as_limited_user(
    tmp_path: Path,
) -> None:
    profile = f"ci-{uuid4().hex[:10]}"
    home = (tmp_path / "HermesHome").resolve()
    release_id = "2026.08.08-ci"
    release = (home / "releases" / release_id).resolve()
    config = (home / "connector" / "profiles" / profile / "config.json").resolve()
    task = build_connector_task(
        release_dir=release,
        release_id=release_id,
        profile=profile,
        hermes_home=home,
        config_file=config,
    )
    task.launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher = render_connector_launcher(task)
    task.launcher.write_bytes(launcher)

    subprocess.run(create_task_command(task), check=True, capture_output=True, text=True)
    try:
        queried = subprocess.run(
            query_task_command(task),
            check=True,
            capture_output=True,
            text=True,
        )
        xml = queried.stdout
        assert "<LogonTrigger>" in xml
        assert "<LogonType>InteractiveToken</LogonType>" in xml
        assert "<RunLevel>HighestAvailable</RunLevel>" not in xml
        assert "cmd.exe" in xml
        assert "run-connector.cmd" in xml
        assert release_id not in xml
        assert release_id in launcher.decode("utf-8")
        assert "<Password>" not in xml
    finally:
        subprocess.run(
            delete_task_command(task),
            check=False,
            capture_output=True,
            text=True,
        )
