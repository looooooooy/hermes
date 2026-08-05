from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENTRYPOINT_NAMES = (
    "business_api",
    "connector_gateway",
    "worker",
    "file_gateway",
)
ENTRYPOINTS_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "hermes_cloud" / "entrypoints"
)
ALLOWED_HERMES_LAYERS = {"adapters", "application", "ports"}


@pytest.mark.parametrize("entrypoint_name", ENTRYPOINT_NAMES)
def test_entrypoint_is_a_package_with_app_and_bootstrap_modules(
    entrypoint_name: str,
) -> None:
    package_root = ENTRYPOINTS_ROOT / entrypoint_name

    assert not package_root.with_suffix(".py").exists()
    assert package_root.is_dir()
    assert (package_root / "__init__.py").is_file()
    assert (package_root / "app.py").is_file()
    assert (package_root / "bootstrap.py").is_file()


def _python_files(
    entrypoints_root: Path = ENTRYPOINTS_ROOT,
) -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(entrypoints_root.rglob("*.py"))
        if path.relative_to(entrypoints_root).parts[0] in ENTRYPOINT_NAMES
    )


def _current_package_parts(
    path: Path,
    entrypoints_root: Path,
) -> tuple[str, ...]:
    relative = path.relative_to(entrypoints_root).with_suffix("")
    return ("hermes_cloud", "entrypoints", *relative.parts[:-1])


def _imported_modules(
    node: ast.Import | ast.ImportFrom,
    *,
    path: Path,
    entrypoints_root: Path,
) -> frozenset[str]:
    if isinstance(node, ast.Import):
        return frozenset(alias.name for alias in node.names)

    if node.level == 0:
        base_parts = tuple((node.module or "").split("."))
    else:
        package_parts = _current_package_parts(path, entrypoints_root)
        keep = max(0, len(package_parts) - (node.level - 1))
        base_parts = package_parts[:keep]
        if node.module is not None:
            base_parts = (*base_parts, *node.module.split("."))

    base = ".".join(part for part in base_parts if part)
    imported = {base} if base else set()
    imported.update(
        f"{base}.{alias.name}" if base else alias.name
        for alias in node.names
        if alias.name != "*"
    )
    return frozenset(imported)


def _dynamic_import_modules(tree: ast.AST) -> tuple[tuple[int, str], ...]:
    importlib_aliases: set[str] = set()
    import_function_aliases = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            importlib_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "importlib"
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "importlib"
        ):
            import_function_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "import_module"
            )

    dynamic_imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        module = node.args[0]
        if not isinstance(module, ast.Constant) or not isinstance(module.value, str):
            continue
        is_import = (
            isinstance(node.func, ast.Name) and node.func.id in import_function_aliases
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in importlib_aliases
        )
        if is_import:
            dynamic_imports.append((node.lineno, module.value))
    return tuple(dynamic_imports)


def _boundary_violation(
    module_name: str,
    *,
    current_entrypoint: str,
) -> str | None:
    entrypoint_prefix = "hermes_cloud.entrypoints."
    if module_name.startswith(entrypoint_prefix):
        imported_entrypoint = module_name.removeprefix(entrypoint_prefix).split(".", 1)[
            0
        ]
        if imported_entrypoint != current_entrypoint:
            return f"imports entrypoint {imported_entrypoint}"
        return None
    if module_name == "hermes_cloud":
        return None
    cloud_prefix = "hermes_cloud."
    if module_name.startswith(cloud_prefix):
        layer = module_name.removeprefix(cloud_prefix).split(".", 1)[0]
        if layer not in ALLOWED_HERMES_LAYERS:
            return f"imports forbidden layer {layer}"
    return None


def _architecture_violations(
    entrypoints_root: Path,
) -> tuple[str, ...]:
    violations: set[str] = set()
    for path in _python_files(entrypoints_root):
        relative = path.relative_to(entrypoints_root)
        current_entrypoint = relative.parts[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            reasons = {
                reason
                for module_name in _imported_modules(
                    node,
                    path=path,
                    entrypoints_root=entrypoints_root,
                )
                if (
                    reason := _boundary_violation(
                        module_name,
                        current_entrypoint=current_entrypoint,
                    )
                )
                is not None
            }
            violations.update(
                f"{relative}:{node.lineno} {reason}" for reason in reasons
            )
        for lineno, module_name in _dynamic_import_modules(tree):
            reason = _boundary_violation(
                module_name,
                current_entrypoint=current_entrypoint,
            )
            if reason is not None:
                violations.add(f"{relative}:{lineno} {reason}")
    return tuple(sorted(violations))


def test_entrypoint_packages_do_not_import_each_other() -> None:
    violations = [
        violation
        for violation in _architecture_violations(ENTRYPOINTS_ROOT)
        if "imports entrypoint " in violation
    ]

    assert violations == []


def test_package_initializers_only_reexport_public_api() -> None:
    violations: list[str] = []

    for entrypoint_name in ENTRYPOINT_NAMES:
        path = ENTRYPOINTS_ROOT / entrypoint_name / "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if isinstance(node, ast.ImportFrom):
                continue
            if isinstance(node, ast.Assign) and all(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                continue
            if isinstance(node, ast.Delete) and all(
                isinstance(target, ast.Name) and target.id in {"app", "bootstrap"}
                for target in node.targets
            ):
                continue
            violations.append(f"{path.relative_to(ENTRYPOINTS_ROOT)}:{node.lineno}")

    assert violations == []


def test_entrypoints_only_depend_on_composition_layers() -> None:
    violations = [
        violation
        for violation in _architecture_violations(ENTRYPOINTS_ROOT)
        if "imports forbidden layer " in violation
    ]

    assert violations == []


def test_nested_triple_relative_cross_entrypoint_mutation_is_rejected(
    tmp_path: Path,
) -> None:
    entrypoints_root = tmp_path / "entrypoints"
    nested = entrypoints_root / "business_api" / "internal" / "handler.py"
    nested.parent.mkdir(parents=True)
    nested.write_text(
        "from ...worker.bootstrap import create_worker as make_worker\n",
        encoding="utf-8",
    )
    scanner = globals().get("_architecture_violations")

    assert scanner is not None
    assert any(
        "imports entrypoint worker" in violation
        for violation in scanner(entrypoints_root)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            (
                "import importlib as loader\n"
                "loader.import_module("
                '"hermes_cloud.entrypoints.worker.bootstrap")\n'
            ),
            "imports entrypoint worker",
        ),
        (
            (
                "from importlib import import_module as load\n"
                'load("hermes_cloud.domain.contract_models")\n'
            ),
            "imports forbidden layer domain",
        ),
        (
            '__import__("hermes_cloud.platform.postgres")\n',
            "imports forbidden layer platform",
        ),
    ],
)
def test_dynamic_import_mutation_is_rejected(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    entrypoints_root = tmp_path / "entrypoints"
    nested = entrypoints_root / "business_api" / "internal" / "handler.py"
    nested.parent.mkdir(parents=True)
    nested.write_text(source, encoding="utf-8")
    scanner = globals().get("_architecture_violations")

    assert scanner is not None
    assert any(expected in violation for violation in scanner(entrypoints_root))
