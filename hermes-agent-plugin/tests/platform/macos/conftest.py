"""Short private filesystem fixtures for macOS platform tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def short_root():
    with tempfile.TemporaryDirectory(
        prefix="hmg-local-gateway-",
        dir=Path("/tmp").resolve(strict=True),
    ) as directory:
        yield Path(directory)


@pytest.fixture
def short_private_directory():
    with tempfile.TemporaryDirectory(
        prefix="hermes-trust-",
        dir=Path("/tmp").resolve(strict=True),
    ) as raw:
        directory = Path(raw)
        directory.chmod(0o700)
        yield directory
