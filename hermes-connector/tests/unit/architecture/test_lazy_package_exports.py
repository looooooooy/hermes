"""Package imports must not eagerly require runtime infrastructure."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_CONNECTOR_ROOT = Path(__file__).resolve().parents[3]


def test_owner_control_import_is_infrastructure_lazy() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_CONNECTOR_ROOT / "src")
    script = """
import sys

import hermes_connector
import hermes_connector.application
import hermes_connector.bootstrap
from hermes_connector.application.owner_control_lane import OwnerControlLane

assert hermes_connector.ConnectorConfig.__name__ == "ConnectorConfig"
assert OwnerControlLane.__name__ == "OwnerControlLane"
assert "sqlalchemy" not in sys.modules
assert "alembic" not in sys.modules
assert "hermes_connector.bootstrap.runtime" not in sys.modules
assert "hermes_connector.adapters.sqlite_storage" not in sys.modules
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_CONNECTOR_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
