from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[3] / "src" / "hermes_connector"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_cloud_code_has_layered_directories_and_bootstrap_entrypoint() -> None:
    assert (PACKAGE_ROOT / "adapters" / "cloud" / "codec.py").is_file()
    assert (PACKAGE_ROOT / "adapters" / "cloud" / "websocket_transport.py").is_file()
    assert (PACKAGE_ROOT / "bootstrap" / "cloud.py").is_file()

    flat_cloud_files = {
        path.name
        for path in (PACKAGE_ROOT / "adapters").glob("*.py")
        if path.stem.startswith(("cloud", "websocket"))
    }
    assert flat_cloud_files == set()


def test_domain_and_application_do_not_import_infrastructure_or_platforms() -> None:
    forbidden_roots = {"websockets", "sqlalchemy", "platform"}
    for layer in ("domain", "application"):
        for path in (PACKAGE_ROOT / layer).glob("*.py"):
            imported_roots = {name.split(".", 1)[0] for name in _imports(path)}
            assert forbidden_roots.isdisjoint(imported_roots), path


def test_cloud_application_depends_on_ports_not_adapters() -> None:
    imports = _imports(PACKAGE_ROOT / "application" / "cloud_wss_client.py")
    assert all(not name.startswith("hermes_connector.adapters") for name in imports)
