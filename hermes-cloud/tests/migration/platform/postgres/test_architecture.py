from __future__ import annotations

import ast
from pathlib import Path
from typing import get_origin, get_type_hints

import pytest
from sqlalchemy.orm import DeclarativeBase, Mapped

from hermes_cloud.platform.postgres.models import (
    ALL_TENANT_MODELS,
    HermesCloudBase,
    MigrationLedgerModel,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = PROJECT_ROOT / "src" / "hermes_cloud"
TYPED_DDL_COMPILER = SOURCE_ROOT / "platform" / "postgres" / "ddl.py"
NEUTRAL_LAYERS = ("application", "domain", "ports", "modules")
FORBIDDEN_NEUTRAL_IMPORTS = (
    "hermes_cloud.adapters",
    "hermes_cloud.platform",
    "sqlalchemy",
)
FORBIDDEN_CALLS = {"DDL", "exec_driver_sql", "execute_script", "text"}
FORBIDDEN_SQL_PREFIXES = (
    "select ",
    "insert ",
    "update ",
    "delete ",
    "create ",
    "alter ",
    "drop ",
    "grant ",
    "revoke ",
    "set ",
)


def test_postgres_models_use_sqlalchemy_2_declarative_mappings() -> None:
    assert issubclass(HermesCloudBase, DeclarativeBase)
    assert MigrationLedgerModel.__table__.schema == "public"
    assert len(ALL_TENANT_MODELS) == 33
    for model in ALL_TENANT_MODELS:
        assert issubclass(model, HermesCloudBase)
        assert "tenant_id" in model.__table__.columns
        assert get_origin(get_type_hints(model)["tenant_id"]) is Mapped


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            value.value if isinstance(value, ast.Constant) else "{}"
            for value in node.values
            if isinstance(value, (ast.Constant, ast.FormattedValue))
        )
    return None


def _is_docstring(node: ast.AST, parent: ast.AST | None) -> bool:
    return isinstance(parent, ast.Expr) and parent.value is node


def _is_reviewed_compiler_return(
    path: Path,
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    if path != TYPED_DDL_COMPILER:
        return False
    current = node
    returned = False
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Return):
            returned = True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return returned and current.name.startswith("_compile")
    return False


def _raw_sql_violations(source_root: Path) -> list[str]:
    violations: list[str] = []

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{path}:{node.lineno}:{name}")
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "execute_script"
            ):
                violations.append(f"{path}:{node.lineno}:execute_script")
            if isinstance(node, ast.Attribute) and node.attr == "sql":
                violations.append(f"{path}:{node.lineno}:Migration.sql")

            literal = _literal_text(node)
            parent = parents.get(node)
            if literal is None or _is_docstring(node, parent):
                continue
            if isinstance(node, ast.Constant) and isinstance(parent, ast.JoinedStr):
                continue
            normalized = " ".join(literal.lower().split())
            if not normalized.startswith(FORBIDDEN_SQL_PREFIXES):
                continue
            if _is_reviewed_compiler_return(path, node, parents):
                continue
            kind = "joined-sql" if isinstance(node, ast.JoinedStr) else "sql-literal"
            violations.append(f"{path}:{node.lineno}:{kind}")

    return violations


def test_business_and_database_adapter_code_cannot_use_raw_sql_escape_hatches() -> None:
    assert _raw_sql_violations(SOURCE_ROOT) == []


