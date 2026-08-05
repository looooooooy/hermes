"""Shared filesystem fixtures for macOS Local Gateway trust tests."""

from __future__ import annotations

import json
import socket
from pathlib import Path


def _private_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "registry"
    directory.mkdir(mode=0o700)
    return directory


def _registry_file(
    directory: Path,
    payload: dict,
    *,
    mode: int = 0o600,
) -> Path:
    path = directory / "gateway-1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _bind_unix_socket(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    return server
