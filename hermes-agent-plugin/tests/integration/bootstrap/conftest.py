"""Short filesystem fixtures for runtime bootstrap integration tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def short_socket_root():
    with tempfile.TemporaryDirectory(
        prefix="hmg-lifecycle-",
        dir=Path("/tmp").resolve(strict=True),
    ) as directory:
        yield Path(directory)