def _imported_modules(
    source_root: Path,
    path: Path,
    node: ast.Import | ast.ImportFrom,
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if node.level == 0:
        base = node.module or ""
    else:
        relative = path.relative_to(source_root)
        package = ("hermes_cloud", *relative.parent.parts)
        parent_depth = node.level - 1
        if parent_depth >= len(package):
            return ()
        base_parts = package[: len(package) - parent_depth]
        if node.module:
            base_parts = (*base_parts, *node.module.split("."))
        base = ".".join(base_parts)
    imported = [base] if base else []
    imported.extend(
        f"{base}.{alias.name}" if base else alias.name
        for alias in node.names
        if alias.name != "*"
    )
    return tuple(imported)


def _neutral_layer_import_violations(source_root: Path) -> list[str]:
    violations: list[str] = []
    for layer in NEUTRAL_LAYERS:
        for path in (source_root / layer).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                forbidden = next(
                    (
                        module
                        for module in _imported_modules(source_root, path, node)
                        if module.startswith(FORBIDDEN_NEUTRAL_IMPORTS)
                    ),
                    None,
                )
                if forbidden is not None:
                    violations.append(f"{path}:{node.lineno}:{forbidden}")
    return violations


def test_domain_application_and_ports_do_not_import_database_implementations() -> None:
    assert _neutral_layer_import_violations(SOURCE_ROOT) == []


def test_identity_and_projection_repositories_are_nested_postgres_adapters() -> None:
    repository_root = SOURCE_ROOT / "platform" / "postgres" / "repositories"
    assert (repository_root / "identity.py").is_file()
    assert (repository_root / "projection.py").is_file()
    assert not (SOURCE_ROOT / "identity_repository.py").exists()
    assert not (SOURCE_ROOT / "projection_repository.py").exists()


@pytest.mark.parametrize(
    ("layer", "source", "forbidden_module"),
    [
        (
            "application",
            "from ..platform.postgres import catalog\n",
            "hermes_cloud.platform.postgres",
        ),
        (
            "domain",
            "from .. import adapters\n",
            "hermes_cloud.adapters",
        ),
        (
            "ports",
            "from ..platform import postgres\n",
            "hermes_cloud.platform",
        ),
    ],
)
def test_relative_database_imports_are_rejected_in_each_neutral_layer(
    tmp_path: Path,
    layer: str,
    source: str,
    forbidden_module: str,
) -> None:
    source_root = tmp_path / "hermes_cloud"
    violation_path = source_root / layer / "violation.py"
    violation_path.parent.mkdir(parents=True)
    violation_path.write_text(source, encoding="utf-8")

    violations = _neutral_layer_import_violations(source_root)

    assert len(violations) == 1
    assert forbidden_module in violations[0]


@pytest.mark.parametrize(
    ("forbidden_source", "forbidden_name"),
    [
        ("text('SELECT 1')", "text"),
        ("connection.exec_driver_sql('SELECT 1')", "exec_driver_sql"),
    ],
)
def test_typed_ddl_compiler_still_rejects_raw_sql_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_source: str,
    forbidden_name: str,
) -> None:
    compiler_path = tmp_path / "platform" / "postgres" / "ddl.py"
    compiler_path.parent.mkdir(parents=True)
    compiler_path.write_text(
        f"def violation(connection):\n    {forbidden_source}\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "SOURCE_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "TYPED_DDL_COMPILER", compiler_path)

    with pytest.raises(AssertionError, match=forbidden_name):
        test_business_and_database_adapter_code_cannot_use_raw_sql_escape_hatches()


@pytest.mark.parametrize(
    ("source", "forbidden_name"),
    [
        ("query = 'SELECT 1'\n", "sql-literal"),
        ("query = f'SELECT {column}'\n", "joined-sql"),
        ("session.execute_script()\n", "execute_script"),
        ("value = migration.sql\n", "Migration.sql"),
        ("statement = DDL('CREATE TABLE items (id int)')\n", "DDL"),
    ],
)
def test_raw_sql_forms_fail_outside_the_reviewed_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    forbidden_name: str,
) -> None:
    source_path = tmp_path / "application" / "violation.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "SOURCE_ROOT", tmp_path)

    with pytest.raises(AssertionError, match=forbidden_name):
        test_business_and_database_adapter_code_cannot_use_raw_sql_escape_hatches()


def test_reviewed_compiler_may_return_typed_sql_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler_path = tmp_path / "platform" / "postgres" / "ddl.py"
    compiler_path.parent.mkdir(parents=True)
    compiler_path.write_text(
        "def _compile_marker(value):\n    return f'SELECT {value}'\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "SOURCE_ROOT", tmp_path)
    monkeypatch.setitem(globals(), "TYPED_DDL_COMPILER", compiler_path)

    test_business_and_database_adapter_code_cannot_use_raw_sql_escape_hatches()
