from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
API_SOURCE_ROOTS = (
    PROJECT_ROOT / "src" / "hermes_cloud" / "modules" / "cloud_api",
    PROJECT_ROOT / "src" / "hermes_cloud" / "entrypoints" / "business_api",
)
API_COMPOSITION_FILES = (
    PROJECT_ROOT / "src" / "hermes_cloud" / "application" / "business_api.py",
)
FORBIDDEN_IMPORTS = ("hermes_cloud.platform", "sqlalchemy")
RAW_SQL = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\s+"
    r"(?:FROM|INTO|SET|TABLE|SCHEMA)\b",
    re.IGNORECASE,
)


def test_business_api_surface_has_no_platform_sqlalchemy_or_raw_sql() -> None:
    violations: list[str] = []
    source_files = (
        tuple(path for root in API_SOURCE_ROOTS for path in sorted(root.rglob("*.py")))
        + API_COMPOSITION_FILES
    )

    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(PROJECT_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported = (node.module or "",)
            else:
                imported = ()
            for module_name in imported:
                if module_name.startswith(FORBIDDEN_IMPORTS):
                    violations.append(f"{relative}:{node.lineno} imports {module_name}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and RAW_SQL.search(node.value)
            ):
                violations.append(f"{relative}:{node.lineno} contains raw SQL")

    assert violations == []
