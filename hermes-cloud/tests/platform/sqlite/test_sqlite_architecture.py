from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic

import pytest

import hermes_cloud

PACKAGE_ROOT = Path(hermes_cloud.__file__).parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
SQLITE_SOURCE_ROOT = PACKAGE_ROOT / "platform" / "sqlite"
NEUTRAL_SQLALCHEMY_ROOT = PACKAGE_ROOT / "platform" / "sqlalchemy"
SQLITE_PRAGMA_POLICY_PATH = SQLITE_SOURCE_ROOT / "engine.py"
SQLITE_PRAGMA_POLICY_FUNCTION = "_configure_sqlite_pragma_policy"
SQLITE_PRAGMA_POLICY_CONSTANT = "_SQLITE_FOREIGN_KEYS_PRAGMA"
SQLITE_PRAGMA_POLICY_STATEMENT = "PRAGMA foreign_keys=ON"
DEPLOY_SQLITE_SCRIPTS = (
    PROJECT_ROOT
    / "deploy"
    / "test_server"
    / "scripts"
    / "cleanup_test_seed_session.py",
    PROJECT_ROOT / "deploy" / "test_server" / "scripts" / "migrate_sqlite.py",
    PROJECT_ROOT / "deploy" / "test_server" / "scripts" / "seed_test_data.py",
)
RAW_SQL_PREFIXES = frozenset(
    {
        "alter",
        "attach",
        "begin",
        "commit",
        "create",
        "delete",
        "detach",
        "drop",
        "explain",
        "grant",
        "insert",
        "pragma",
        "release",
        "replace",
        "revoke",
        "rollback",
        "savepoint",
        "select",
        "set",
        "update",
        "vacuum",
        "with",
    }
)


def _sql_contract_files() -> tuple[Path, ...]:
    return tuple(
        sorted(
            (
                *SQLITE_SOURCE_ROOT.rglob("*.py"),
                *NEUTRAL_SQLALCHEMY_ROOT.rglob("*.py"),
                *DEPLOY_SQLITE_SCRIPTS,
            )
        )
    )


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _enclosing_function(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> str | None:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return None


def _is_pragma_policy_call(
    path: Path,
    node: ast.Call,
    parents: dict[ast.AST, ast.AST],
    *,
    pragma_policy_path: Path,
) -> bool:
    if (
        path != pragma_policy_path
        or _enclosing_function(node, parents) != SQLITE_PRAGMA_POLICY_FUNCTION
    ):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr == "cursor":
        return True
    argument = node.args[0] if node.args else None
    module: ast.AST = node
    while module in parents:
        module = parents[module]
    constant_assignments = (
        tuple(
            statement
            for statement in module.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            and SQLITE_PRAGMA_POLICY_CONSTANT in _assigned_names(statement)
            and statement.value is not None
        )
        if isinstance(module, ast.Module)
        else ()
    )
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and isinstance(argument, ast.Name)
        and argument.id == SQLITE_PRAGMA_POLICY_CONSTANT
        and len(constant_assignments) == 1
        and _literal_string(constant_assignments[0].value)
        == SQLITE_PRAGMA_POLICY_STATEMENT
    )


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _looks_like_raw_sql(value: str) -> bool:
    normalized = " ".join(value.strip().lower().split())
    if not normalized:
        return False
    prefix = normalized.split(maxsplit=1)[0].rstrip(";")
    return prefix in RAW_SQL_PREFIXES


_AST_NODE_BUDGET = 20_000
_AST_DEPTH_BUDGET = 64
_CALL_ANALYSIS_BUDGET = 4_096


@dataclass(frozen=True)
class _AbstractValue:
    kind: str
    text: str | None = None
    literal: int | bool | None = None
    member: str | None = None
    items: tuple[_AbstractValue, ...] = ()
    entries: tuple[tuple[str | int, _AbstractValue], ...] = ()
    risks: frozenset[str] = frozenset()
    reference: int | None = None


@dataclass(frozen=True)
class _LexicalScope:
    kind: str
    environment: dict[str, _AbstractValue]
    bound_names: frozenset[str]


@dataclass
class _DeferredCallable:
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    environment: dict[str, _AbstractValue]
    scope_chain: tuple[_LexicalScope, ...]
    positional_defaults: dict[str, _AbstractValue]
    keyword_defaults: dict[str, _AbstractValue]
    method_kind: str | None = None
    defining_class_reference: int | None = None
    called: bool = False
    active: bool = False


@dataclass(frozen=True)
class _DeferredClass:
    namespace: dict[str, _AbstractValue]
    method_references: tuple[int, ...]
    base_references: tuple[int, ...] = ()


@dataclass(frozen=True)
class _DeferredInstance:
    class_reference: int
    namespace: dict[str, _AbstractValue]


@dataclass(frozen=True)
class _DeferredIterable:
    node: ast.GeneratorExp | None
    environment: dict[str, _AbstractValue]
    scope_name: str
    depth: int
    leading_iterable: _AbstractValue | None = None
    mapper: _AbstractValue | None = None
    source: _AbstractValue | None = None


@dataclass(frozen=True)
class _BlockFlow:
    fallthrough: bool = True
    returns: tuple[_AbstractValue, ...] = ()


@dataclass(frozen=True)
class _CallArguments:
    positional: tuple[_AbstractValue, ...]
    keywords: tuple[tuple[str, _AbstractValue], ...]
    unknown_positional: bool = False
    unknown_keywords: bool = False


@dataclass(frozen=True)
class _ObjectStateSnapshot:
    classes: dict[int, _DeferredClass]
    instances: dict[int, _DeferredInstance]
    next_class_reference: int
    next_instance_reference: int


class _SqlStatementProof(Enum):
    ORM_CORE = "orm-core"
    UNPROVEN = "unproven"


class _SqlReceiverProof(Enum):
    SQLALCHEMY = "sqlalchemy"
    NON_SQL = "non-sql"
    UNPROVEN = "unproven"


_UNKNOWN_VALUE = _AbstractValue("unknown")
_SAFE_VALUE = _AbstractValue("safe")
_SQLITE_MODULE_VALUE = _AbstractValue("sqlite-module")
_SQLITE_CONNECT_VALUE = _AbstractValue("sqlite-connect")
_SQLITE_CONNECTION_VALUE = _AbstractValue("sqlite-connection")
_SQLITE_CURSOR_VALUE = _AbstractValue("sqlite-cursor")
_SQLALCHEMY_MODULE_VALUE = _AbstractValue("sqlalchemy-module", text="sqlalchemy")
_SQLALCHEMY_TEXT_VALUE = _AbstractValue("sqlalchemy-text")
_SQLALCHEMY_TEXTUAL_CALLABLE_VALUE = _AbstractValue("sqlalchemy-textual-callable")
_SQLALCHEMY_TEXTUAL_VALUE = _AbstractValue(
    "sqlalchemy-textual-value",
    risks=frozenset({"raw-sql", "sqlalchemy-text"}),
)
_SQLALCHEMY_DDL_CALLABLE_VALUE = _AbstractValue("sqlalchemy-ddl-callable")
_SQLALCHEMY_DDL_TYPE_VALUE = _AbstractValue("sqlalchemy-ddl-type")
_SQLALCHEMY_DDL_VALUE = _AbstractValue(
    "sqlalchemy-ddl",
    risks=frozenset({"raw-sql", "sqlalchemy-ddl"}),
)
_SQLALCHEMY_INSPECT_CALLABLE_VALUE = _AbstractValue("sqlalchemy-inspect-callable")
_SQLALCHEMY_EVENT_LISTEN_VALUE = _AbstractValue("sqlalchemy-event-listen")
_SQLALCHEMY_EVENT_LISTENS_FOR_VALUE = _AbstractValue("sqlalchemy-event-listens-for")
_SQLALCHEMY_SESSION_TYPE_VALUE = _AbstractValue("sqlalchemy-session-type")
_SQLALCHEMY_CONNECTION_TYPE_VALUE = _AbstractValue("sqlalchemy-connection-type")
_SQLALCHEMY_SESSION_VALUE = _AbstractValue("sqlalchemy-session")
_SQLALCHEMY_CONNECTION_VALUE = _AbstractValue("sqlalchemy-connection")
_SQLALCHEMY_SESSIONMAKER_VALUE = _AbstractValue("sqlalchemy-sessionmaker")
_SQLALCHEMY_SESSION_FACTORY_VALUE = _AbstractValue("sqlalchemy-session-factory")
_SQLALCHEMY_SESSION_CONTEXT_VALUE = _AbstractValue("sqlalchemy-session-context")
_SQLALCHEMY_TRANSPORT_HELPER_VALUE = _AbstractValue(
    "sqlalchemy-transport-helper"
)
_SQLALCHEMY_STATEMENT_TYPE_VALUE = _AbstractValue("sqlalchemy-statement-type")
_SQLALCHEMY_SESSION_OWNER_BASE_VALUE = _AbstractValue("sqlalchemy-session-owner-base")
_IMPORTLIB_MODULE_VALUE = _AbstractValue("importlib-module")
_IMPORT_MODULE_VALUE = _AbstractValue("import-module")
_BUILTIN_IMPORT_VALUE = _AbstractValue("builtin-import")
_BUILTINS_MODULE_VALUE = _AbstractValue("builtins-module")
_FUNCTOOLS_MODULE_VALUE = _AbstractValue("functools-module")
_FUNCTOOLS_PARTIAL_VALUE = _AbstractValue("functools-partial")
_OPERATOR_MODULE_VALUE = _AbstractValue("operator-module")
_OPERATOR_METHODCALLER_VALUE = _AbstractValue("operator-methodcaller")
_OPERATOR_ATTRGETTER_VALUE = _AbstractValue("operator-attrgetter")
_TYPING_MODULE_VALUE = _AbstractValue("typing-module")
_TYPING_ANNOTATED_VALUE = _AbstractValue("typing-annotated")
_TYPING_OPTIONAL_VALUE = _AbstractValue("typing-optional")
_TYPING_UNION_VALUE = _AbstractValue("typing-union")
_GETATTR_CALLABLE_VALUE = _AbstractValue("getattr-callable")
_SAFE_FUNCTION_DECORATOR_VALUE = _AbstractValue("safe-function-decorator")
_PROPERTY_DECORATOR_VALUE = _AbstractValue("property-decorator")
_ORM_STATEMENT_CALLABLE_VALUE = _AbstractValue("orm-statement-callable")
_ORM_EXPRESSION_CALLABLE_VALUE = _AbstractValue("orm-expression-callable")
_ORM_EXPRESSION_NAMESPACE_VALUE = _AbstractValue("orm-expression-namespace")
_ORM_STATEMENT_VALUE = _AbstractValue("orm-statement")
_ORM_EXPRESSION_VALUE = _AbstractValue("orm-expression")
_ORM_RESULT_VALUE = _AbstractValue("orm-result")
_ORM_RESULT_COLLECTION_VALUE = _AbstractValue("orm-result-collection")
_ORM_MODEL_VALUE = _AbstractValue("orm-model")
_ORM_MODEL_MODULE_VALUE = _AbstractValue("orm-model-module")
_ORM_TABLE_VALUE = _AbstractValue("orm-table")
_ORM_COLUMN_VALUE = _AbstractValue("orm-column")
_ORM_COLUMN_NAMESPACE_VALUE = _AbstractValue("orm-column-namespace")
_ORM_METADATA_CALLABLE_VALUE = _AbstractValue("orm-metadata-callable")
_ORM_METADATA_VALUE = _AbstractValue("orm-metadata")
_ORM_TABLE_CALLABLE_VALUE = _AbstractValue("orm-table-callable")
_ORM_COLUMN_CALLABLE_VALUE = _AbstractValue("orm-column-callable")
_ORM_CORE_TYPE_CALLABLE_VALUE = _AbstractValue("orm-core-type-callable")
_ORM_CORE_TYPE_VALUE = _AbstractValue("orm-core-type")
_ORM_BIND_VALUE = _AbstractValue("orm-bind")
_ORM_BIND_MAPPING_VALUE = _AbstractValue("orm-bind-mapping")
_ORM_BIND_TYPE_VALUE = _AbstractValue("orm-bind-type")
_MAP_CALLABLE_VALUE = _AbstractValue("map-callable")
_SEQUENCE_CALLABLE_VALUE = _AbstractValue("sequence-callable")
_UNKNOWN_SQL_VALUE = _AbstractValue(
    "unknown",
    risks=frozenset({"raw-sql"}),
)
_UNKNOWN_SQLALCHEMY_EXPORT_VALUE = _AbstractValue(
    "unknown-sqlalchemy-export",
    risks=frozenset({"raw-sql"}),
)
_FAIL_CLOSED_VALUE = _AbstractValue(
    "unknown",
    risks=frozenset(
        {
            "raw-sql",
            "sqlite-module",
            "sqlite-connect",
            "sqlite-connection",
            "sqlite-cursor",
            "sqlalchemy-text",
        }
    ),
)
_ORM_STATEMENT_CONSTRUCTOR_NAMES = frozenset(
    {
        "delete",
        "insert",
        "select",
        "update",
    }
)
_ORM_EXPRESSION_CONSTRUCTOR_NAMES = frozenset(
    {
        "and_",
        "case",
        "cast",
        "column",
        "exists",
        "literal",
        "not_",
        "or_",
        "table",
        "tuple_",
    }
)
_ORM_STATEMENT_METHODS = frozenset(
    {
        "distinct",
        "execution_options",
        "join",
        "outerjoin",
        "limit",
        "offset",
        "on_conflict_do_nothing",
        "on_conflict_do_update",
        "order_by",
        "select_from",
        "values",
        "where",
        "with_for_update",
    }
)
_SQLALCHEMY_STATEMENT_SINK_METHODS = frozenset(
    {
        "execute",
        "scalar",
        "scalars",
        "stream",
        "stream_scalars",
    }
)
_SQLALCHEMY_RECEIVER_CLASS_MEMBER = "<sqlalchemy-receiver>"
_SQLALCHEMY_DDL_CLASS_MEMBER = "<sqlalchemy-ddl>"
_ORM_MODEL_MODULE = "hermes_cloud.platform.postgres.models"
_ORM_MODEL_MODULES = frozenset(
    {
        _ORM_MODEL_MODULE,
        "hermes_cloud.platform.sqlalchemy.observer_projection_models",
    }
)
_ORM_MODEL_PACKAGE = "hermes_cloud.platform.postgres"
_ORM_MODEL_EXPORT_CACHE: dict[str, frozenset[str]] = {}


def _orm_model_export_proven(module: str | None, name: str) -> bool:
    if module in _ORM_MODEL_MODULES:
        return True
    prefix = "hermes_cloud.platform.sqlalchemy."
    if module is None or not module.startswith(prefix):
        return False
    cached = _ORM_MODEL_EXPORT_CACHE.get(module)
    if cached is None:
        relative = module.removeprefix(prefix).replace(".", "/") + ".py"
        source = (NEUTRAL_SQLALCHEMY_ROOT / relative).resolve()
        if (
            not source.is_relative_to(NEUTRAL_SQLALCHEMY_ROOT.resolve())
            or not source.is_file()
        ):
            cached = frozenset()
        else:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            declarative_bases = {
                alias.asname or alias.name
                for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and statement.module == "sqlalchemy.orm"
                for alias in statement.names
                if alias.name == "DeclarativeBase"
            }
            class_bases = {
                statement.name: {
                    base.id for base in statement.bases if isinstance(base, ast.Name)
                }
                for statement in tree.body
                if isinstance(statement, ast.ClassDef)
            }
            proven = set(declarative_bases)
            while True:
                additions = {
                    class_name
                    for class_name, bases in class_bases.items()
                    if bases & proven
                } - proven
                if not additions:
                    break
                proven.update(additions)
            cached = frozenset(proven - declarative_bases)
        _ORM_MODEL_EXPORT_CACHE[module] = cached
    return name in cached


_SQLALCHEMY_CORE_SCHEMA_IMPORT_MODULES = frozenset(
    {
        "sqlalchemy",
        "sqlalchemy.schema",
        "sqlalchemy.sql.schema",
    }
)
_SQLALCHEMY_CORE_TYPE_IMPORT_MODULES = frozenset(
    {
        "sqlalchemy",
        "sqlalchemy.sql.sqltypes",
        "sqlalchemy.types",
    }
)
_SQLALCHEMY_CORE_TYPE_NAMES = frozenset(
    {
        "BigInteger",
        "Boolean",
        "Date",
        "DateTime",
        "Enum",
        "Float",
        "Integer",
        "JSON",
        "LargeBinary",
        "Numeric",
        "SmallInteger",
        "String",
        "Text",
        "Time",
        "Unicode",
        "UnicodeText",
        "Uuid",
    }
)
_SQLALCHEMY_DDL_IMPORT_MODULES = frozenset(
    {
        "sqlalchemy",
        "sqlalchemy.schema",
        "sqlalchemy.sql.ddl",
    }
)
_SQLALCHEMY_DDL_CONSTRUCTOR_NAMES = frozenset(
    {
        "AddConstraint",
        "CreateIndex",
        "CreateSchema",
        "CreateSequence",
        "CreateTable",
        "DDL",
        "DDLElement",
        "DropColumnComment",
        "DropConstraint",
        "DropConstraintComment",
        "DropIndex",
        "DropSchema",
        "DropSequence",
        "DropTable",
        "DropTableComment",
        "SetColumnComment",
        "SetConstraintComment",
        "SetTableComment",
    }
)
_SQLALCHEMY_STATEMENT_IMPORTS = {
    "sqlalchemy": _ORM_STATEMENT_CONSTRUCTOR_NAMES,
    "sqlalchemy.sql": _ORM_STATEMENT_CONSTRUCTOR_NAMES,
    "sqlalchemy.sql.expression": _ORM_STATEMENT_CONSTRUCTOR_NAMES,
    "sqlalchemy.dialects.sqlite": frozenset({"insert"}),
}
_SQLALCHEMY_EXPRESSION_IMPORT_MODULES = frozenset(
    {
        "sqlalchemy",
        "sqlalchemy.sql",
        "sqlalchemy.sql.expression",
    }
)
_SQLALCHEMY_STATEMENT_TYPE_IMPORTS = {
    "sqlalchemy": frozenset({"Delete", "Insert", "Select", "Update"}),
    "sqlalchemy.sql": frozenset({"Delete", "Insert", "Select", "Update"}),
    "sqlalchemy.sql.expression": frozenset({"Delete", "Insert", "Select", "Update"}),
    "sqlalchemy.sql.dml": frozenset({"Delete", "Insert", "Update"}),
    "sqlalchemy.sql.selectable": frozenset({"Select"}),
}
_SQLALCHEMY_SESSION_TYPE_IMPORTS = frozenset(
    {
        ("sqlalchemy.orm", "Session"),
        ("sqlalchemy.ext.asyncio", "AsyncSession"),
    }
)
_SQLALCHEMY_CONNECTION_TYPE_IMPORTS = frozenset(
    {
        ("sqlalchemy", "Connection"),
        ("sqlalchemy.engine", "Connection"),
        ("sqlalchemy.ext.asyncio", "AsyncConnection"),
    }
)
_SQLALCHEMY_SESSIONMAKER_IMPORTS = frozenset(
    {
        ("sqlalchemy.orm", "sessionmaker"),
        ("sqlalchemy.ext.asyncio", "async_sessionmaker"),
    }
)
_SQLALCHEMY_TEXT_IMPORTS = frozenset(
    {
        ("sqlalchemy", "text"),
        ("sqlalchemy.sql", "text"),
        ("sqlalchemy.sql.expression", "text"),
    }
)
_SQLALCHEMY_TEXTUAL_IMPORTS = frozenset(
    {
        ("sqlalchemy", "literal_column"),
        ("sqlalchemy.sql", "literal_column"),
        ("sqlalchemy.sql.expression", "literal_column"),
    }
)
_SQLALCHEMY_KNOWN_MODULES = frozenset(
    {
        "sqlalchemy",
        "sqlalchemy.dialects",
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.engine",
        "sqlalchemy.event",
        "sqlalchemy.ext",
        "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm",
        "sqlalchemy.schema",
        "sqlalchemy.sql",
        "sqlalchemy.sql.dml",
        "sqlalchemy.sql.ddl",
        "sqlalchemy.sql.expression",
        "sqlalchemy.sql.schema",
        "sqlalchemy.sql.selectable",
        "sqlalchemy.sql.sqltypes",
        "sqlalchemy.types",
    }
)


def _sqlalchemy_module_value(module: str) -> _AbstractValue:
    return _AbstractValue("sqlalchemy-module", text=module)


def _sqlalchemy_export_value(
    module: str,
    name: str,
) -> _AbstractValue | None:
    if module == "sqlalchemy" and name == "inspect":
        return _SQLALCHEMY_INSPECT_CALLABLE_VALUE
    if (module, name) in _SQLALCHEMY_SESSION_TYPE_IMPORTS:
        return _SQLALCHEMY_SESSION_TYPE_VALUE
    if (module, name) in _SQLALCHEMY_CONNECTION_TYPE_IMPORTS:
        return _SQLALCHEMY_CONNECTION_TYPE_VALUE
    if (module, name) in _SQLALCHEMY_SESSIONMAKER_IMPORTS:
        return _SQLALCHEMY_SESSIONMAKER_VALUE
    if name in _SQLALCHEMY_STATEMENT_TYPE_IMPORTS.get(module, frozenset()):
        return _SQLALCHEMY_STATEMENT_TYPE_VALUE
    if module in _SQLALCHEMY_DDL_IMPORT_MODULES:
        if name == "ExecutableDDLElement":
            return _SQLALCHEMY_DDL_TYPE_VALUE
        if name in _SQLALCHEMY_DDL_CONSTRUCTOR_NAMES:
            return _SQLALCHEMY_DDL_CALLABLE_VALUE
    if module == "sqlalchemy.event":
        if name == "listen":
            return _SQLALCHEMY_EVENT_LISTEN_VALUE
        if name == "listens_for":
            return _SQLALCHEMY_EVENT_LISTENS_FOR_VALUE
    if module in _SQLALCHEMY_CORE_SCHEMA_IMPORT_MODULES:
        if name == "MetaData":
            return _ORM_METADATA_CALLABLE_VALUE
        if name == "Table":
            return _ORM_TABLE_CALLABLE_VALUE
        if name == "Column":
            return _ORM_COLUMN_CALLABLE_VALUE
    if (
        module in _SQLALCHEMY_CORE_TYPE_IMPORT_MODULES
        and name in _SQLALCHEMY_CORE_TYPE_NAMES
    ):
        return _ORM_CORE_TYPE_CALLABLE_VALUE
    if name in _SQLALCHEMY_STATEMENT_IMPORTS.get(module, frozenset()):
        return _AbstractValue("orm-statement-callable", member=name)
    if (
        module in _SQLALCHEMY_EXPRESSION_IMPORT_MODULES
        and name in _ORM_EXPRESSION_CONSTRUCTOR_NAMES
    ):
        return _AbstractValue("orm-expression-callable", member=name)
    if (module, name) in _SQLALCHEMY_TEXT_IMPORTS:
        return _SQLALCHEMY_TEXT_VALUE
    if (module, name) in _SQLALCHEMY_TEXTUAL_IMPORTS:
        return _SQLALCHEMY_TEXTUAL_CALLABLE_VALUE
    if module == "sqlalchemy" and name == "func":
        return _ORM_EXPRESSION_NAMESPACE_VALUE
    qualified_name = f"{module}.{name}"
    if qualified_name in _SQLALCHEMY_KNOWN_MODULES:
        return _sqlalchemy_module_value(qualified_name)
    return None


def _string_value(text: str) -> _AbstractValue:
    risks = frozenset({"raw-sql"}) if _looks_like_raw_sql(text) else frozenset()
    return _AbstractValue("string", text=text, risks=risks)


def _value_risks(value: _AbstractValue) -> frozenset[str]:
    kind_risks = {
        "sqlite-module",
        "sqlite-connect",
        "sqlite-connection",
        "sqlite-cursor",
        "sqlalchemy-text",
        "import-module",
        "builtin-import",
    }
    risks = value.risks
    if value.kind in kind_risks:
        risks |= {value.kind}
    if value.kind == "union":
        for item in value.items:
            risks |= _value_risks(item)
    return risks


def _merge_values(
    left: _AbstractValue,
    right: _AbstractValue,
) -> _AbstractValue:
    if left == right:
        return left
    alternatives: list[_AbstractValue] = []
    for value in (left, right):
        candidates = value.items if value.kind == "union" else (value,)
        for candidate in candidates:
            if candidate not in alternatives:
                alternatives.append(candidate)
    return _AbstractValue(
        "union",
        items=tuple(alternatives),
        risks=frozenset(risk for value in alternatives for risk in _value_risks(value)),
    )


def _merge_environments(
    environments: tuple[dict[str, _AbstractValue], ...],
) -> dict[str, _AbstractValue]:
    names = set().union(*(environment.keys() for environment in environments))
    merged: dict[str, _AbstractValue] = {}
    for name in names:
        values = tuple(
            environment.get(name, _UNKNOWN_VALUE) for environment in environments
        )
        value = values[0]
        for candidate in values[1:]:
            value = _merge_values(value, candidate)
        merged[name] = value
    return merged


def _target_names(node: ast.AST) -> tuple[str, ...]:
    stack = [node]
    names: list[str] = []
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Name):
            names.append(current.id)
        elif isinstance(current, (ast.Tuple, ast.List)):
            stack.extend(reversed(current.elts))
    return tuple(names)


def _bounded_node_count(tree: ast.AST) -> int | None:
    count = 0
    stack = [tree]
    while stack:
        current = stack.pop()
        count += 1
        if count > _AST_NODE_BUDGET:
            return None
        stack.extend(ast.iter_child_nodes(current))
    return count


class _ScopedSqlContractAnalyzer:
    def __init__(
        self,
        *,
        path: Path,
        tree: ast.Module,
        pragma_policy_path: Path,
    ) -> None:
        self._path = path
        self._pragma_policy_path = pragma_policy_path
        self._parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        self._violations: list[str] = []
        self._seen_violations: set[tuple[int, str]] = set()
        self._callables: dict[int, _DeferredCallable] = {}
        self._classes: dict[int, _DeferredClass] = {}
        self._instances: dict[int, _DeferredInstance] = {}
        self._iterables: dict[int, _DeferredIterable] = {}
        self._next_callable_reference = 0
        self._next_class_reference = 0
        self._next_instance_reference = 0
        self._next_iterable_reference = 0
        self._call_analysis_count = 0
        self._late_scope_stack: list[_LexicalScope] = []
        self._active_callable_stack: list[_DeferredCallable] = []
        self._owner_redirects: dict[
            int,
            dict[str, dict[str, _AbstractValue]],
        ] = {}

    def analyze(self, tree: ast.Module) -> list[str]:
        bind_constructors = {name: _ORM_BIND_TYPE_VALUE for name in ("int", "str")}
        environment = {
            "__import__": _BUILTIN_IMPORT_VALUE,
            **bind_constructors,
        }
        self._late_scope_stack.append(
            _LexicalScope(
                kind="module",
                environment=environment,
                bound_names=self._scope_bound_names(tree.body)
                | frozenset({"__import__", *bind_constructors}),
            )
        )
        try:
            self._analyze_block(
                tree.body,
                environment,
                scope_name="<module>",
                depth=0,
                finalize_scope=True,
            )
        finally:
            self._late_scope_stack.pop()
        return self._violations

    def _add_violation(self, node: ast.AST, kind: str) -> None:
        line_number = getattr(node, "lineno", 1)
        key = (line_number, kind)
        if key in self._seen_violations:
            return
        self._seen_violations.add(key)
        self._violations.append(f"{self._path}:{line_number}:{kind}")

    def _value_has(self, value: _AbstractValue, kind: str) -> bool:
        return value.kind == kind or kind in _value_risks(value)

    def _statement_proof(self, value: _AbstractValue) -> _SqlStatementProof:
        if value.kind == "orm-statement":
            return _SqlStatementProof.ORM_CORE
        if (
            value.kind == "union"
            and value.items
            and all(
                self._statement_proof(item) is _SqlStatementProof.ORM_CORE
                for item in value.items
            )
        ):
            return _SqlStatementProof.ORM_CORE
        return _SqlStatementProof.UNPROVEN

    def _contains_sql_sink(self, value: _AbstractValue) -> bool:
        if value.kind == "sql-statement-sink-callable":
            return True
        return any(self._contains_sql_sink(item) for item in value.items) or any(
            self._contains_sql_sink(item) for _, item in value.entries
        )

    def _contains_sql_receiver(self, value: _AbstractValue) -> bool:
        if self._sql_receiver_proof(value) is _SqlReceiverProof.SQLALCHEMY:
            return True
        return any(self._contains_sql_receiver(item) for item in value.items) or any(
            self._contains_sql_receiver(item) for _, item in value.entries
        )

    def _is_textual_executable(self, value: _AbstractValue) -> bool:
        if value.kind in {
            "sqlalchemy-ddl",
            "sqlalchemy-ddl-callable",
            "sqlalchemy-ddl-type",
            "sqlalchemy-textual-value",
        }:
            return True
        if self._value_has(value, "sqlalchemy-ddl") or self._value_has(
            value,
            "sqlalchemy-text",
        ):
            return True
        if value.kind == "union":
            return any(self._is_textual_executable(item) for item in value.items)
        if value.kind in {"class", "instance"} and value.reference is not None:
            class_reference = value.reference
            if value.kind == "instance":
                instance = self._instances.get(value.reference)
                if instance is None:
                    return False
                class_reference = instance.class_reference
            return (
                self._class_member(
                    class_reference,
                    _SQLALCHEMY_DDL_CLASS_MEMBER,
                )
                is not None
            )
        return False

    def _is_callable_proof(self, value: _AbstractValue) -> bool:
        return value.kind in {
            "bound-callable",
            "callable",
            "getattr-callable",
            "operator-attrgetter-callable",
            "operator-methodcaller-callable",
            "partial-callable",
            "sql-statement-sink-callable",
        }

    def _sql_clause_proven(self, value: _AbstractValue) -> bool:
        if value.kind in {
            "orm-column",
            "orm-expression",
            "orm-model",
            "orm-statement",
            "orm-table",
        }:
            return True
        if value.kind == "union" and value.items:
            return all(self._sql_clause_proven(item) for item in value.items)
        return False

    def _sql_value_proven(self, value: _AbstractValue) -> bool:
        if self._sql_clause_proven(value):
            return True
        if value.kind in {
            "literal",
            "orm-bind",
            "orm-bind-member",
            "orm-bind-mapping",
            "safe",
            "string",
        }:
            return True
        if value.kind == "sequence":
            return not value.risks and all(
                self._sql_value_proven(item) for item in value.items
            )
        if value.kind == "mapping":
            return not value.risks and all(
                self._sql_value_proven(item) for _, item in value.entries
            )
        if value.kind == "union" and value.items:
            return all(self._sql_value_proven(item) for item in value.items)
        return False

    def _statement_constructor_arguments_proven(
        self,
        constructor: str | None,
        call_arguments: _CallArguments,
    ) -> bool:
        if call_arguments.unknown_positional or call_arguments.unknown_keywords:
            return False
        positional = call_arguments.positional
        if constructor == "select":
            return all(self._sql_clause_proven(value) for value in positional) and all(
                self._sql_value_proven(value) for _, value in call_arguments.keywords
            )
        if constructor in {"delete", "insert", "update"}:
            return (
                len(positional) == 1
                and positional[0].kind in {"orm-model", "orm-table"}
                and not call_arguments.keywords
            )
        return False

    def _statement_method_arguments_proven(
        self,
        method: str | None,
        call_arguments: _CallArguments,
    ) -> bool:
        if call_arguments.unknown_positional or call_arguments.unknown_keywords:
            return False
        positional = call_arguments.positional
        keywords = tuple(value for _, value in call_arguments.keywords)
        if method == "where":
            return (
                bool(positional)
                and all(self._sql_clause_proven(value) for value in positional)
                and all(self._sql_value_proven(value) for value in keywords)
            )
        if method in {"order_by", "select_from"}:
            return all(self._sql_clause_proven(value) for value in positional) and all(
                self._sql_value_proven(value) for value in keywords
            )
        if method == "join":
            return (
                bool(positional)
                and positional[0].kind in {"orm-model", "orm-table"}
                and all(self._sql_clause_proven(value) for value in positional[1:])
                and all(self._sql_value_proven(value) for value in keywords)
            )
        return all(self._sql_value_proven(value) for value in (*positional, *keywords))

    def _expression_arguments_proven(
        self,
        constructor: str | None,
        call_arguments: _CallArguments,
    ) -> bool:
        if call_arguments.unknown_positional or call_arguments.unknown_keywords:
            return False
        positional = call_arguments.positional
        keyword_values = tuple(value for _, value in call_arguments.keywords)
        if constructor == "column":
            is_literal = next(
                (
                    value
                    for name, value in call_arguments.keywords
                    if name == "is_literal"
                ),
                None,
            )
            return (
                bool(positional)
                and positional[0].kind == "string"
                and (is_literal is None or self._truthiness(is_literal) is False)
                and all(
                    self._sql_value_proven(value)
                    for value in (*positional[1:], *keyword_values)
                )
            )
        if constructor == "table":
            return (
                bool(positional)
                and positional[0].kind == "string"
                and all(value.kind == "orm-column" for value in positional[1:])
                and all(self._sql_value_proven(value) for value in keyword_values)
            )
        if constructor in {"and_", "not_", "or_"}:
            return bool(positional) and all(
                self._sql_clause_proven(value) for value in positional
            )
        if constructor == "literal":
            return all(
                self._sql_value_proven(value)
                for value in (*positional, *keyword_values)
            )
        if constructor == "func":
            return all(
                self._sql_value_proven(value)
                for value in (*positional, *keyword_values)
            )
        return all(
            self._sql_clause_proven(value) or self._sql_value_proven(value)
            for value in (*positional, *keyword_values)
        )

    def _metadata_arguments_proven(
        self,
        call_arguments: _CallArguments,
    ) -> bool:
        return (
            not call_arguments.unknown_positional
            and not call_arguments.unknown_keywords
            and all(
                self._sql_value_proven(value)
                for value in (
                    *call_arguments.positional,
                    *(value for _, value in call_arguments.keywords),
                )
            )
        )

    def _column_arguments_proven(
        self,
        call_arguments: _CallArguments,
    ) -> bool:
        if call_arguments.unknown_positional or call_arguments.unknown_keywords:
            return False
        positional = list(call_arguments.positional)
        keywords = dict(call_arguments.keywords)
        name = keywords.pop("name", None)
        column_type = keywords.pop("type_", None)
        if positional:
            first = positional.pop(0)
            if first.kind == "string":
                if name is not None:
                    return False
                name = first
                if positional:
                    if column_type is not None:
                        return False
                    column_type = positional.pop(0)
            elif first.kind in {"orm-core-type", "orm-core-type-callable"}:
                if column_type is not None:
                    return False
                column_type = first
            else:
                return False
        return (
            name is not None
            and name.kind == "string"
            and not name.risks
            and column_type is not None
            and column_type.kind in {"orm-core-type", "orm-core-type-callable"}
            and all(self._sql_value_proven(value) for value in positional)
            and all(self._sql_value_proven(value) for value in keywords.values())
        )

    def _table_arguments_proven(
        self,
        call_arguments: _CallArguments,
    ) -> bool:
        positional = call_arguments.positional
        return (
            not call_arguments.unknown_positional
            and not call_arguments.unknown_keywords
            and len(positional) >= 2
            and positional[0].kind == "string"
            and not positional[0].risks
            and positional[1].kind == "orm-metadata"
            and all(value.kind == "orm-column" for value in positional[2:])
            and all(
                self._sql_value_proven(value) for _, value in call_arguments.keywords
            )
        )

    def _truthiness(self, value: _AbstractValue) -> bool | None:
        if value.kind == "union":
            outcomes = {self._truthiness(item) for item in value.items}
            return outcomes.pop() if len(outcomes) == 1 else None
        if value.kind == "literal":
            return bool(value.literal)
        if value.kind == "string":
            return bool(value.text)
        if value.kind == "sequence" and not value.risks:
            return bool(value.items)
        if value.kind == "mapping" and not value.risks:
            return bool(value.entries)
        return None

    def _literal_key(self, value: _AbstractValue) -> str | int | None:
        if value.kind == "union":
            keys = {self._literal_key(item) for item in value.items}
            return keys.pop() if len(keys) == 1 else None
        if value.kind == "string":
            return value.text
        if value.kind == "literal" and isinstance(value.literal, (int, bool)):
            return value.literal
        return None

    def _values_equal(
        self,
        left: _AbstractValue,
        right: _AbstractValue,
    ) -> bool | None:
        if left.kind == right.kind == "string":
            return left.text == right.text
        if left.kind == right.kind == "literal":
            return left.literal == right.literal
        if left.kind == right.kind == "sequence":
            if len(left.items) != len(right.items):
                return False
            comparisons = tuple(
                self._values_equal(left_item, right_item)
                for left_item, right_item in zip(
                    left.items,
                    right.items,
                    strict=True,
                )
            )
            if all(comparison is True for comparison in comparisons):
                return True
            if any(comparison is False for comparison in comparisons):
                return False
        if left.kind == "union":
            outcomes = {self._values_equal(item, right) for item in left.items}
            return outcomes.pop() if len(outcomes) == 1 else None
        if right.kind == "union":
            outcomes = {self._values_equal(left, item) for item in right.items}
            return outcomes.pop() if len(outcomes) == 1 else None
        return None

    def _new_instance(self, class_reference: int) -> _AbstractValue:
        reference = self._next_instance_reference
        self._next_instance_reference += 1
        self._instances[reference] = _DeferredInstance(
            class_reference=class_reference,
            namespace={},
        )
        return _AbstractValue("instance", reference=reference)

    def _class_mro(
        self,
        class_reference: int,
        visited: frozenset[int] = frozenset(),
    ) -> tuple[int, ...]:
        if class_reference in visited:
            return (class_reference,)
        class_record = self._classes.get(class_reference)
        if class_record is None:
            return (class_reference,)
        if not class_record.base_references:
            return (class_reference,)
        next_visited = visited | {class_reference}
        sequences = [
            list(self._class_mro(base_reference, next_visited))
            for base_reference in class_record.base_references
        ]
        sequences.append(list(class_record.base_references))
        linearized = [class_reference]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if all(sequence[0] not in other[1:] for other in sequences)
                ),
                None,
            )
            if candidate is None:
                for sequence in sequences:
                    linearized.extend(
                        reference
                        for reference in sequence
                        if reference not in linearized
                    )
                break
            linearized.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        return tuple(linearized)

    def _class_member(
        self,
        class_reference: int,
        attribute: str,
    ) -> _AbstractValue | None:
        for reference in self._class_mro(class_reference):
            record = self._classes.get(reference)
            if record is not None and attribute in record.namespace:
                return record.namespace[attribute]
        return None

    def _snapshot_object_state(self) -> _ObjectStateSnapshot:
        return _ObjectStateSnapshot(
            classes={
                reference: _DeferredClass(
                    namespace=record.namespace.copy(),
                    method_references=record.method_references,
                    base_references=record.base_references,
                )
                for reference, record in self._classes.items()
            },
            instances={
                reference: _DeferredInstance(
                    class_reference=record.class_reference,
                    namespace=record.namespace.copy(),
                )
                for reference, record in self._instances.items()
            },
            next_class_reference=self._next_class_reference,
            next_instance_reference=self._next_instance_reference,
        )

    def _restore_object_state(self, snapshot: _ObjectStateSnapshot) -> None:
        self._classes = {
            reference: _DeferredClass(
                namespace=record.namespace.copy(),
                method_references=record.method_references,
                base_references=record.base_references,
            )
            for reference, record in snapshot.classes.items()
        }
        self._instances = {
            reference: _DeferredInstance(
                class_reference=record.class_reference,
                namespace=record.namespace.copy(),
            )
            for reference, record in snapshot.instances.items()
        }
        self._next_class_reference = max(
            self._next_class_reference,
            snapshot.next_class_reference,
        )
        self._next_instance_reference = max(
            self._next_instance_reference,
            snapshot.next_instance_reference,
        )

    def _merge_object_states(
        self,
        snapshots: tuple[_ObjectStateSnapshot, ...],
    ) -> _ObjectStateSnapshot:
        class_references = set().union(
            *(snapshot.classes.keys() for snapshot in snapshots)
        )
        instance_references = set().union(
            *(snapshot.instances.keys() for snapshot in snapshots)
        )
        classes: dict[int, _DeferredClass] = {}
        for reference in class_references:
            records = tuple(snapshot.classes.get(reference) for snapshot in snapshots)
            template = next(record for record in records if record is not None)
            names = set().union(
                *(record.namespace.keys() for record in records if record is not None)
            )
            namespace: dict[str, _AbstractValue] = {}
            for name in names:
                values = tuple(
                    record.namespace.get(name, _UNKNOWN_VALUE)
                    if record is not None
                    else _UNKNOWN_VALUE
                    for record in records
                )
                value = values[0]
                for candidate in values[1:]:
                    value = _merge_values(value, candidate)
                namespace[name] = value
            classes[reference] = _DeferredClass(
                namespace=namespace,
                method_references=template.method_references,
                base_references=template.base_references,
            )
        instances: dict[int, _DeferredInstance] = {}
        for reference in instance_references:
            records = tuple(snapshot.instances.get(reference) for snapshot in snapshots)
            template = next(record for record in records if record is not None)
            names = set().union(
                *(record.namespace.keys() for record in records if record is not None)
            )
            namespace: dict[str, _AbstractValue] = {}
            for name in names:
                values = tuple(
                    record.namespace.get(name, _UNKNOWN_VALUE)
                    if record is not None
                    else _UNKNOWN_VALUE
                    for record in records
                )
                value = values[0]
                for candidate in values[1:]:
                    value = _merge_values(value, candidate)
                namespace[name] = value
            instances[reference] = _DeferredInstance(
                class_reference=template.class_reference,
                namespace=namespace,
            )
        return _ObjectStateSnapshot(
            classes=classes,
            instances=instances,
            next_class_reference=max(
                snapshot.next_class_reference for snapshot in snapshots
            ),
            next_instance_reference=max(
                snapshot.next_instance_reference for snapshot in snapshots
            ),
        )

    def _read_name(
        self,
        environment: dict[str, _AbstractValue],
        name: str,
    ) -> _AbstractValue:
        owner = self._owner_redirects.get(id(environment), {}).get(name)
        if owner is not None:
            return owner.get(name, _UNKNOWN_VALUE)
        return environment.get(name, _UNKNOWN_VALUE)

    def _write_name(
        self,
        environment: dict[str, _AbstractValue],
        name: str,
        value: _AbstractValue,
    ) -> None:
        owner = self._owner_redirects.get(id(environment), {}).get(name)
        (environment if owner is None else owner)[name] = value

    def _clone_environment(
        self,
        environment: dict[str, _AbstractValue],
    ) -> dict[str, _AbstractValue]:
        cloned = environment.copy()
        redirects = self._owner_redirects.get(id(environment))
        if redirects:
            self._owner_redirects[id(cloned)] = redirects.copy()
        return cloned

    def _tracked_environments(
        self,
        environment: dict[str, _AbstractValue],
    ) -> tuple[dict[str, _AbstractValue], ...]:
        tracked: list[dict[str, _AbstractValue]] = []
        for candidate in (
            environment,
            *(scope.environment for scope in self._late_scope_stack),
        ):
            if all(candidate is not existing for existing in tracked):
                tracked.append(candidate)
        return tuple(tracked)

    def _snapshot_environments(
        self,
        environments: tuple[dict[str, _AbstractValue], ...],
    ) -> tuple[dict[str, _AbstractValue], ...]:
        return tuple(environment.copy() for environment in environments)

    def _restore_environments(
        self,
        environments: tuple[dict[str, _AbstractValue], ...],
        snapshot: tuple[dict[str, _AbstractValue], ...],
    ) -> None:
        for environment, values in zip(environments, snapshot, strict=True):
            environment.clear()
            environment.update(values)

    def _merge_environment_snapshots(
        self,
        environments: tuple[dict[str, _AbstractValue], ...],
        snapshots: tuple[tuple[dict[str, _AbstractValue], ...], ...],
    ) -> None:
        for index, environment in enumerate(environments):
            environment.clear()
            environment.update(
                _merge_environments(tuple(snapshot[index] for snapshot in snapshots))
            )

    def _scope_declarations(
        self,
        statements: list[ast.stmt],
    ) -> tuple[frozenset[str], frozenset[str]]:
        global_names: set[str] = set()
        nonlocal_names: set[str] = set()
        stack: list[ast.AST] = list(reversed(statements))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Global):
                global_names.update(node.names)
            elif isinstance(node, ast.Nonlocal):
                nonlocal_names.update(node.names)
            stack.extend(ast.iter_child_nodes(node))
        return frozenset(global_names), frozenset(nonlocal_names)

    def _scope_bound_names(
        self,
        statements: list[ast.stmt],
    ) -> frozenset[str]:
        names: set[str] = set()
        stack: list[ast.AST] = list(reversed(statements))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                for target in targets:
                    names.update(_target_names(target))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                names.update(_target_names(node.target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        names.update(_target_names(item.optional_vars))
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                names.add(node.name)
            elif isinstance(node, ast.Import):
                names.update(
                    alias.asname or alias.name.split(".", 1)[0] for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom):
                names.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.NamedExpr):
                names.update(_target_names(node.target))
            stack.extend(ast.iter_child_nodes(node))
        return frozenset(names)

    def _parameter_names(self, arguments: ast.arguments) -> frozenset[str]:
        return (
            frozenset(
                argument.arg
                for argument in (
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                )
            )
            | (
                frozenset({arguments.vararg.arg})
                if arguments.vararg is not None
                else frozenset()
            )
            | (
                frozenset({arguments.kwarg.arg})
                if arguments.kwarg is not None
                else frozenset()
            )
        )

    def _annotation_value(
        self,
        annotation: ast.AST | None,
        environment: dict[str, _AbstractValue],
    ) -> _AbstractValue:
        if annotation is None:
            return _UNKNOWN_VALUE
        if isinstance(annotation, ast.Constant) and annotation.value is None:
            return _AbstractValue("literal", literal=None)
        if isinstance(annotation, ast.Name):
            value = self._read_name(environment, annotation.id)
            if (
                value == _UNKNOWN_VALUE
                and annotation.id
                in {
                    "bool",
                    "bytes",
                    "date",
                    "datetime",
                    "float",
                    "int",
                    "object",
                    "str",
                    "timedelta",
                    "UUID",
                }
                and annotation.id not in environment
            ):
                value = _ORM_BIND_TYPE_VALUE
        elif isinstance(annotation, ast.Attribute):
            value = self._attribute_value(
                self._annotation_value(annotation.value, environment),
                annotation.attr,
            )
        elif isinstance(annotation, ast.Subscript):
            container = self._annotation_value(annotation.value, environment)
            if container.kind == "sqlalchemy-session-factory":
                return container
            arguments = (
                tuple(annotation.slice.elts)
                if isinstance(annotation.slice, ast.Tuple)
                else (annotation.slice,)
            )
            if container.kind == "typing-annotated":
                return self._annotation_value(arguments[0], environment)
            if container.kind == "typing-optional":
                return _merge_values(
                    self._annotation_value(arguments[0], environment),
                    _AbstractValue("literal", literal=None),
                )
            if container.kind == "typing-union":
                values = tuple(
                    self._annotation_value(argument, environment)
                    for argument in arguments
                )
                value = values[0]
                for candidate in values[1:]:
                    value = _merge_values(value, candidate)
                return value
            return self._annotation_value(arguments[0], environment)
        elif isinstance(annotation, ast.Tuple):
            values = tuple(
                self._annotation_value(item, environment) for item in annotation.elts
            )
            value = values[0]
            for candidate in values[1:]:
                value = _merge_values(value, candidate)
        elif isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            value = _merge_values(
                self._annotation_value(annotation.left, environment),
                self._annotation_value(annotation.right, environment),
            )
        else:
            return _UNKNOWN_VALUE
        if value.kind == "sqlalchemy-session-type":
            return _SQLALCHEMY_SESSION_VALUE
        if value.kind == "sqlalchemy-connection-type":
            return _SQLALCHEMY_CONNECTION_VALUE
        if value.kind == "sqlalchemy-sessionmaker":
            return _SQLALCHEMY_SESSION_FACTORY_VALUE
        if value.kind == "orm-bind-type":
            return _ORM_BIND_VALUE
        if value.kind == "orm-model":
            return _ORM_RESULT_VALUE
        if value.kind == "unknown":
            return _ORM_BIND_VALUE
        if value.kind == "class" and value.member == "sqlalchemy-session-factory-type":
            return _SQLALCHEMY_SESSION_FACTORY_VALUE
        return value

    def _bind_target(
        self,
        target: ast.AST,
        value: _AbstractValue,
        environment: dict[str, _AbstractValue],
    ) -> None:
        if (
            target is not None
            and value.kind == "union"
            and isinstance(
                target,
                (ast.Tuple, ast.List),
            )
        ):
            for child in target.elts:
                self._bind_target(child, _UNKNOWN_SQL_VALUE, environment)
            return
        if isinstance(target, ast.Name):
            self._write_name(environment, target.id, value)
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            values = (
                value.items
                if value.kind == "sequence" and len(value.items) == len(target.elts)
                else tuple(_UNKNOWN_VALUE for _ in target.elts)
            )
            for child, child_value in zip(target.elts, values, strict=True):
                self._bind_target(child, child_value, environment)
            return
        if isinstance(target, ast.Attribute):
            owner = self._evaluate(
                target.value,
                environment,
                scope_name="<assignment>",
                depth=0,
            )
            if owner.kind == "union":
                for candidate in owner.items:
                    self._bind_attribute(candidate, target.attr, value)
                return
            self._bind_attribute(owner, target.attr, value)

    def _bind_attribute(
        self,
        owner: _AbstractValue,
        attribute: str,
        value: _AbstractValue,
    ) -> None:
        if owner.kind == "class" and owner.reference is not None:
            record = self._classes.get(owner.reference)
            if record is not None:
                record.namespace[attribute] = value
        elif owner.kind == "instance" and owner.reference is not None:
            record = self._instances.get(owner.reference)
            if record is not None:
                record.namespace[attribute] = value

    def _analyze_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        environment: dict[str, _AbstractValue],
        *,
        depth: int,
        method_kind: str | None = None,
    ) -> None:
        decorators = tuple(
            self._evaluate(
                expression,
                environment,
                scope_name=node.name,
                depth=depth,
            )
            for expression in node.decorator_list
        )
        positional_defaults, keyword_defaults = self._capture_defaults(
            node.args,
            environment,
            scope_name=node.name,
            depth=depth,
        )
        function = self._deferred_callable_value(
            node,
            positional_defaults=positional_defaults,
            keyword_defaults=keyword_defaults,
            method_kind=method_kind,
        )
        for decorator in reversed(decorators):
            if decorator.kind in {
                "safe-function-decorator",
                "sqlalchemy-event-listener-decorator",
            }:
                continue
            if decorator.kind == "property-decorator" and function.kind == "callable":
                function = _AbstractValue(
                    "property-descriptor",
                    items=(function,),
                )
                continue
            if (
                decorator.kind == "property-mutator-decorator"
                and decorator.items
                and function.kind == "callable"
            ):
                descriptor = decorator.items[0]
                slots = [
                    *descriptor.items,
                    *(_UNKNOWN_VALUE for _ in range(3 - len(descriptor.items))),
                ][:3]
                slot = {
                    "getter": 0,
                    "setter": 1,
                    "deleter": 2,
                }.get(decorator.member)
                if slot is not None:
                    slots[slot] = function
                    function = _AbstractValue(
                        "property-descriptor",
                        items=tuple(slots),
                    )
                    continue
            function = _UNKNOWN_SQL_VALUE
            break
        self._write_name(
            environment,
            node.name,
            function,
        )

    def _capture_defaults(
        self,
        arguments: ast.arguments,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> tuple[dict[str, _AbstractValue], dict[str, _AbstractValue]]:
        positional_parameters = (*arguments.posonlyargs, *arguments.args)
        positional_names = (
            positional_parameters[-len(arguments.defaults) :]
            if arguments.defaults
            else ()
        )
        positional_defaults = {
            parameter.arg: self._evaluate(
                default,
                environment,
                scope_name=scope_name,
                depth=depth,
            )
            for parameter, default in zip(
                positional_names,
                arguments.defaults,
                strict=True,
            )
        }
        keyword_defaults = {
            parameter.arg: self._evaluate(
                default,
                environment,
                scope_name=scope_name,
                depth=depth,
            )
            for parameter, default in zip(
                arguments.kwonlyargs,
                arguments.kw_defaults,
                strict=True,
            )
            if default is not None
        }
        return positional_defaults, keyword_defaults

    def _deferred_callable_value(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        *,
        positional_defaults: dict[str, _AbstractValue],
        keyword_defaults: dict[str, _AbstractValue],
        method_kind: str | None = None,
    ) -> _AbstractValue:
        reference = self._next_callable_reference
        self._next_callable_reference += 1
        lexical_scopes = tuple(
            scope for scope in self._late_scope_stack if scope.kind != "class"
        )
        self._callables[reference] = _DeferredCallable(
            node=node,
            environment=lexical_scopes[-1].environment,
            scope_chain=lexical_scopes,
            positional_defaults=positional_defaults,
            keyword_defaults=keyword_defaults,
            method_kind=method_kind,
        )
        return _AbstractValue("callable", reference=reference)

    def _bind_callable_arguments(
        self,
        record: _DeferredCallable,
        arguments: ast.arguments,
        call_arguments: _CallArguments,
        environment: dict[str, _AbstractValue],
    ) -> None:
        environment.update(record.positional_defaults)
        environment.update(record.keyword_defaults)
        positional_parameters = (*arguments.posonlyargs, *arguments.args)
        keyword_parameters = {
            parameter.arg for parameter in (*arguments.args, *arguments.kwonlyargs)
        }
        positional_only = {parameter.arg for parameter in arguments.posonlyargs}
        explicitly_bound: set[str] = set()

        for parameter, value in zip(
            positional_parameters,
            call_arguments.positional,
        ):
            current = environment.get(parameter.arg, _UNKNOWN_VALUE)
            environment[parameter.arg] = (
                current
                if current.kind
                in {
                    "sqlalchemy-connection",
                    "sqlalchemy-session",
                    "sqlalchemy-session-factory",
                }
                and value.kind == "unknown"
                else value
            )
            explicitly_bound.add(parameter.arg)

        extra_positional = call_arguments.positional[len(positional_parameters) :]
        if arguments.vararg is not None:
            environment[arguments.vararg.arg] = _AbstractValue(
                "sequence",
                items=extra_positional,
                risks=(
                    _FAIL_CLOSED_VALUE.risks
                    if call_arguments.unknown_positional
                    else frozenset()
                ),
            )
        if call_arguments.unknown_positional:
            for parameter in positional_parameters:
                current = environment.get(parameter.arg, _UNKNOWN_VALUE)
                environment[parameter.arg] = _merge_values(
                    current,
                    _FAIL_CLOSED_VALUE,
                )

        extra_keywords: dict[str, _AbstractValue] = {}
        for name, value in call_arguments.keywords:
            if name in keyword_parameters and name not in positional_only:
                if name in explicitly_bound:
                    environment[name] = _merge_values(environment[name], value)
                else:
                    current = environment.get(name, _UNKNOWN_VALUE)
                    environment[name] = (
                        current
                        if current.kind
                        in {
                            "sqlalchemy-connection",
                            "sqlalchemy-session",
                            "sqlalchemy-session-factory",
                        }
                        and value.kind == "unknown"
                        else value
                    )
                    explicitly_bound.add(name)
            else:
                previous = extra_keywords.get(name)
                extra_keywords[name] = (
                    value if previous is None else _merge_values(previous, value)
                )

        if call_arguments.unknown_keywords:
            for parameter in (*arguments.args, *arguments.kwonlyargs):
                current = environment.get(parameter.arg, _UNKNOWN_VALUE)
                environment[parameter.arg] = _merge_values(
                    current,
                    _FAIL_CLOSED_VALUE,
                )
        if arguments.kwarg is not None:
            environment[arguments.kwarg.arg] = _AbstractValue(
                "mapping",
                entries=tuple(extra_keywords.items()),
                risks=(
                    _FAIL_CLOSED_VALUE.risks
                    if call_arguments.unknown_keywords
                    else frozenset()
                ),
            )
        for parameter in (*positional_parameters, *arguments.kwonlyargs):
            if environment.get(parameter.arg, _UNKNOWN_VALUE) == _UNKNOWN_VALUE:
                environment[parameter.arg] = _UNKNOWN_SQL_VALUE

    def _internal_sql_parameter_contract(
        self,
        record: _DeferredCallable,
    ) -> dict[str, _AbstractValue]:
        if not isinstance(record.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return {}
        if self._path == DEPLOY_SQLITE_SCRIPTS[1] and record.node.name in {
            "_ensure_model",
            "_one_or_none",
        }:
            return {
                "model": _ORM_MODEL_VALUE,
                "identity": _ORM_EXPRESSION_VALUE,
                **(
                    {"values": _ORM_BIND_MAPPING_VALUE}
                    if record.node.name == "_ensure_model"
                    else {}
                ),
            }
        if self._path == SQLITE_SOURCE_ROOT / "repositories" / "projection.py":
            if record.node.name == "_read_unique_model":
                return {"statement": _ORM_STATEMENT_VALUE}
            if record.node.name == "_insert_or_compare":
                return {
                    "model": _ORM_MODEL_VALUE,
                    "values": _ORM_BIND_MAPPING_VALUE,
                    "identity": _AbstractValue(
                        "sequence",
                        items=(_ORM_EXPRESSION_VALUE,),
                    ),
                }
        return {}

    def _analyze_deferred_callable(
        self,
        record: _DeferredCallable,
        *,
        call_arguments: _CallArguments,
        trigger: ast.AST,
        depth: int,
        commit_side_effects: bool,
    ) -> _AbstractValue:
        record.called = True
        if record.active:
            self._add_violation(trigger, "analysis-budget")
            return _FAIL_CLOSED_VALUE
        self._call_analysis_count += 1
        if (
            self._call_analysis_count > _CALL_ANALYSIS_BUDGET
            or depth > _AST_DEPTH_BUDGET
        ):
            self._add_violation(trigger, "analysis-budget")
            return _FAIL_CLOSED_VALUE
        object_snapshot = None if commit_side_effects else self._snapshot_object_state()
        if commit_side_effects:
            scope_chain = record.scope_chain
            defining_environment = record.environment
        else:
            cloned_environments = {
                id(scope.environment): scope.environment.copy()
                for scope in record.scope_chain
            }
            for scope in record.scope_chain:
                redirects = self._owner_redirects.get(id(scope.environment), {})
                if redirects:
                    cloned = cloned_environments[id(scope.environment)]
                    self._owner_redirects[id(cloned)] = {
                        name: cloned_environments.get(id(target), target.copy())
                        for name, target in redirects.items()
                    }
            scope_chain = tuple(
                _LexicalScope(
                    kind=scope.kind,
                    environment=cloned_environments[id(scope.environment)],
                    bound_names=scope.bound_names,
                )
                for scope in record.scope_chain
            )
            defining_environment = cloned_environments[id(record.environment)]
        child_environment = defining_environment.copy()
        if isinstance(record.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            global_names, nonlocal_names = self._scope_declarations(record.node.body)
            bound_names = self._scope_bound_names(
                record.node.body
            ) | self._parameter_names(record.node.args)
            child_environment.update(
                {
                    name: _UNKNOWN_VALUE
                    for name in bound_names - global_names - nonlocal_names
                }
            )
            redirects = {name: scope_chain[0].environment for name in global_names}
            for name in nonlocal_names:
                owner = next(
                    (
                        candidate.environment
                        for candidate in reversed(scope_chain[1:])
                        if candidate.kind == "function"
                        and name in candidate.bound_names
                    ),
                    scope_chain[-1].environment,
                )
                redirects[name] = owner
            if redirects:
                self._owner_redirects[id(child_environment)] = redirects
            for parameter in (
                *record.node.args.posonlyargs,
                *record.node.args.args,
                *record.node.args.kwonlyargs,
            ):
                annotation_value = self._annotation_value(
                    parameter.annotation,
                    defining_environment,
                )
                if annotation_value != _UNKNOWN_VALUE:
                    child_environment[parameter.arg] = annotation_value
            child_environment.update(self._internal_sql_parameter_contract(record))
            self._bind_callable_arguments(
                record,
                record.node.args,
                call_arguments,
                child_environment,
            )
            body = record.node.body
            scope_name = record.node.name
        else:
            bound_names = self._parameter_names(record.node.args)
            child_environment.update({name: _UNKNOWN_VALUE for name in bound_names})
            self._bind_callable_arguments(
                record,
                record.node.args,
                call_arguments,
                child_environment,
            )
            body = None
            scope_name = "<lambda>"

        record.active = True
        previous_scope_chain = self._late_scope_stack
        child_scope = _LexicalScope(
            kind="function",
            environment=child_environment,
            bound_names=bound_names,
        )
        self._late_scope_stack = [*scope_chain, child_scope]
        self._active_callable_stack.append(record)
        try:
            if body is None:
                return_value = self._evaluate(
                    record.node.body,
                    child_environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                self._finalize_callables(child_environment, depth=depth + 1)
            else:
                flow = self._analyze_block(
                    body,
                    child_environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                    finalize_scope=True,
                )
                return_values = list(flow.returns)
                if flow.fallthrough:
                    return_values.append(_SAFE_VALUE)
                return_value = (
                    self._merge_return_values(tuple(return_values))
                    if return_values
                    else _SAFE_VALUE
                )
        finally:
            self._active_callable_stack.pop()
            self._late_scope_stack = previous_scope_chain
            record.active = False
            if object_snapshot is not None:
                self._restore_object_state(object_snapshot)
        return return_value

    def _finalize_callables(
        self,
        environment: dict[str, _AbstractValue],
        *,
        depth: int,
    ) -> None:
        for record in tuple(self._callables.values()):
            if record.environment is environment and not record.called:
                self._analyze_deferred_callable(
                    record,
                    call_arguments=_CallArguments((), ()),
                    trigger=record.node,
                    depth=depth,
                    commit_side_effects=False,
                )

    def _method_kind(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> str:
        for decorator in node.decorator_list:
            name = (
                decorator.id
                if isinstance(decorator, ast.Name)
                else (decorator.attr if isinstance(decorator, ast.Attribute) else None)
            )
            if name in {"staticmethod", "classmethod"}:
                return name
        return "instance"

    def _analyze_class(
        self,
        node: ast.ClassDef,
        environment: dict[str, _AbstractValue],
        *,
        depth: int,
    ) -> None:
        for expression in node.decorator_list:
            self._evaluate(expression, environment, scope_name=node.name, depth=depth)
        base_values = tuple(
            self._evaluate(
                expression,
                environment,
                scope_name=node.name,
                depth=depth,
            )
            for expression in node.bases
        )
        for keyword in node.keywords:
            self._evaluate(
                keyword.value,
                environment,
                scope_name=node.name,
                depth=depth,
            )
        child_environment = self._clone_environment(environment)
        local_names = self._scope_bound_names(node.body)
        class_scope = _LexicalScope(
            kind="class",
            environment=child_environment,
            bound_names=local_names,
        )
        previous_scope_chain = self._late_scope_stack
        references_before = set(self._callables)
        self._late_scope_stack = [*previous_scope_chain, class_scope]
        try:
            self._analyze_block(
                node.body,
                child_environment,
                scope_name=node.name,
                depth=depth,
                class_body=True,
            )
        finally:
            self._late_scope_stack = previous_scope_chain
        method_references = tuple(
            reference
            for reference in self._callables
            if reference not in references_before
            and self._callables[reference].method_kind is not None
        )
        reference = self._next_class_reference
        self._next_class_reference += 1
        session_factory_type = any(
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name in {"__call__", "begin"}
            and self._annotation_value(
                statement.returns,
                child_environment,
            ).kind
            == "sqlalchemy-session"
            for statement in node.body
        )
        class_value = _AbstractValue(
            "class",
            member=(
                "sqlalchemy-session-factory-type" if session_factory_type else None
            ),
            reference=reference,
        )
        class_namespace = {
            name: child_environment.get(name, _UNKNOWN_VALUE) for name in local_names
        }
        if any(value.kind == "sqlalchemy-session-owner-base" for value in base_values):
            class_namespace["_session"] = _SQLALCHEMY_SESSION_VALUE
        receiver_base = next(
            (
                (
                    _SQLALCHEMY_SESSION_VALUE
                    if value.kind == "sqlalchemy-session-type"
                    else _SQLALCHEMY_CONNECTION_VALUE
                )
                for value in base_values
                if value.kind
                in {
                    "sqlalchemy-session-type",
                    "sqlalchemy-connection-type",
                }
            ),
            None,
        )
        if receiver_base is not None:
            class_namespace[_SQLALCHEMY_RECEIVER_CLASS_MEMBER] = receiver_base
        if any(
            value.kind in {"sqlalchemy-ddl-callable", "sqlalchemy-ddl-type"}
            or (
                value.kind == "class"
                and value.reference is not None
                and self._class_member(
                    value.reference,
                    _SQLALCHEMY_DDL_CLASS_MEMBER,
                )
                is not None
            )
            for value in base_values
        ):
            class_namespace[_SQLALCHEMY_DDL_CLASS_MEMBER] = _SQLALCHEMY_DDL_TYPE_VALUE
        self._classes[reference] = _DeferredClass(
            namespace=class_namespace,
            method_references=method_references,
            base_references=tuple(
                value.reference
                for value in base_values
                if value.kind == "class" and value.reference is not None
            ),
        )
        self._write_name(environment, node.name, class_value)
        for method_reference in method_references:
            self._callables[method_reference].defining_class_reference = reference
        tracked_environments = self._tracked_environments(environment)
        initial_environments = self._snapshot_environments(tracked_environments)
        initial_objects = self._snapshot_object_state()
        fallback_instance = self._new_instance(reference)
        try:
            initializer = self._class_member(reference, "__init__")
            if (
                initializer is not None
                and initializer.kind == "callable"
                and initializer.reference is not None
            ):
                initializer_record = self._callables.get(initializer.reference)
                if initializer_record is not None:
                    self._analyze_deferred_callable(
                        initializer_record,
                        call_arguments=_CallArguments((fallback_instance,), ()),
                        trigger=initializer_record.node,
                        depth=depth + 1,
                        commit_side_effects=True,
                    )
            for method_reference in method_references:
                record = self._callables[method_reference]
                if record.called or (
                    isinstance(record.node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and record.node.name == "__init__"
                ):
                    continue
                if record.method_kind == "staticmethod":
                    implicit: tuple[_AbstractValue, ...] = ()
                elif record.method_kind == "classmethod":
                    implicit = (class_value,)
                else:
                    implicit = (fallback_instance,)
                self._analyze_deferred_callable(
                    record,
                    call_arguments=_CallArguments(implicit, ()),
                    trigger=record.node,
                    depth=depth + 1,
                    commit_side_effects=False,
                )
        finally:
            self._restore_environments(tracked_environments, initial_environments)
            self._restore_object_state(initial_objects)

    def _analyze_branch(
        self,
        statements: list[ast.stmt],
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> tuple[
        dict[str, _AbstractValue],
        _BlockFlow,
        _ObjectStateSnapshot,
    ]:
        initial_state = self._snapshot_object_state()
        branch_environment = self._clone_environment(environment)
        flow = self._analyze_block(
            statements,
            branch_environment,
            scope_name=scope_name,
            depth=depth,
        )
        branch_state = self._snapshot_object_state()
        self._restore_object_state(initial_state)
        return branch_environment, flow, branch_state

    def _match_pattern(
        self,
        pattern: ast.pattern,
        subject: _AbstractValue,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> bool | None:
        if isinstance(pattern, ast.MatchValue):
            expected = self._evaluate(
                pattern.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            return self._values_equal(subject, expected)
        if isinstance(pattern, ast.MatchSingleton):
            expected = _AbstractValue("literal", literal=pattern.value)
            return self._values_equal(subject, expected)
        if isinstance(pattern, ast.MatchAs):
            matched = (
                True
                if pattern.pattern is None
                else self._match_pattern(
                    pattern.pattern,
                    subject,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
            )
            if matched is not False and pattern.name is not None:
                self._write_name(environment, pattern.name, subject)
            return matched
        if isinstance(pattern, ast.MatchSequence):
            if subject.kind != "sequence":
                return None if subject.kind == "unknown" else False
            if len(subject.items) != len(pattern.patterns):
                return False if not subject.risks else None
            results = tuple(
                self._match_pattern(
                    child,
                    value,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                for child, value in zip(
                    pattern.patterns,
                    subject.items,
                    strict=True,
                )
            )
            if any(result is False for result in results):
                return False
            return True if all(result is True for result in results) else None
        if isinstance(pattern, ast.MatchMapping):
            if subject.kind != "mapping":
                return None if subject.kind == "unknown" else False
            entries = dict(subject.entries)
            results: list[bool | None] = []
            matched_entries: dict[str | int, _AbstractValue] = {}
            for key_node, child_pattern in zip(
                pattern.keys,
                pattern.patterns,
                strict=True,
            ):
                key = self._literal_key(
                    self._evaluate(
                        key_node,
                        environment,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                )
                if key is None:
                    results.append(None)
                    continue
                if key not in entries:
                    results.append(None if subject.risks else False)
                    continue
                matched_entries[key] = entries[key]
                results.append(
                    self._match_pattern(
                        child_pattern,
                        entries[key],
                        environment,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                )
            if pattern.rest is not None:
                self._write_name(
                    environment,
                    pattern.rest,
                    _AbstractValue(
                        "mapping",
                        entries=tuple(
                            (key, value)
                            for key, value in entries.items()
                            if key not in matched_entries
                        ),
                        risks=subject.risks,
                    ),
                )
            if any(result is False for result in results):
                return False
            return True if all(result is True for result in results) else None
        if isinstance(pattern, ast.MatchOr):
            outcomes: list[bool | None] = []
            binding_environments: list[dict[str, _AbstractValue]] = []
            for child in pattern.patterns:
                candidate = self._clone_environment(environment)
                outcome = self._match_pattern(
                    child,
                    subject,
                    candidate,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                outcomes.append(outcome)
                if outcome is not False:
                    binding_environments.append(candidate)
                if outcome is True:
                    environment.clear()
                    environment.update(candidate)
                    return True
            if binding_environments:
                environment.clear()
                environment.update(_merge_environments(tuple(binding_environments)))
            return None if any(outcome is None for outcome in outcomes) else False
        if isinstance(pattern, ast.MatchStar):
            if pattern.name is not None:
                self._write_name(environment, pattern.name, subject)
            return True
        return None

    def _analyze_match(
        self,
        statement: ast.Match,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> _BlockFlow:
        subject = self._evaluate(
            statement.subject,
            environment,
            scope_name=scope_name,
            depth=depth + 1,
        )
        initial_state = self._snapshot_object_state()
        paths: list[
            tuple[
                dict[str, _AbstractValue],
                _BlockFlow,
                _ObjectStateSnapshot,
            ]
        ] = []
        exhaustive = False
        for case in statement.cases:
            self._restore_object_state(initial_state)
            candidate = self._clone_environment(environment)
            matched = self._match_pattern(
                case.pattern,
                subject,
                candidate,
                scope_name=scope_name,
                depth=depth + 1,
            )
            if matched is False:
                continue
            guard_truthiness: bool | None = True
            if case.guard is not None:
                guard_truthiness = self._truthiness(
                    self._evaluate(
                        case.guard,
                        candidate,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                )
            if guard_truthiness is False:
                continue
            flow = self._analyze_block(
                case.body,
                candidate,
                scope_name=scope_name,
                depth=depth + 1,
            )
            paths.append((candidate, flow, self._snapshot_object_state()))
            if matched is True and guard_truthiness is True:
                exhaustive = True
                break
        if not exhaustive:
            paths.append(
                (
                    self._clone_environment(environment),
                    _BlockFlow(),
                    initial_state,
                )
            )
        continuing = tuple(
            candidate for candidate, flow, _ in paths if flow.fallthrough
        )
        if continuing:
            environment.clear()
            environment.update(_merge_environments(continuing))
        self._restore_object_state(
            self._merge_object_states(tuple(state for _, _, state in paths))
        )
        return _BlockFlow(
            fallthrough=bool(continuing),
            returns=tuple(value for _, flow, _ in paths for value in flow.returns),
        )

    def _analyze_block(
        self,
        statements: list[ast.stmt],
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
        finalize_scope: bool = False,
        class_body: bool = False,
    ) -> _BlockFlow:
        if depth > _AST_DEPTH_BUDGET:
            if statements:
                self._add_violation(statements[0], "analysis-budget")
            return _BlockFlow(
                fallthrough=False,
                returns=(_FAIL_CLOSED_VALUE,),
            )
        returns: list[_AbstractValue] = []
        fallthrough = True
        for statement in statements:
            if not fallthrough:
                break
            next_depth = depth + 1
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._analyze_function(
                    statement,
                    environment,
                    depth=next_depth,
                    method_kind=(self._method_kind(statement) if class_body else None),
                )
            elif isinstance(statement, ast.ClassDef):
                self._analyze_class(statement, environment, depth=next_depth)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    name = alias.asname or alias.name.split(".", 1)[0]
                    if alias.name == "sqlite3":
                        self._write_name(environment, name, _SQLITE_MODULE_VALUE)
                    elif alias.name == "builtins":
                        self._write_name(environment, name, _BUILTINS_MODULE_VALUE)
                    elif alias.name == "importlib":
                        self._write_name(environment, name, _IMPORTLIB_MODULE_VALUE)
                    elif alias.name == "functools":
                        self._write_name(environment, name, _FUNCTOOLS_MODULE_VALUE)
                    elif alias.name == "operator":
                        self._write_name(environment, name, _OPERATOR_MODULE_VALUE)
                    elif alias.name == "typing":
                        self._write_name(environment, name, _TYPING_MODULE_VALUE)
                    elif alias.name in _ORM_MODEL_MODULES:
                        self._write_name(environment, name, _ORM_MODEL_MODULE_VALUE)
                    elif alias.name == "sqlalchemy" or alias.name.startswith(
                        "sqlalchemy."
                    ):
                        self._write_name(
                            environment,
                            name,
                            _sqlalchemy_module_value(
                                alias.name if alias.asname else "sqlalchemy"
                            ),
                        )
                    else:
                        self._write_name(environment, name, _UNKNOWN_SQL_VALUE)
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    name = alias.asname or alias.name
                    if statement.module == "sqlite3" and alias.name == "connect":
                        self._write_name(environment, name, _SQLITE_CONNECT_VALUE)
                    elif (
                        statement.module == "importlib"
                        and alias.name == "import_module"
                    ):
                        self._write_name(environment, name, _IMPORT_MODULE_VALUE)
                    elif statement.module == "functools" and alias.name == "partial":
                        self._write_name(environment, name, _FUNCTOOLS_PARTIAL_VALUE)
                    elif (
                        statement.module == "functools"
                        and alias.name == "cached_property"
                    ) or (statement.module == "builtins" and alias.name == "property"):
                        self._write_name(
                            environment,
                            name,
                            _PROPERTY_DECORATOR_VALUE,
                        )
                    elif statement.module == "operator" and alias.name in {
                        "attrgetter",
                        "methodcaller",
                    }:
                        self._write_name(
                            environment,
                            name,
                            (
                                _OPERATOR_ATTRGETTER_VALUE
                                if alias.name == "attrgetter"
                                else _OPERATOR_METHODCALLER_VALUE
                            ),
                        )
                    elif statement.module == "typing" and alias.name in {
                        "Annotated",
                        "Optional",
                        "Union",
                    }:
                        self._write_name(
                            environment,
                            name,
                            {
                                "Annotated": _TYPING_ANNOTATED_VALUE,
                                "Optional": _TYPING_OPTIONAL_VALUE,
                                "Union": _TYPING_UNION_VALUE,
                            }[alias.name],
                        )
                    elif statement.module == "abc" and alias.name == "abstractmethod":
                        self._write_name(
                            environment,
                            name,
                            _SAFE_FUNCTION_DECORATOR_VALUE,
                        )
                    elif statement.module in {
                        "hermes_cloud.platform.sqlalchemy.repositories.base",
                        "hermes_cloud.platform.sqlalchemy.repositories.identity",
                        "hermes_cloud.platform.sqlalchemy.repositories.projection",
                    } and alias.name in {
                        "SqlAlchemySessionRepositoryBase",
                        "SqlAlchemyIdentityRepositoryBase",
                        "SqlAlchemySessionProjectionRepositoryBase",
                    }:
                        self._write_name(
                            environment,
                            name,
                            _SQLALCHEMY_SESSION_OWNER_BASE_VALUE,
                        )
                    elif (
                        statement.module
                        == "hermes_cloud.platform.sqlalchemy.repositories.identity"
                        and alias.name
                        in {"ticket_consumption_scope", "ticket_session_scope"}
                    ):
                        self._write_name(
                            environment,
                            name,
                            _ORM_EXPRESSION_CALLABLE_VALUE,
                        )
                    elif (
                        statement.module
                        == "hermes_cloud.platform.sqlalchemy.connector_transport_cursor"
                        and alias.name
                        in {
                            "advance_locked_connector_transport_cursor",
                            "lock_active_connector_transport_epoch",
                            "lock_connector_transport_cursor",
                        }
                    ):
                        self._write_name(
                            environment,
                            name,
                            _SQLALCHEMY_TRANSPORT_HELPER_VALUE,
                        )
                    elif (
                        statement.module == _ORM_MODEL_PACKAGE
                        and alias.name == "models"
                    ):
                        self._write_name(environment, name, _ORM_MODEL_MODULE_VALUE)
                    elif _orm_model_export_proven(statement.module, alias.name):
                        self._write_name(environment, name, _ORM_MODEL_VALUE)
                    elif (
                        statement.module is not None
                        and statement.module.startswith("hermes_cloud.modules.")
                        and statement.module.endswith((".domain", ".ports"))
                    ) or (
                        statement.module in {"datetime", "uuid"}
                        and alias.name
                        in {
                            "date",
                            "datetime",
                            "timedelta",
                            "UUID",
                            "uuid4",
                            "uuid5",
                        }
                    ):
                        self._write_name(environment, name, _ORM_BIND_TYPE_VALUE)
                    elif (
                        statement.module is not None
                        and (
                            sqlalchemy_value := _sqlalchemy_export_value(
                                statement.module,
                                alias.name,
                            )
                        )
                        is not None
                    ):
                        self._write_name(environment, name, sqlalchemy_value)
                    elif statement.module is not None and (
                        statement.module == "sqlalchemy"
                        or statement.module.startswith("sqlalchemy.")
                    ):
                        self._write_name(
                            environment,
                            name,
                            _UNKNOWN_SQLALCHEMY_EXPORT_VALUE,
                        )
                    else:
                        self._write_name(environment, name, _UNKNOWN_SQL_VALUE)
            elif isinstance(statement, ast.Assign):
                value = self._evaluate(
                    statement.value,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                for target in statement.targets:
                    self._bind_target(target, value, environment)
            elif isinstance(statement, ast.AnnAssign):
                annotation_value = self._annotation_value(
                    statement.annotation,
                    environment,
                )
                assigned_value = (
                    self._evaluate(
                        statement.value,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    if statement.value is not None
                    else _UNKNOWN_VALUE
                )
                value = (
                    annotation_value
                    if annotation_value != _UNKNOWN_VALUE
                    and assigned_value.kind in {"unknown", "safe"}
                    else assigned_value
                )
                self._bind_target(statement.target, value, environment)
            elif isinstance(statement, ast.AugAssign):
                current = self._evaluate(
                    statement.target,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                added = self._evaluate(
                    statement.value,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                value = self._binary_value(statement.op, current, added)
                self._bind_target(statement.target, value, environment)
            elif isinstance(statement, ast.Expr):
                self._evaluate(
                    statement.value,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
            elif isinstance(statement, ast.If):
                condition = self._evaluate(
                    statement.test,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                truthiness = self._truthiness(condition)
                branches: list[
                    tuple[
                        dict[str, _AbstractValue],
                        _BlockFlow,
                        _ObjectStateSnapshot,
                    ]
                ] = []
                if truthiness is not False:
                    branches.append(
                        self._analyze_branch(
                            statement.body,
                            environment,
                            scope_name=scope_name,
                            depth=next_depth,
                        )
                    )
                if truthiness is not True:
                    branches.append(
                        self._analyze_branch(
                            statement.orelse,
                            environment,
                            scope_name=scope_name,
                            depth=next_depth,
                        )
                    )
                returns.extend(
                    value
                    for _, branch_flow, _ in branches
                    for value in branch_flow.returns
                )
                continuing = tuple(
                    candidate for candidate, flow, _ in branches if flow.fallthrough
                )
                if continuing:
                    environment.clear()
                    environment.update(_merge_environments(continuing))
                else:
                    fallthrough = False
                self._restore_object_state(
                    self._merge_object_states(tuple(state for _, _, state in branches))
                )
            elif isinstance(statement, ast.Match):
                match_flow = self._analyze_match(
                    statement,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                returns.extend(match_flow.returns)
                fallthrough = match_flow.fallthrough
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                iterable = self._evaluate(
                    statement.iter,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                iterable = self._consume_iterable(
                    iterable,
                    trigger=statement.iter,
                    depth=next_depth,
                )
                if iterable.kind == "sequence":
                    for item in iterable.items:
                        self._bind_target(statement.target, item, environment)
                        loop_flow = self._analyze_block(
                            statement.body,
                            environment,
                            scope_name=scope_name,
                            depth=next_depth,
                        )
                        returns.extend(loop_flow.returns)
                        if not loop_flow.fallthrough:
                            fallthrough = False
                            break
                else:
                    before_loop = self._clone_environment(environment)
                    before_loop_state = self._snapshot_object_state()
                    loop_environment = self._clone_environment(environment)
                    self._bind_target(
                        statement.target,
                        _UNKNOWN_VALUE,
                        loop_environment,
                    )
                    loop_flow = self._analyze_block(
                        statement.body,
                        loop_environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    returns.extend(loop_flow.returns)
                    loop_state = self._snapshot_object_state()
                    environment.clear()
                    environment.update(
                        _merge_environments((before_loop, loop_environment))
                    )
                    self._restore_object_state(
                        self._merge_object_states((before_loop_state, loop_state))
                    )
                if fallthrough:
                    else_flow = self._analyze_block(
                        statement.orelse,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    returns.extend(else_flow.returns)
                    fallthrough = else_flow.fallthrough
            elif isinstance(statement, ast.While):
                condition = self._evaluate(
                    statement.test,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                if self._truthiness(condition) is False:
                    else_flow = self._analyze_block(
                        statement.orelse,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    returns.extend(else_flow.returns)
                    fallthrough = else_flow.fallthrough
                    continue
                before_loop_state = self._snapshot_object_state()
                loop_environment, loop_flow, loop_state = self._analyze_branch(
                    statement.body,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                returns.extend(loop_flow.returns)
                environment.update(
                    _merge_environments((environment.copy(), loop_environment))
                )
                self._restore_object_state(
                    self._merge_object_states((before_loop_state, loop_state))
                )
                else_flow = self._analyze_block(
                    statement.orelse,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                returns.extend(else_flow.returns)
                fallthrough = else_flow.fallthrough
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    value = self._evaluate(
                        item.context_expr,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    if item.optional_vars is not None:
                        if value.kind == "instance" and value.reference is not None:
                            instance_record = self._instances.get(value.reference)
                            enter = (
                                self._class_member(
                                    instance_record.class_reference,
                                    "__enter__",
                                )
                                if instance_record is not None
                                else None
                            )
                            if (
                                enter is not None
                                and enter.kind == "callable"
                                and enter.reference is not None
                            ):
                                enter_record = self._callables.get(enter.reference)
                                if enter_record is not None:
                                    value = self._analyze_deferred_callable(
                                        enter_record,
                                        call_arguments=_CallArguments((value,), ()),
                                        trigger=item.context_expr,
                                        depth=next_depth,
                                        commit_side_effects=True,
                                    )
                        if value.kind == "sqlalchemy-session-context":
                            value = _SQLALCHEMY_SESSION_VALUE
                        self._bind_target(
                            item.optional_vars,
                            value,
                            environment,
                        )
                nested_flow = self._analyze_block(
                    statement.body,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                returns.extend(nested_flow.returns)
                fallthrough = nested_flow.fallthrough
            elif isinstance(statement, ast.Try):
                paths = [
                    self._analyze_branch(
                        [*statement.body, *statement.orelse],
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                ]
                paths.extend(
                    self._analyze_branch(
                        handler.body,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    for handler in statement.handlers
                )
                returns.extend(value for _, flow, _ in paths for value in flow.returns)
                continuing = tuple(
                    branch for branch, flow, _ in paths if flow.fallthrough
                )
                if continuing:
                    environment.clear()
                    environment.update(_merge_environments(continuing))
                else:
                    fallthrough = False
                self._restore_object_state(
                    self._merge_object_states(tuple(state for _, _, state in paths))
                )
                final_flow = self._analyze_block(
                    statement.finalbody,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                returns.extend(final_flow.returns)
                fallthrough = fallthrough and final_flow.fallthrough
            elif isinstance(statement, ast.Return):
                returns.append(
                    self._evaluate(
                        statement.value,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                    if statement.value is not None
                    else _SAFE_VALUE
                )
                fallthrough = False
            elif isinstance(statement, ast.Raise):
                value = statement.exc
                if value is not None:
                    self._evaluate(
                        value,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
                fallthrough = False
            elif isinstance(statement, ast.Assert):
                self._evaluate(
                    statement.test,
                    environment,
                    scope_name=scope_name,
                    depth=next_depth,
                )
                if statement.msg is not None:
                    self._evaluate(
                        statement.msg,
                        environment,
                        scope_name=scope_name,
                        depth=next_depth,
                    )
            elif isinstance(statement, ast.Delete):
                for target in statement.targets:
                    for name in _target_names(target):
                        self._write_name(environment, name, _UNKNOWN_VALUE)
        if finalize_scope:
            self._finalize_callables(environment, depth=depth + 1)
        return _BlockFlow(
            fallthrough=fallthrough,
            returns=tuple(returns),
        )

    def _merge_return_values(
        self,
        values: tuple[_AbstractValue, ...],
    ) -> _AbstractValue:
        value = values[0]
        for candidate in values[1:]:
            value = _merge_values(value, candidate)
        return value

    def _binary_value(
        self,
        operator: ast.operator,
        left: _AbstractValue,
        right: _AbstractValue,
    ) -> _AbstractValue:
        if (
            isinstance(operator, ast.Add)
            and left.kind == "string"
            and right.kind == "string"
        ):
            return _string_value((left.text or "") + (right.text or ""))
        if (
            left.kind == "literal"
            and right.kind == "literal"
            and isinstance(left.literal, int)
            and isinstance(right.literal, int)
        ):
            if isinstance(operator, ast.Add):
                return _AbstractValue(
                    "literal",
                    literal=left.literal + right.literal,
                )
            if isinstance(operator, ast.Sub):
                return _AbstractValue(
                    "literal",
                    literal=left.literal - right.literal,
                )
            if isinstance(operator, ast.Mult):
                return _AbstractValue(
                    "literal",
                    literal=left.literal * right.literal,
                )
            if isinstance(operator, ast.FloorDiv) and right.literal:
                return _AbstractValue(
                    "literal",
                    literal=left.literal // right.literal,
                )
        if isinstance(operator, ast.Mod) and left.kind == "string":
            if right.kind == "string":
                interpolation: object = right.text or ""
            elif right.kind == "literal":
                interpolation = right.literal
            elif right.kind == "sequence":
                concrete: list[object] = []
                for value in right.items:
                    if value.kind == "string":
                        concrete.append(value.text or "")
                    elif value.kind == "literal":
                        concrete.append(value.literal)
                    else:
                        return _UNKNOWN_SQL_VALUE
                interpolation = tuple(concrete)
            else:
                return _UNKNOWN_SQL_VALUE
            try:
                return _string_value((left.text or "") % interpolation)
            except (TypeError, ValueError):
                return _UNKNOWN_SQL_VALUE
        return _merge_values(left, right)

    def _comparison_value(
        self,
        operator: ast.cmpop,
        left: _AbstractValue,
        right: _AbstractValue,
    ) -> _AbstractValue:
        if self._sql_clause_proven(left) or self._sql_clause_proven(right):
            left_is_bound = self._sql_value_proven(left) or (
                left.kind == "unknown" and not left.risks
            )
            right_is_bound = self._sql_value_proven(right) or (
                right.kind == "unknown" and not right.risks
            )
            return (
                _ORM_EXPRESSION_VALUE
                if left_is_bound and right_is_bound
                else _UNKNOWN_SQL_VALUE
            )
        if isinstance(operator, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            equal = self._values_equal(left, right)
            if equal is None:
                return _UNKNOWN_VALUE
            result = (
                not equal if isinstance(operator, (ast.NotEq, ast.IsNot)) else equal
            )
            return _AbstractValue("literal", literal=result)
        left_value: str | int | bool | None
        right_value: str | int | bool | None
        left_value = (
            left.text
            if left.kind == "string"
            else left.literal
            if left.kind == "literal"
            else None
        )
        right_value = (
            right.text
            if right.kind == "string"
            else right.literal
            if right.kind == "literal"
            else None
        )
        try:
            if isinstance(operator, ast.Lt) and left_value is not None:
                result = left_value < right_value  # type: ignore[operator]
            elif isinstance(operator, ast.LtE) and left_value is not None:
                result = left_value <= right_value  # type: ignore[operator]
            elif isinstance(operator, ast.Gt) and left_value is not None:
                result = left_value > right_value  # type: ignore[operator]
            elif isinstance(operator, ast.GtE) and left_value is not None:
                result = left_value >= right_value  # type: ignore[operator]
            elif isinstance(operator, (ast.In, ast.NotIn)):
                if right.kind == "sequence":
                    comparisons = tuple(
                        self._values_equal(left, item) for item in right.items
                    )
                    if any(comparison is True for comparison in comparisons):
                        result = True
                    elif all(comparison is False for comparison in comparisons):
                        result = False
                    else:
                        return _UNKNOWN_VALUE
                elif right.kind == "mapping":
                    key = self._literal_key(left)
                    if key is None:
                        return _UNKNOWN_VALUE
                    result = key in dict(right.entries)
                elif left.kind == right.kind == "string":
                    result = (left.text or "") in (right.text or "")
                else:
                    return _UNKNOWN_VALUE
                if isinstance(operator, ast.NotIn):
                    result = not result
            else:
                return _UNKNOWN_VALUE
        except TypeError:
            return _UNKNOWN_VALUE
        return _AbstractValue("literal", literal=result)

    def _receiver_class_reference(
        self,
        receiver: _AbstractValue,
    ) -> int | None:
        if receiver.kind == "class":
            return receiver.reference
        if receiver.kind == "instance" and receiver.reference is not None:
            record = self._instances.get(receiver.reference)
            return record.class_reference if record is not None else None
        return None

    def _super_attribute_value(
        self,
        owner: _AbstractValue,
        attribute: str,
    ) -> _AbstractValue:
        if owner.reference is None or not owner.items:
            return _UNKNOWN_SQL_VALUE
        receiver = owner.items[0]
        receiver_class_reference = self._receiver_class_reference(receiver)
        if receiver_class_reference is None:
            return _UNKNOWN_SQL_VALUE
        mro = self._class_mro(receiver_class_reference)
        try:
            start_index = mro.index(owner.reference)
        except ValueError:
            return _UNKNOWN_SQL_VALUE
        for class_reference in mro[start_index + 1 :]:
            class_record = self._classes.get(class_reference)
            if class_record is None or attribute not in class_record.namespace:
                continue
            member = class_record.namespace[attribute]
            if member.kind != "callable" or member.reference is None:
                return member
            callable_record = self._callables.get(member.reference)
            method_kind = (
                callable_record.method_kind if callable_record is not None else None
            )
            if method_kind == "staticmethod":
                return member
            if method_kind == "classmethod":
                return _AbstractValue(
                    "bound-callable",
                    items=(
                        _AbstractValue(
                            "class",
                            reference=receiver_class_reference,
                        ),
                    ),
                    reference=member.reference,
                )
            return _AbstractValue(
                "bound-callable",
                items=(receiver,),
                reference=member.reference,
            )
        return _UNKNOWN_SQL_VALUE

    def _attribute_value(
        self,
        owner: _AbstractValue,
        attribute: str,
    ) -> _AbstractValue:
        if owner.kind == "union":
            candidates = owner.items
            non_null = tuple(
                candidate
                for candidate in candidates
                if not (candidate.kind == "literal" and candidate.literal is None)
            )
            if (
                non_null
                and len(non_null) != len(candidates)
                and all(candidate.kind == "orm-result" for candidate in non_null)
                and attribute not in _SQLALCHEMY_STATEMENT_SINK_METHODS
            ):
                candidates = non_null
            values = tuple(
                self._attribute_value(candidate, attribute) for candidate in candidates
            )
            return self._merge_return_values(values)
        if owner.kind == "super":
            return self._super_attribute_value(owner, attribute)
        if attribute == "__call__" and self._is_callable_proof(owner):
            return owner
        if owner.kind == "property-descriptor" and attribute in {
            "deleter",
            "getter",
            "setter",
        }:
            return _AbstractValue(
                "property-mutator-decorator",
                member=attribute,
                items=(owner,),
            )
        if owner.kind in {"class", "instance"} and owner.reference is not None:
            if owner.kind == "class":
                class_reference = owner.reference
                member = self._class_member(class_reference, attribute)
            else:
                instance_record = self._instances.get(owner.reference)
                class_reference = (
                    instance_record.class_reference
                    if instance_record is not None
                    else -1
                )
                member = (
                    instance_record.namespace.get(attribute)
                    if instance_record is not None
                    else None
                )
                if member is None:
                    member = self._class_member(class_reference, attribute)
            if member is None:
                if (
                    attribute in _SQLALCHEMY_STATEMENT_SINK_METHODS
                    and self._sql_receiver_proof(owner) is _SqlReceiverProof.SQLALCHEMY
                ):
                    return _AbstractValue(
                        "sql-statement-sink-callable",
                        member=attribute,
                        items=(owner,),
                    )
                return _UNKNOWN_SQL_VALUE
            if (
                member.kind == "property-descriptor"
                and owner.kind == "instance"
                and member.items
            ):
                getter = member.items[0]
                trigger = (
                    self._callables[getter.reference].node
                    if getter.reference in self._callables
                    else ast.Pass()
                )
                return self._invoke_callable_candidate(
                    _AbstractValue(
                        "bound-callable",
                        items=(owner,),
                        reference=getter.reference,
                    ),
                    _CallArguments((), ()),
                    trigger=trigger,
                    depth=0,
                )
            if member.kind == "callable" and member.reference is not None:
                callable_record = self._callables.get(member.reference)
                method_kind = (
                    callable_record.method_kind if callable_record is not None else None
                )
                if method_kind == "staticmethod":
                    return member
                if method_kind == "classmethod":
                    return _AbstractValue(
                        "bound-callable",
                        items=(_AbstractValue("class", reference=class_reference),),
                        reference=member.reference,
                    )
                if owner.kind == "instance":
                    return _AbstractValue(
                        "bound-callable",
                        items=(owner,),
                        reference=member.reference,
                    )
            return member
        if attribute == "connect" and self._value_has(owner, "sqlite-module"):
            return _SQLITE_CONNECT_VALUE
        if self._value_has(owner, "sqlalchemy-module"):
            module = owner.text or "sqlalchemy"
            exported = _sqlalchemy_export_value(module, attribute)
            return exported if exported is not None else _UNKNOWN_SQL_VALUE
        if owner.kind == "functools-module" and attribute == "partial":
            return _FUNCTOOLS_PARTIAL_VALUE
        if (owner.kind == "functools-module" and attribute == "cached_property") or (
            owner.kind == "builtins-module" and attribute == "property"
        ):
            return _PROPERTY_DECORATOR_VALUE
        if owner.kind == "operator-module" and attribute in {
            "attrgetter",
            "methodcaller",
        }:
            return (
                _OPERATOR_ATTRGETTER_VALUE
                if attribute == "attrgetter"
                else _OPERATOR_METHODCALLER_VALUE
            )
        if owner.kind == "typing-module" and attribute in {
            "Annotated",
            "Optional",
            "Union",
        }:
            return {
                "Annotated": _TYPING_ANNOTATED_VALUE,
                "Optional": _TYPING_OPTIONAL_VALUE,
                "Union": _TYPING_UNION_VALUE,
            }[attribute]
        if attribute == "import_module" and self._value_has(owner, "importlib-module"):
            return _IMPORT_MODULE_VALUE
        if attribute == "join" and owner.kind == "string":
            return _AbstractValue("bound-join", text=owner.text)
        if attribute == "format" and owner.kind == "string":
            return _AbstractValue("bound-format", text=owner.text)
        if owner.kind == "string":
            return _AbstractValue(
                "bound-string-method",
                text=owner.text,
                member=attribute,
            )
        if attribute == "get" and owner.kind == "mapping":
            return _AbstractValue(
                "bound-mapping-get",
                entries=owner.entries,
                risks=owner.risks,
            )
        if owner.kind == "sqlalchemy-session-factory" and attribute == "begin":
            return _AbstractValue("sqlalchemy-session-context-callable")
        if (
            attribute == "get"
            and self._sql_receiver_proof(owner) is _SqlReceiverProof.SQLALCHEMY
        ):
            return _AbstractValue("orm-get-callable", items=(owner,))
        if owner.kind == "orm-expression-namespace":
            return _AbstractValue("orm-expression-callable", member="func")
        if owner.kind == "orm-statement":
            if attribute in {"c", "excluded"}:
                return _ORM_COLUMN_NAMESPACE_VALUE
            return _AbstractValue("orm-statement-method", member=attribute)
        if owner.kind == "orm-result":
            if attribute in {
                "all",
                "first",
                "one",
                "one_or_none",
                "scalar_one",
                "scalar_one_or_none",
            }:
                return _AbstractValue("orm-result-method", member=attribute)
            return _ORM_BIND_VALUE
        if owner.kind == "orm-result-collection":
            if attribute in {"all", "first", "one", "one_or_none"}:
                return _AbstractValue("orm-result-method", member=attribute)
            return _UNKNOWN_SQL_VALUE
        if owner.kind in {"orm-column-namespace"}:
            return _ORM_COLUMN_VALUE
        if owner.kind == "orm-model-module":
            return _ORM_MODEL_VALUE
        if owner.kind in {"orm-model", "orm-table"}:
            return (
                _ORM_COLUMN_NAMESPACE_VALUE if attribute == "c" else _ORM_COLUMN_VALUE
            )
        if owner.kind in {"orm-column", "orm-expression"}:
            return _AbstractValue("orm-expression-callable", member=attribute)
        if owner.kind in {"orm-bind", "orm-bind-member", "orm-bind-type"}:
            return _AbstractValue(
                "orm-bind-member",
                member=attribute,
            )
        if attribute in _SQLALCHEMY_STATEMENT_SINK_METHODS:
            return _AbstractValue(
                "sql-statement-sink-callable",
                member=attribute,
                items=(owner,),
            )
        return _UNKNOWN_SQL_VALUE

    def _attribute_path_value(
        self,
        owner: _AbstractValue,
        path: str,
    ) -> _AbstractValue:
        value = owner
        for attribute in path.split("."):
            if not attribute:
                return _UNKNOWN_SQL_VALUE
            value = self._attribute_value(value, attribute)
        return value

    def _sql_receiver_proof(
        self,
        owner: _AbstractValue,
    ) -> _SqlReceiverProof:
        if owner.kind == "union":
            proofs = {self._sql_receiver_proof(candidate) for candidate in owner.items}
            return proofs.pop() if len(proofs) == 1 else _SqlReceiverProof.UNPROVEN
        if owner.kind in {
            "sqlalchemy-session",
            "sqlalchemy-connection",
            "sqlite-connection",
            "sqlite-cursor",
        }:
            return _SqlReceiverProof.SQLALCHEMY
        if owner.kind == "instance" and owner.reference is not None:
            instance = self._instances.get(owner.reference)
            receiver = (
                self._class_member(
                    instance.class_reference,
                    _SQLALCHEMY_RECEIVER_CLASS_MEMBER,
                )
                if instance is not None
                else None
            )
            return (
                _SqlReceiverProof.SQLALCHEMY
                if receiver is not None
                else _SqlReceiverProof.NON_SQL
            )
        return _SqlReceiverProof.UNPROVEN

    def _evaluate_call_arguments(
        self,
        node: ast.Call,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> _CallArguments:
        positional: list[_AbstractValue] = []
        keywords: list[tuple[str, _AbstractValue]] = []
        unknown_positional = False
        unknown_keywords = False
        for argument in node.args:
            if isinstance(argument, ast.Starred):
                value = self._evaluate(
                    argument.value,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                if value.kind == "sequence":
                    positional.extend(value.items)
                    unknown_positional = unknown_positional or bool(value.risks)
                else:
                    unknown_positional = True
                continue
            positional.append(
                self._evaluate(
                    argument,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
            )
        for keyword in node.keywords:
            value = self._evaluate(
                keyword.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            if keyword.arg is not None:
                keywords.append((keyword.arg, value))
            elif value.kind == "mapping":
                keywords.extend(value.entries)
                unknown_keywords = unknown_keywords or bool(value.risks)
            elif value.kind == "orm-bind-mapping":
                continue
            else:
                unknown_keywords = True
        return _CallArguments(
            positional=tuple(positional),
            keywords=tuple(keywords),
            unknown_positional=unknown_positional,
            unknown_keywords=unknown_keywords,
        )

    def _evaluate_string_method(
        self,
        function: _AbstractValue,
        arguments: tuple[_AbstractValue, ...],
    ) -> _AbstractValue:
        method = function.member or ""
        text = function.text or ""
        string_arguments = [
            argument.text if argument.kind == "string" else None
            for argument in arguments
        ]
        literal_arguments = [
            argument.literal if argument.kind == "literal" else None
            for argument in arguments
        ]
        try:
            if (
                method
                in {
                    "capitalize",
                    "casefold",
                    "lower",
                    "swapcase",
                    "title",
                    "upper",
                }
                and not arguments
            ):
                return _string_value(getattr(text, method)())
            if (
                method in {"strip", "lstrip", "rstrip"}
                and len(arguments) <= 1
                and (not arguments or string_arguments[0] is not None)
            ):
                return _string_value(
                    getattr(text, method)(string_arguments[0] if arguments else None)
                )
            if method in {"removeprefix", "removesuffix"} and (
                len(arguments) == 1 and string_arguments[0] is not None
            ):
                return _string_value(getattr(text, method)(string_arguments[0]))
            if method == "replace" and 2 <= len(arguments) <= 3:
                old, new = string_arguments[:2]
                count = literal_arguments[2] if len(arguments) == 3 else None
                if (
                    old is not None
                    and new is not None
                    and (count is None or isinstance(count, int))
                ):
                    return _string_value(
                        text.replace(old, new)
                        if count is None
                        else text.replace(old, new, count)
                    )
            if method in {"center", "ljust", "rjust"} and 1 <= len(arguments) <= 2:
                width = literal_arguments[0]
                fill = string_arguments[1] if len(arguments) == 2 else " "
                if isinstance(width, int) and fill is not None:
                    return _string_value(getattr(text, method)(width, fill))
            if method == "zfill" and len(arguments) == 1:
                width = literal_arguments[0]
                if isinstance(width, int):
                    return _string_value(text.zfill(width))
        except (TypeError, ValueError):
            return _UNKNOWN_SQL_VALUE
        return _UNKNOWN_SQL_VALUE

    def _deferred_iterable_value(
        self,
        *,
        node: ast.GeneratorExp | None,
        environment: dict[str, _AbstractValue],
        scope_name: str,
        depth: int,
        mapper: _AbstractValue | None = None,
        source: _AbstractValue | None = None,
        leading_iterable: _AbstractValue | None = None,
    ) -> _AbstractValue:
        reference = self._next_iterable_reference
        self._next_iterable_reference += 1
        self._iterables[reference] = _DeferredIterable(
            node=node,
            environment=environment,
            scope_name=scope_name,
            depth=depth,
            leading_iterable=leading_iterable,
            mapper=mapper,
            source=source,
        )
        return _AbstractValue("lazy-iterable", reference=reference)

    def _invoke_mapper(
        self,
        mapper: _AbstractValue,
        value: _AbstractValue,
        *,
        trigger: ast.AST,
        depth: int,
    ) -> _AbstractValue:
        if mapper.kind == "union":
            return self._merge_return_values(
                tuple(
                    self._invoke_mapper(
                        candidate,
                        value,
                        trigger=trigger,
                        depth=depth,
                    )
                    for candidate in mapper.items
                )
            )
        if (
            mapper.kind in {"callable", "bound-callable"}
            and mapper.reference is not None
        ):
            record = self._callables.get(mapper.reference)
            if record is None:
                return _FAIL_CLOSED_VALUE
            positional = (
                (*mapper.items, value) if mapper.kind == "bound-callable" else (value,)
            )
            return self._analyze_deferred_callable(
                record,
                call_arguments=_CallArguments(positional, ()),
                trigger=trigger,
                depth=depth + 1,
                commit_side_effects=True,
            )
        return _UNKNOWN_SQL_VALUE

    def _consume_iterable(
        self,
        value: _AbstractValue,
        *,
        trigger: ast.AST,
        depth: int,
    ) -> _AbstractValue:
        if value.kind == "sequence":
            return value
        if value.kind == "union":
            consumed = tuple(
                self._consume_iterable(
                    candidate,
                    trigger=trigger,
                    depth=depth + 1,
                )
                for candidate in value.items
            )
            items = tuple(
                item
                for candidate in consumed
                if candidate.kind == "sequence"
                for item in candidate.items
            )
            risks = frozenset(
                risk for candidate in consumed for risk in _value_risks(candidate)
            )
            return _AbstractValue("sequence", items=items, risks=risks)
        if value.kind != "lazy-iterable" or value.reference is None:
            return _AbstractValue("sequence", items=(_UNKNOWN_SQL_VALUE,))
        record = self._iterables.get(value.reference)
        if record is None:
            return _AbstractValue("sequence", items=(_UNKNOWN_SQL_VALUE,))
        if record.node is not None:
            return self._evaluate_sequence_comprehension(
                record.node,
                record.environment,
                scope_name=record.scope_name,
                depth=record.depth + 1,
                leading_iterable=record.leading_iterable,
            )
        if record.mapper is None or record.source is None:
            return _AbstractValue("sequence", items=(_UNKNOWN_SQL_VALUE,))
        source = self._consume_iterable(
            record.source,
            trigger=trigger,
            depth=depth + 1,
        )
        if source.kind != "sequence":
            return _AbstractValue("sequence", items=(_UNKNOWN_SQL_VALUE,))
        return _AbstractValue(
            "sequence",
            items=tuple(
                self._invoke_mapper(
                    record.mapper,
                    item,
                    trigger=trigger,
                    depth=depth + 1,
                )
                for item in source.items
            ),
            risks=source.risks,
        )

    def _evaluate_super_call(
        self,
        call_arguments: _CallArguments,
        environment: dict[str, _AbstractValue],
    ) -> _AbstractValue:
        if len(call_arguments.positional) >= 2:
            starting_class, receiver = call_arguments.positional[:2]
            if starting_class.kind == "class" and starting_class.reference is not None:
                return _AbstractValue(
                    "super",
                    items=(receiver,),
                    reference=starting_class.reference,
                )
            return _UNKNOWN_SQL_VALUE
        if call_arguments.positional or not self._active_callable_stack:
            return _UNKNOWN_SQL_VALUE
        record = self._active_callable_stack[-1]
        if record.defining_class_reference is None:
            return _UNKNOWN_SQL_VALUE
        positional_parameters = (
            (*record.node.args.posonlyargs, *record.node.args.args)
            if isinstance(
                record.node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
            else ()
        )
        if not positional_parameters:
            return _UNKNOWN_SQL_VALUE
        receiver = self._read_name(environment, positional_parameters[0].arg)
        return _AbstractValue(
            "super",
            items=(receiver,),
            reference=record.defining_class_reference,
        )

    def _invoke_callable_candidate(
        self,
        function: _AbstractValue,
        call_arguments: _CallArguments,
        *,
        trigger: ast.AST,
        depth: int,
    ) -> _AbstractValue:
        if function.kind == "sql-statement-sink-callable":
            return self._evaluate_statement_sink_call(
                function,
                call_arguments,
                trigger=trigger,
            )
        if function.kind == "getattr-callable":
            if (
                len(call_arguments.positional) < 2
                or call_arguments.positional[1].kind != "string"
                or call_arguments.positional[1].text is None
                or call_arguments.unknown_positional
                or call_arguments.unknown_keywords
            ):
                if call_arguments.positional and self._contains_sql_receiver(
                    call_arguments.positional[0]
                ):
                    self._add_violation(trigger, "unproven-sql-call-chain")
                return _UNKNOWN_SQL_VALUE
            return self._attribute_value(
                call_arguments.positional[0],
                call_arguments.positional[1].text,
            )
        if function.kind == "partial-callable" and function.items:
            return self._invoke_callable_candidate(
                function.items[0],
                _CallArguments(
                    positional=(
                        *function.items[1:],
                        *call_arguments.positional,
                    ),
                    keywords=(
                        *function.entries,
                        *call_arguments.keywords,
                    ),
                    unknown_positional=(
                        "unknown-positional" in function.risks
                        or call_arguments.unknown_positional
                    ),
                    unknown_keywords=(
                        "unknown-keywords" in function.risks
                        or call_arguments.unknown_keywords
                    ),
                ),
                trigger=trigger,
                depth=depth + 1,
            )
        if function.kind in {
            "operator-attrgetter-callable",
            "operator-methodcaller-callable",
        }:
            receiver = (
                call_arguments.positional[0]
                if call_arguments.positional
                else _UNKNOWN_VALUE
            )
            if (
                len(call_arguments.positional) != 1
                or call_arguments.unknown_positional
                or call_arguments.unknown_keywords
            ):
                return _UNKNOWN_SQL_VALUE
            if function.kind == "operator-attrgetter-callable":
                paths = function.items
                if (
                    not paths
                    or function.entries
                    or function.risks
                    or any(path.kind != "string" or path.text is None for path in paths)
                ):
                    if self._contains_sql_receiver(receiver):
                        self._add_violation(trigger, "unproven-sql-call-chain")
                    return _UNKNOWN_SQL_VALUE
                targets = tuple(
                    self._attribute_path_value(receiver, path.text or "")
                    for path in paths
                )
                return (
                    targets[0]
                    if len(targets) == 1
                    else _AbstractValue("sequence", items=targets)
                )
            if function.member is None:
                if self._contains_sql_receiver(receiver):
                    self._add_violation(trigger, "unproven-sql-call-chain")
                return _UNKNOWN_SQL_VALUE
            if function.member == "__getattribute__":
                if (
                    len(function.items) != 1
                    or function.items[0].kind != "string"
                    or function.items[0].text is None
                    or function.entries
                    or function.risks
                ):
                    if self._contains_sql_receiver(receiver):
                        self._add_violation(trigger, "unproven-sql-call-chain")
                    return _UNKNOWN_SQL_VALUE
                return self._attribute_value(receiver, function.items[0].text)
            target = self._attribute_value(receiver, function.member)
            return self._invoke_callable_candidate(
                target,
                _CallArguments(
                    positional=function.items,
                    keywords=function.entries,
                    unknown_positional="unknown-positional" in function.risks,
                    unknown_keywords="unknown-keywords" in function.risks,
                ),
                trigger=trigger,
                depth=depth + 1,
            )
        if function.kind not in {"callable", "bound-callable"}:
            return _UNKNOWN_SQL_VALUE
        if function.reference is None:
            return _UNKNOWN_SQL_VALUE
        record = self._callables.get(function.reference)
        if record is None:
            return _FAIL_CLOSED_VALUE
        if function.kind == "bound-callable":
            call_arguments = _CallArguments(
                positional=(*function.items, *call_arguments.positional),
                keywords=call_arguments.keywords,
                unknown_positional=call_arguments.unknown_positional,
                unknown_keywords=call_arguments.unknown_keywords,
            )
        return self._analyze_deferred_callable(
            record,
            call_arguments=call_arguments,
            trigger=trigger,
            depth=depth + 1,
            commit_side_effects=True,
        )

    def _evaluate_statement_sink_call(
        self,
        function: _AbstractValue,
        call_arguments: _CallArguments,
        *,
        trigger: ast.AST,
    ) -> _AbstractValue:
        method = function.member
        arguments = call_arguments.positional
        statement_values = [
            *arguments[:1],
            *(
                value
                for name, value in call_arguments.keywords
                if name in {"statement", "query", "sql"}
            ),
        ]
        if (
            method != "execute"
            and not statement_values
            and not call_arguments.unknown_positional
        ):
            return _UNKNOWN_SQL_VALUE
        owner = function.items[0] if function.items else _UNKNOWN_VALUE
        direct_call = (
            isinstance(trigger, ast.Call)
            and isinstance(trigger.func, ast.Attribute)
            and trigger.func.attr == method
        )
        pragma_allowed = (
            method == "execute"
            and direct_call
            and _is_pragma_policy_call(
                self._path,
                trigger,
                self._parents,
                pragma_policy_path=self._pragma_policy_path,
            )
        )
        if (
            method == "execute"
            and (
                self._value_has(owner, "sqlite-connection")
                or self._value_has(owner, "sqlite-cursor")
            )
            and not pragma_allowed
        ):
            self._add_violation(trigger, "sqlite3-execute")
        receiver_proof = self._sql_receiver_proof(owner)
        if receiver_proof is _SqlReceiverProof.NON_SQL:
            return _UNKNOWN_SQL_VALUE
        if receiver_proof is _SqlReceiverProof.UNPROVEN and not pragma_allowed:
            self._add_violation(trigger, "unproven-sql-receiver")
        statement_is_proven = (
            not call_arguments.unknown_positional
            and bool(statement_values)
            and all(
                self._statement_proof(value) is _SqlStatementProof.ORM_CORE
                for value in statement_values
            )
        )
        if not pragma_allowed and not statement_is_proven:
            self._add_violation(trigger, "unproven-sql-statement")
        statement_is_raw = (
            call_arguments.unknown_positional
            or call_arguments.unknown_keywords
            or any(
                self._value_has(value, "raw-sql")
                or (value.kind == "string" and _looks_like_raw_sql(value.text or ""))
                for value in statement_values
            )
        )
        if not pragma_allowed and statement_is_raw:
            self._add_violation(trigger, "raw-sql-execute")
        if (
            receiver_proof is _SqlReceiverProof.SQLALCHEMY
            and statement_is_proven
            and not statement_is_raw
        ):
            return (
                _ORM_RESULT_COLLECTION_VALUE
                if method in {"scalars", "stream_scalars"}
                else _ORM_RESULT_VALUE
            )
        return _UNKNOWN_SQL_VALUE

    def _evaluate_callable_union(
        self,
        function: _AbstractValue,
        call_arguments: _CallArguments,
        environment: dict[str, _AbstractValue],
        *,
        trigger: ast.AST,
        depth: int,
    ) -> _AbstractValue:
        environments = self._tracked_environments(environment)
        initial_environments = self._snapshot_environments(environments)
        initial_objects = self._snapshot_object_state()
        results: list[_AbstractValue] = []
        environment_paths: list[tuple[dict[str, _AbstractValue], ...]] = []
        object_paths: list[_ObjectStateSnapshot] = []
        for candidate in function.items:
            self._restore_environments(environments, initial_environments)
            self._restore_object_state(initial_objects)
            results.append(
                self._invoke_callable_candidate(
                    candidate,
                    call_arguments,
                    trigger=trigger,
                    depth=depth,
                )
            )
            environment_paths.append(self._snapshot_environments(environments))
            object_paths.append(self._snapshot_object_state())
        self._merge_environment_snapshots(environments, tuple(environment_paths))
        self._restore_object_state(self._merge_object_states(tuple(object_paths)))
        return self._merge_return_values(tuple(results))

    def _evaluate_call(
        self,
        node: ast.Call,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> _AbstractValue:
        if isinstance(node.func, ast.Name) and node.func.id == "super":
            call_arguments = self._evaluate_call_arguments(
                node,
                environment,
                scope_name=scope_name,
                depth=depth,
            )
            return self._evaluate_super_call(call_arguments, environment)
        if isinstance(node.func, ast.Attribute):
            owner = self._evaluate(
                node.func.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            function = self._attribute_value(owner, node.func.attr)
        else:
            owner = _UNKNOWN_VALUE
            function = self._evaluate(
                node.func,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
        call_arguments = self._evaluate_call_arguments(
            node,
            environment,
            scope_name=scope_name,
            depth=depth,
        )
        arguments = call_arguments.positional

        if function.kind == "union":
            return self._evaluate_callable_union(
                function,
                call_arguments,
                environment,
                trigger=node,
                depth=depth,
            )
        if function.kind in {
            "getattr-callable",
            "operator-attrgetter-callable",
            "operator-methodcaller-callable",
            "partial-callable",
            "sql-statement-sink-callable",
        }:
            return self._invoke_callable_candidate(
                function,
                call_arguments,
                trigger=node,
                depth=depth,
            )
        if function.kind == "functools-partial":
            if not arguments:
                return _UNKNOWN_SQL_VALUE
            return _AbstractValue(
                "partial-callable",
                items=arguments,
                entries=call_arguments.keywords,
                risks=frozenset(
                    {
                        *(
                            {"unknown-positional"}
                            if call_arguments.unknown_positional
                            else set()
                        ),
                        *(
                            {"unknown-keywords"}
                            if call_arguments.unknown_keywords
                            else set()
                        ),
                    }
                ),
            )
        if function.kind in {"operator-attrgetter", "operator-methodcaller"}:
            method = (
                arguments[0].text
                if arguments
                and arguments[0].kind == "string"
                and arguments[0].text is not None
                else None
            )
            return _AbstractValue(
                (
                    "operator-attrgetter-callable"
                    if function.kind == "operator-attrgetter"
                    else "operator-methodcaller-callable"
                ),
                member=method,
                items=(
                    arguments
                    if function.kind == "operator-attrgetter"
                    else arguments[1:]
                ),
                entries=call_arguments.keywords,
                risks=frozenset(
                    {
                        *(
                            {"unknown-positional"}
                            if call_arguments.unknown_positional
                            else set()
                        ),
                        *(
                            {"unknown-keywords"}
                            if call_arguments.unknown_keywords
                            else set()
                        ),
                    }
                ),
            )
        if function.kind in {"sqlalchemy-ddl-callable", "sqlalchemy-ddl-type"}:
            self._add_violation(node, "sqlalchemy-ddl")
            self._add_violation(node, "raw-sql-executable")
            return _SQLALCHEMY_DDL_VALUE
        if function.kind == "sqlalchemy-inspect-callable":
            if (
                len(arguments) == 1
                and not call_arguments.keywords
                and not call_arguments.unknown_positional
                and not call_arguments.unknown_keywords
            ):
                return _SAFE_VALUE
            if any(self._contains_sql_receiver(value) for value in arguments):
                self._add_violation(node, "unproven-sql-call-chain")
            return _UNKNOWN_SQL_VALUE
        if function.kind == "sqlalchemy-event-listen":
            listener = (
                arguments[2]
                if len(arguments) >= 3
                else next(
                    (
                        value
                        for name, value in call_arguments.keywords
                        if name in {"fn", "listener"}
                    ),
                    _UNKNOWN_VALUE,
                )
            )
            if self._is_textual_executable(listener):
                self._add_violation(node, "sqlalchemy-event-textual-listener")
            return _SAFE_VALUE
        if function.kind == "sqlalchemy-event-listens-for":
            return _AbstractValue("sqlalchemy-event-listener-decorator")
        if function.kind == "sqlalchemy-transport-helper":
            return _SAFE_VALUE
        if function.kind == "sqlalchemy-event-listener-decorator":
            listener = arguments[0] if arguments else _UNKNOWN_VALUE
            if (
                call_arguments.unknown_positional
                or call_arguments.unknown_keywords
                or self._is_textual_executable(listener)
            ):
                self._add_violation(node, "sqlalchemy-event-textual-listener")
                return _UNKNOWN_SQL_VALUE
            return listener
        if function.kind == "orm-metadata-callable":
            return (
                _ORM_METADATA_VALUE
                if self._metadata_arguments_proven(call_arguments)
                else _UNKNOWN_SQL_VALUE
            )
        if function.kind == "orm-core-type-callable":
            return (
                _ORM_CORE_TYPE_VALUE
                if self._metadata_arguments_proven(call_arguments)
                else _UNKNOWN_SQL_VALUE
            )
        if function.kind == "orm-column-callable":
            return (
                _ORM_COLUMN_VALUE
                if self._column_arguments_proven(call_arguments)
                else _UNKNOWN_SQL_VALUE
            )
        if function.kind == "orm-table-callable":
            return (
                _ORM_TABLE_VALUE
                if self._table_arguments_proven(call_arguments)
                else _UNKNOWN_SQL_VALUE
            )
        if function.kind == "orm-statement-callable":
            return (
                _ORM_STATEMENT_VALUE
                if self._statement_constructor_arguments_proven(
                    function.member,
                    call_arguments,
                )
                else _UNKNOWN_SQL_VALUE
            )
        if function.kind == "orm-expression-callable":
            if not self._expression_arguments_proven(
                function.member,
                call_arguments,
            ):
                if function.member == "column":
                    self._add_violation(node, "sqlalchemy-textual-builder")
                return _UNKNOWN_SQL_VALUE
            if function.member == "column":
                return _ORM_COLUMN_VALUE
            if function.member == "table":
                return _ORM_TABLE_VALUE
            return _ORM_EXPRESSION_VALUE
        if function.kind == "sqlalchemy-textual-callable":
            self._add_violation(node, "sqlalchemy-textual-builder")
            return _UNKNOWN_SQL_VALUE
        if function.kind == "orm-statement-method":
            if function.member == "returning":
                self._add_violation(node, "sqlalchemy-returning")
            proven_method = (
                function.member in _ORM_STATEMENT_METHODS
                and self._statement_method_arguments_proven(
                    function.member,
                    call_arguments,
                )
            )
            return _ORM_STATEMENT_VALUE if proven_method else _UNKNOWN_SQL_VALUE
        if function.kind == "orm-result-method":
            if (
                arguments
                or call_arguments.keywords
                or call_arguments.unknown_positional
                or call_arguments.unknown_keywords
            ):
                return _UNKNOWN_SQL_VALUE
            return (
                _AbstractValue("sequence", items=(_ORM_RESULT_VALUE,))
                if function.member == "all"
                else _ORM_RESULT_VALUE
            )
        if function.kind == "orm-get-callable":
            return (
                _ORM_RESULT_VALUE
                if len(arguments) == 2
                and arguments[0].kind == "orm-model"
                and self._sql_value_proven(arguments[1])
                and not call_arguments.unknown_positional
                and not call_arguments.unknown_keywords
                else _UNKNOWN_SQL_VALUE
            )
        if function.kind == "orm-bind-type":
            return _ORM_BIND_VALUE
        if function.kind == "orm-bind-member":
            return (
                _ORM_BIND_MAPPING_VALUE
                if function.member == "as_record"
                else _ORM_BIND_VALUE
            )
        if function.kind == "orm-model":
            return _ORM_RESULT_VALUE
        if function.kind == "sqlalchemy-session-type":
            return _SQLALCHEMY_SESSION_VALUE
        if function.kind == "sqlalchemy-connection-type":
            return _SQLALCHEMY_CONNECTION_VALUE
        if function.kind == "sqlalchemy-sessionmaker":
            return _SQLALCHEMY_SESSION_FACTORY_VALUE
        if function.kind == "sqlalchemy-session-factory":
            return _SQLALCHEMY_SESSION_VALUE
        if function.kind == "sqlalchemy-session-context-callable":
            return _SQLALCHEMY_SESSION_CONTEXT_VALUE
        if function.kind == "map-callable":
            if len(arguments) < 2:
                return _UNKNOWN_SQL_VALUE
            return self._deferred_iterable_value(
                node=None,
                environment=environment,
                scope_name=scope_name,
                depth=depth,
                mapper=arguments[0],
                source=arguments[1],
            )
        if function.kind == "sequence-callable":
            if (
                len(arguments) > 1
                or call_arguments.keywords
                or call_arguments.unknown_positional
                or call_arguments.unknown_keywords
            ):
                return _UNKNOWN_SQL_VALUE
            return (
                self._consume_iterable(
                    arguments[0],
                    trigger=node,
                    depth=depth + 1,
                )
                if arguments
                else _AbstractValue("sequence")
            )
        if function.kind == "bound-string-method":
            return self._evaluate_string_method(function, arguments)
        if function.kind == "bound-mapping-get":
            key = self._literal_key(arguments[0]) if arguments else None
            default = arguments[1] if len(arguments) > 1 else _SAFE_VALUE
            entries = dict(function.entries)
            if key is not None and key in entries:
                return entries[key]
            if key is None and entries:
                value = default
                for candidate in entries.values():
                    value = _merge_values(value, candidate)
                if function.risks:
                    value = _merge_values(value, _UNKNOWN_SQL_VALUE)
                return value
            if function.risks:
                return _merge_values(default, _UNKNOWN_SQL_VALUE)
            return default

        if function.kind in {"callable", "bound-callable"} and (
            function.reference is not None
        ):
            return self._invoke_callable_candidate(
                function,
                call_arguments,
                trigger=node,
                depth=depth,
            )

        if function.kind == "class" and self._is_textual_executable(function):
            self._add_violation(node, "sqlalchemy-ddl")
            self._add_violation(node, "raw-sql-executable")
            return _SQLALCHEMY_DDL_VALUE

        if function.kind == "class" and function.reference is not None:
            instance = self._new_instance(function.reference)
            initializer = self._class_member(function.reference, "__init__")
            if (
                initializer is not None
                and initializer.kind == "callable"
                and initializer.reference is not None
            ):
                record = self._callables.get(initializer.reference)
                if record is not None:
                    self._analyze_deferred_callable(
                        record,
                        call_arguments=_CallArguments(
                            positional=(instance, *call_arguments.positional),
                            keywords=call_arguments.keywords,
                            unknown_positional=call_arguments.unknown_positional,
                            unknown_keywords=call_arguments.unknown_keywords,
                        ),
                        trigger=node,
                        depth=depth + 1,
                        commit_side_effects=True,
                    )
            return instance

        if self._value_has(function, "sqlite-connect"):
            self._add_violation(node, "sqlite3-connect")
            return _SQLITE_CONNECTION_VALUE
        if self._value_has(function, "sqlalchemy-text"):
            self._add_violation(node, "sqlalchemy-text")
            return _SQLALCHEMY_TEXTUAL_VALUE
        if self._value_has(function, "builtin-import") or self._value_has(
            function, "import-module"
        ):
            self._add_violation(node, "dynamic-import")
            target = (
                arguments[0].text
                if arguments and arguments[0].kind == "string"
                else None
            )
            if target == "sqlite3":
                return _SQLITE_MODULE_VALUE
            return _AbstractValue("unknown", risks=frozenset({"dynamic-import"}))
        if function.kind == "bound-join":
            if arguments:
                iterable = self._consume_iterable(
                    arguments[0],
                    trigger=node,
                    depth=depth + 1,
                )
            else:
                iterable = _UNKNOWN_SQL_VALUE
            if iterable.kind == "sequence":
                strings = iterable.items
                if all(value.kind == "string" for value in strings):
                    return _string_value(
                        (function.text or "").join(
                            value.text or "" for value in strings
                        )
                    )
                if any(self._value_has(value, "raw-sql") for value in strings):
                    return _UNKNOWN_SQL_VALUE
            return _UNKNOWN_SQL_VALUE
        if function.kind == "bound-format":
            positional_values: list[object] = []
            keyword_values: dict[str, object] = {}
            for argument in arguments:
                if argument.kind == "string":
                    positional_values.append(argument.text or "")
                elif argument.kind == "literal":
                    positional_values.append(argument.literal)
                else:
                    return _UNKNOWN_SQL_VALUE
            for name, argument in call_arguments.keywords:
                if argument.kind == "string":
                    keyword_values[name] = argument.text or ""
                elif argument.kind == "literal":
                    keyword_values[name] = argument.literal
                else:
                    return _UNKNOWN_SQL_VALUE
            try:
                return _string_value(
                    (function.text or "").format(
                        *positional_values,
                        **keyword_values,
                    )
                )
            except (IndexError, KeyError, TypeError, ValueError):
                return _UNKNOWN_SQL_VALUE

        if any(
            self._contains_sql_sink(value) or self._contains_sql_receiver(value)
            for value in (
                *call_arguments.positional,
                *(value for _, value in call_arguments.keywords),
            )
        ):
            self._add_violation(node, "unproven-sql-call-chain")
        if not isinstance(node.func, ast.Attribute):
            return _UNKNOWN_SQL_VALUE
        attribute = node.func.attr
        if attribute == "returning":
            self._add_violation(node, "sqlalchemy-returning")
        elif attribute == "exec_driver_sql":
            self._add_violation(node, "exec_driver_sql")
        elif attribute == "cursor":
            if not _is_pragma_policy_call(
                self._path,
                node,
                self._parents,
                pragma_policy_path=self._pragma_policy_path,
            ):
                self._add_violation(node, "sqlite3-cursor")
            return _SQLITE_CURSOR_VALUE
        elif attribute in {"executemany", "executescript"}:
            self._add_violation(node, f"sqlite3-{attribute}")
        elif attribute == "connect" and self._value_has(owner, "sqlite-module"):
            self._add_violation(node, "sqlite3-connect")
            return _SQLITE_CONNECTION_VALUE
        return _UNKNOWN_SQL_VALUE

    def _comprehension_environments(
        self,
        generators: list[ast.comprehension],
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
        leading_iterable: _AbstractValue | None = None,
    ) -> tuple[dict[str, _AbstractValue], ...]:
        environments = (self._clone_environment(environment),)
        for index, generator in enumerate(generators):
            expanded: list[dict[str, _AbstractValue]] = []
            for current in environments:
                iterable = (
                    leading_iterable
                    if index == 0 and leading_iterable is not None
                    else self._evaluate(
                        generator.iter,
                        current,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                )
                values = (
                    iterable.items
                    if iterable.kind == "sequence"
                    else (_UNKNOWN_SQL_VALUE,)
                )
                for value in values:
                    candidate = self._clone_environment(current)
                    self._bind_target(generator.target, value, candidate)
                    include = True
                    for condition in generator.ifs:
                        condition_value = self._evaluate(
                            condition,
                            candidate,
                            scope_name=scope_name,
                            depth=depth + 1,
                        )
                        if self._truthiness(condition_value) is False:
                            include = False
                            break
                    if include:
                        expanded.append(candidate)
            environments = tuple(expanded)
        return environments

    def _evaluate_sequence_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.GeneratorExp,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
        leading_iterable: _AbstractValue | None = None,
    ) -> _AbstractValue:
        environments = self._comprehension_environments(
            node.generators,
            environment,
            scope_name=scope_name,
            depth=depth,
            leading_iterable=leading_iterable,
        )
        return _AbstractValue(
            "sequence",
            items=tuple(
                self._evaluate(
                    node.elt,
                    candidate,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                for candidate in environments
            ),
        )

    def _evaluate_dict_comprehension(
        self,
        node: ast.DictComp,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> _AbstractValue:
        entries: dict[str | int, _AbstractValue] = {}
        risks: set[str] = set()
        for candidate in self._comprehension_environments(
            node.generators,
            environment,
            scope_name=scope_name,
            depth=depth,
        ):
            key_value = self._evaluate(
                node.key,
                candidate,
                scope_name=scope_name,
                depth=depth + 1,
            )
            value = self._evaluate(
                node.value,
                candidate,
                scope_name=scope_name,
                depth=depth + 1,
            )
            key = self._literal_key(key_value)
            if key is None:
                risks.update(_UNKNOWN_SQL_VALUE.risks)
            else:
                entries[key] = value
        return _AbstractValue(
            "mapping",
            entries=tuple(entries.items()),
            risks=frozenset(risks),
        )

    def _evaluate(
        self,
        node: ast.AST,
        environment: dict[str, _AbstractValue],
        *,
        scope_name: str,
        depth: int,
    ) -> _AbstractValue:
        if depth > _AST_DEPTH_BUDGET:
            self._add_violation(node, "analysis-budget")
            return _AbstractValue(
                "unknown",
                risks=frozenset(
                    {
                        "raw-sql",
                        "sqlite-module",
                        "sqlite-connect",
                        "sqlite-connection",
                        "sqlite-cursor",
                        "sqlalchemy-text",
                    }
                ),
            )
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return _string_value(node.value)
            if isinstance(node.value, (int, float, bool)) or node.value is None:
                return _AbstractValue("literal", literal=node.value)
            return _SAFE_VALUE
        if isinstance(node, ast.Name):
            value = self._read_name(environment, node.id)
            if (
                value == _UNKNOWN_VALUE
                and node.id == "getattr"
                and node.id not in environment
            ):
                return _GETATTR_CALLABLE_VALUE
            if (
                value == _UNKNOWN_VALUE
                and node.id in {"classmethod", "staticmethod"}
                and node.id not in environment
            ):
                return _SAFE_FUNCTION_DECORATOR_VALUE
            if (
                value == _UNKNOWN_VALUE
                and node.id == "property"
                and node.id not in environment
            ):
                return _PROPERTY_DECORATOR_VALUE
            if (
                value == _UNKNOWN_VALUE
                and node.id == "map"
                and node.id not in environment
            ):
                return _MAP_CALLABLE_VALUE
            if (
                value == _UNKNOWN_VALUE
                and node.id in {"list", "set", "tuple"}
                and node.id not in environment
            ):
                return _SEQUENCE_CALLABLE_VALUE
            return value
        if isinstance(node, ast.Attribute):
            owner = self._evaluate(
                node.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            return self._attribute_value(owner, node.attr)
        if isinstance(node, ast.Call):
            return self._evaluate_call(
                node,
                environment,
                scope_name=scope_name,
                depth=depth,
            )
        if isinstance(node, ast.BinOp):
            left = self._evaluate(
                node.left,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            right = self._evaluate(
                node.right,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            return self._binary_value(node.op, left, right)
        if isinstance(node, ast.UnaryOp):
            operand = self._evaluate(
                node.operand,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            if isinstance(node.op, ast.USub) and (
                operand.kind == "literal" and isinstance(operand.literal, int)
            ):
                return _AbstractValue("literal", literal=-operand.literal)
            if isinstance(node.op, ast.UAdd) and (
                operand.kind == "literal" and isinstance(operand.literal, int)
            ):
                return operand
            if isinstance(node.op, ast.Not):
                truthiness = self._truthiness(operand)
                return (
                    _AbstractValue("literal", literal=not truthiness)
                    if truthiness is not None
                    else _UNKNOWN_VALUE
                )
            return _UNKNOWN_SQL_VALUE
        if isinstance(node, ast.Compare):
            left = self._evaluate(
                node.left,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            outcomes: list[_AbstractValue] = []
            for operator, comparator in zip(
                node.ops,
                node.comparators,
                strict=True,
            ):
                right = self._evaluate(
                    comparator,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                outcome = self._comparison_value(operator, left, right)
                outcomes.append(outcome)
                if self._truthiness(outcome) is False:
                    return outcome
                left = right
            if any(outcome.kind == "orm-expression" for outcome in outcomes) and all(
                outcome.kind == "orm-expression" or self._truthiness(outcome) is True
                for outcome in outcomes
            ):
                return _ORM_EXPRESSION_VALUE
            if all(self._truthiness(outcome) is True for outcome in outcomes):
                return _AbstractValue("literal", literal=True)
            return _UNKNOWN_VALUE
        if isinstance(node, ast.BoolOp):
            possible: list[_AbstractValue] = []
            for index, expression in enumerate(node.values):
                value = self._evaluate(
                    expression,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                truthiness = self._truthiness(value)
                is_last = index == len(node.values) - 1
                if isinstance(node.op, ast.And):
                    if truthiness is False or is_last:
                        possible.append(value)
                    if truthiness is False:
                        break
                    if truthiness is None and not is_last:
                        possible.append(value)
                else:
                    if truthiness is True or is_last:
                        possible.append(value)
                    if truthiness is True:
                        break
                    if truthiness is None and not is_last:
                        possible.append(value)
            return self._merge_return_values(tuple(possible))
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    resolved = self._evaluate(
                        value.value,
                        environment,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                    parts.append(
                        resolved.text
                        if resolved.kind == "string" and resolved.text is not None
                        else "{}"
                    )
            return _string_value("".join(parts))
        if isinstance(node, (ast.Tuple, ast.List)):
            items: list[_AbstractValue] = []
            risks: set[str] = set()
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    expanded = self._evaluate(
                        item.value,
                        environment,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                    if expanded.kind == "sequence":
                        items.extend(expanded.items)
                        risks.update(expanded.risks)
                    else:
                        risks.update(_FAIL_CLOSED_VALUE.risks)
                else:
                    items.append(
                        self._evaluate(
                            item,
                            environment,
                            scope_name=scope_name,
                            depth=depth + 1,
                        )
                    )
            return _AbstractValue(
                "sequence",
                items=tuple(items),
                risks=frozenset(risks),
            )
        if isinstance(node, ast.GeneratorExp):
            leading_iterable = self._evaluate(
                node.generators[0].iter,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            return self._deferred_iterable_value(
                node=node,
                environment=environment,
                scope_name=scope_name,
                depth=depth,
                leading_iterable=leading_iterable,
            )
        if isinstance(node, (ast.ListComp, ast.SetComp)):
            return self._evaluate_sequence_comprehension(
                node,
                environment,
                scope_name=scope_name,
                depth=depth,
            )
        if isinstance(node, ast.DictComp):
            return self._evaluate_dict_comprehension(
                node,
                environment,
                scope_name=scope_name,
                depth=depth,
            )
        if isinstance(node, ast.Dict):
            entries: dict[str | int, _AbstractValue] = {}
            risks: set[str] = set()
            for key_node, value_node in zip(
                node.keys,
                node.values,
                strict=True,
            ):
                value = self._evaluate(
                    value_node,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
                if key_node is None:
                    if value.kind == "mapping":
                        expanded_keys = {key for key, _ in value.entries}
                        if value.risks:
                            for key, previous in tuple(entries.items()):
                                if key not in expanded_keys:
                                    entries[key] = _merge_values(
                                        previous,
                                        _FAIL_CLOSED_VALUE,
                                    )
                        for key, nested_value in value.entries:
                            entries[key] = nested_value
                        risks.update(value.risks)
                    else:
                        for key, previous in tuple(entries.items()):
                            entries[key] = _merge_values(
                                previous,
                                _FAIL_CLOSED_VALUE,
                            )
                        risks.update(_FAIL_CLOSED_VALUE.risks)
                    continue
                if isinstance(key_node, ast.Constant) and isinstance(
                    key_node.value,
                    (str, int),
                ):
                    key = key_node.value
                    entries[key] = value
                else:
                    self._evaluate(
                        key_node,
                        environment,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                    risks.update(_FAIL_CLOSED_VALUE.risks)
            return _AbstractValue(
                "mapping",
                entries=tuple(entries.items()),
                risks=frozenset(risks),
            )
        if isinstance(node, ast.Subscript):
            owner = self._evaluate(
                node.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            key_value = self._evaluate(
                node.slice,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            key = self._literal_key(key_value)
            if owner.kind == "sequence" and isinstance(key, int):
                if -len(owner.items) <= key < len(owner.items):
                    value = owner.items[key]
                    return (
                        _merge_values(value, _FAIL_CLOSED_VALUE)
                        if owner.risks
                        else value
                    )
                return _FAIL_CLOSED_VALUE if owner.risks else _UNKNOWN_VALUE
            if owner.kind == "sequence" and key is None:
                if not owner.items:
                    return _FAIL_CLOSED_VALUE if owner.risks else _UNKNOWN_VALUE
                value = owner.items[0]
                for candidate in owner.items[1:]:
                    value = _merge_values(value, candidate)
                return (
                    _merge_values(value, _FAIL_CLOSED_VALUE) if owner.risks else value
                )
            if owner.kind == "mapping" and key is not None:
                entries = dict(owner.entries)
                if key in entries:
                    return entries[key]
                return _FAIL_CLOSED_VALUE if owner.risks else _UNKNOWN_VALUE
            if owner.kind == "mapping" and key is None:
                value = _UNKNOWN_VALUE
                for candidate in dict(owner.entries).values():
                    value = _merge_values(value, candidate)
                return (
                    _merge_values(value, _FAIL_CLOSED_VALUE) if owner.risks else value
                )
            return _FAIL_CLOSED_VALUE if owner.risks else _UNKNOWN_VALUE
        if isinstance(node, ast.Await):
            return self._evaluate(
                node.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
        if isinstance(node, ast.IfExp):
            condition = self._evaluate(
                node.test,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            truthiness = self._truthiness(condition)
            if truthiness is True:
                return self._evaluate(
                    node.body,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
            if truthiness is False:
                return self._evaluate(
                    node.orelse,
                    environment,
                    scope_name=scope_name,
                    depth=depth + 1,
                )
            environments = self._tracked_environments(environment)
            initial_environments = self._snapshot_environments(environments)
            initial_objects = self._snapshot_object_state()
            values: list[_AbstractValue] = []
            environment_paths: list[tuple[dict[str, _AbstractValue], ...]] = []
            object_paths: list[_ObjectStateSnapshot] = []
            for expression in (node.body, node.orelse):
                self._restore_environments(environments, initial_environments)
                self._restore_object_state(initial_objects)
                values.append(
                    self._evaluate(
                        expression,
                        environment,
                        scope_name=scope_name,
                        depth=depth + 1,
                    )
                )
                environment_paths.append(self._snapshot_environments(environments))
                object_paths.append(self._snapshot_object_state())
            self._merge_environment_snapshots(environments, tuple(environment_paths))
            self._restore_object_state(self._merge_object_states(tuple(object_paths)))
            return self._merge_return_values(tuple(values))
        if isinstance(node, ast.NamedExpr):
            value = self._evaluate(
                node.value,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
            self._bind_target(node.target, value, environment)
            return value
        if isinstance(node, ast.Lambda):
            positional_defaults, keyword_defaults = self._capture_defaults(
                node.args,
                environment,
                scope_name="<lambda>",
                depth=depth,
            )
            return self._deferred_callable_value(
                node,
                positional_defaults=positional_defaults,
                keyword_defaults=keyword_defaults,
            )
        for child in ast.iter_child_nodes(node):
            self._evaluate(
                child,
                environment,
                scope_name=scope_name,
                depth=depth + 1,
            )
        return _UNKNOWN_VALUE


def _sqlite_sql_contract_violations(
    paths: tuple[Path, ...] | None = None,
    *,
    pragma_policy_path: Path = SQLITE_PRAGMA_POLICY_PATH,
) -> list[str]:
    scoped_violations: list[str] = []
    for path in _sql_contract_files() if paths is None else paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _bounded_node_count(tree) is None:
            scoped_violations.append(f"{path}:1:analysis-budget")
            continue
        scoped_violations.extend(
            _ScopedSqlContractAnalyzer(
                path=path,
                tree=tree,
                pragma_policy_path=pragma_policy_path,
            ).analyze(tree)
        )
    return scoped_violations


def _with_sqlalchemy_proofs(source: str) -> str:
    return (
        "from sqlalchemy import delete, insert, select, update\n"
        "from sqlalchemy.orm import Session\n"
        "session: Session\n"
        + _with_orm_model_proofs(_with_proven_non_sql_receivers(source))
    )


def _with_orm_model_proofs(source: str) -> str:
    return (
        "from hermes_cloud.platform.postgres.models import (\n"
        "    TenantModel as Model,\n"
        "    UserModel as OtherModel,\n"
        "    WorkspaceModel as DefaultModel,\n"
        ")\n"
        "identity = 'identity'\n"
        f"{source}"
    )


def _with_proven_non_sql_receivers(source: str) -> str:
    return (
        "class _NonSqlReceiver:\n"
        "    def execute(self, *args, **kwargs):\n"
        "        return None\n"
        "    def info(self, *args, **kwargs):\n"
        "        return None\n"
        "worker = _NonSqlReceiver()\n"
        "logger = _NonSqlReceiver()\n"
        f"{source}"
    )


def test_sqlite_behavior_depends_on_neutral_sqlalchemy_layer() -> None:
    package = Path(hermes_cloud.__file__).parent
    sqlite = package / "platform" / "sqlite"
    neutral = package / "platform" / "sqlalchemy"

    assert (neutral / "runtime.py").is_file()
    assert (neutral / "repositories" / "identity.py").is_file()
    assert (neutral / "repositories" / "projection.py").is_file()

    for source in sqlite.rglob("*.py"):
        text = source.read_text()
        assert "platform.postgres.runtime" not in text
        assert "platform.postgres.repositories" not in text

    identity = (neutral / "repositories" / "identity.py").read_text()
    projection = (neutral / "repositories" / "projection.py").read_text()
    runtime = (neutral / "runtime.py").read_text()
    assert "class SqlAlchemyIdentityRepositoryBase" in identity
    assert "class SqlAlchemySessionProjectionRepositoryBase" in projection
    assert "class OperationScopedIdentityRepository" in runtime
    assert "class OperationScopedSessionProjectionRepository" in runtime
    assert "class SqlAlchemyLoginTenantResolver" in runtime


def test_sqlite_catalog_compatibility_source_is_explicitly_frozen() -> None:
    package = Path(hermes_cloud.__file__).parent
    architecture = package / "platform" / "sqlite" / "README.md"

    text = architecture.read_text()
    normalized = " ".join(text.lower().split())
    assert "frozen shared catalog compatibility source" in normalized
    assert "platform.postgres.models" in text
    assert "runtime" in text
    assert "repositories" in text


def test_sqlite_and_neutral_provider_files_have_an_ast_sql_contract_gate() -> None:
    scanner = globals().get("_sqlite_sql_contract_violations")

    assert scanner is not None
    assert scanner() == []


def test_sqlite_ast_contract_gate_proves_shared_ticket_consumption_scope(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "shared_ticket_consumption_scope.py"
    fixture.write_text(
        "from datetime import datetime\n"
        "from sqlalchemy import update\n"
        "from sqlalchemy.orm import Session\n"
        "from hermes_cloud.modules.identity.domain import WebSocketTicketClaim\n"
        "from hermes_cloud.platform.postgres.models import WebSocketTicketModel\n"
        "from hermes_cloud.platform.sqlalchemy.repositories.identity import "
        "ticket_consumption_scope\n"
        "def consume(db: Session, claim: WebSocketTicketClaim, now: datetime):\n"
        "    statement = update(WebSocketTicketModel).where(\n"
        "        ticket_consumption_scope(claim, now=now)\n"
        "    ).values(consumed_at=now)\n"
        "    db.execute(statement)\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


def test_sqlite_ast_contract_gate_proves_shared_transport_cursor_helpers(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "shared_transport_cursor_helpers.py"
    fixture.write_text(
        "from datetime import datetime, timedelta\n"
        "from sqlalchemy.orm import Session\n"
        "from hermes_cloud.domain.connector_gateway import ConnectorIdentity\n"
        "from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (\n"
        "    advance_locked_connector_transport_cursor,\n"
        "    lock_connector_transport_cursor,\n"
        ")\n"
        "def advance(db: Session, identity: ConnectorIdentity, now: datetime):\n"
        "    locked = lock_connector_transport_cursor(\n"
        "        db, identity=identity, connection_id='connection',\n"
        "        connector_instance_id='instance', runtime_generation='runtime',\n"
        "        expected_next_connector_sequence=1,\n"
        "        expected_next_cloud_sequence=1, now=now,\n"
        "    )\n"
        "    advance_locked_connector_transport_cursor(\n"
        "        db, locked=locked, next_connector_sequence=2,\n"
        "        next_cloud_sequence=1, now=now, ownership_lease=timedelta(seconds=1),\n"
        "    )\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


def test_sqlite_ast_contract_gate_accepts_normalized_neutral_model_binds(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "normalized_neutral_model_binds.py"
    fixture.write_text(
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "from hermes_cloud.platform.sqlalchemy.observer_projection_models "
        "import ObserverSessionModel\n"
        "class Payload:\n"
        "    profile = object()\n"
        "    event_sequence = object()\n"
        "payload = Payload()\n"
        "session: Session\n"
        "profile = str(payload.profile)\n"
        "event_sequence = int(payload.event_sequence)\n"
        "statement = select(ObserverSessionModel).where(\n"
        "    ObserverSessionModel.profile == profile,\n"
        "    ObserverSessionModel.event_sequence == event_sequence,\n"
        ")\n"
        "session.execute(statement)\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


def test_sqlite_ast_contract_gate_accepts_typed_sqlalchemy_inspection(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "typed_sqlalchemy_inspection.py"
    fixture.write_text(
        "from sqlalchemy import Connection, inspect\n"
        "connection: Connection\n"
        "inspector = inspect(connection)\n"
        "inspector.get_table_names()\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    ("source", "violation"),
    (
        (
            "import sqlite3\nconnection = sqlite3.connect('database.sqlite3')\n",
            "sqlite3-connect",
        ),
        (
            "cursor = connection.cursor()\n",
            "sqlite3-cursor",
        ),
        (
            "cursor = connection.cursor()\ncursor.execute(statement)\n",
            "sqlite3-execute",
        ),
        (
            "from sqlalchemy import text\nstatement = text('SELECT 1')\n",
            "sqlalchemy-text",
        ),
        (
            "from sqlalchemy.sql import text\nstatement = text('SELECT 1')\n",
            "sqlalchemy-text",
        ),
        (
            ("import sqlalchemy.sql as sa_sql\nstatement = sa_sql.text('SELECT 1')\n"),
            "sqlalchemy-text",
        ),
        (
            (
                "import sqlalchemy.sql as sa_sql\n"
                "make_text = sa_sql.text\n"
                "raw_text = make_text\n"
                "statement = raw_text('SELECT 1')\n"
            ),
            "sqlalchemy-text",
        ),
        (
            (
                "import sqlite3\n"
                "open_db = sqlite3.connect\n"
                "connection = open_db('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "import sqlite3\n"
                "open_db = sqlite3.connect\n"
                "alias = open_db\n"
                "connection = alias('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "database_module = __import__('sqlite3')\n"
                "connection = database_module.connect('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "import importlib\n"
                "database_module = importlib.import_module('sqlite3')\n"
                "connection = database_module.connect('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "module_name = configured_module()\n"
                "database_module = __import__(module_name)\n"
            ),
            "dynamic-import",
        ),
        (
            "connection.exec_driver_sql('SELECT 1')\n",
            "exec_driver_sql",
        ),
        (
            "session.execute('SELECT 1')\n",
            "raw-sql-execute",
        ),
        (
            "session.execute(f'SELECT {column}')\n",
            "raw-sql-execute",
        ),
        (
            "query = 'SELECT 1'\nsession.execute(query)\n",
            "raw-sql-execute",
        ),
        (
            ("query = 'SELECT 1'\nraw_query = query\nsession.execute(raw_query)\n"),
            "raw-sql-execute",
        ),
        (
            ("query = ''.join(('SELECT', ' 1'))\nsession.execute(query)\n"),
            "raw-sql-execute",
        ),
        (
            (
                "prefix = 'SELECT'\n"
                "suffix = ' 1'\n"
                "query = prefix + suffix\n"
                "session.execute(query)\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "parts = ('SELECT', ' 1')\n"
                "query = ''.join(parts)\n"
                "session.execute(query)\n"
            ),
            "raw-sql-execute",
        ),
        (
            "query = 'SEL'\nquery += 'ECT 1'\nsession.execute(query)\n",
            "raw-sql-execute",
        ),
        (
            "prefix = 'SELECT'\nsession.execute(f'{prefix} 1')\n",
            "raw-sql-execute",
        ),
        (
            "session.execute('SELECT %s' % column)\n",
            "raw-sql-execute",
        ),
        (
            "template = 'SELECT {}'\nsession.execute(template.format(column))\n",
            "raw-sql-execute",
        ),
        (
            ("joiner = ''.join\nsession.execute(joiner(('SELECT', ' 1')))\n"),
            "raw-sql-execute",
        ),
        (
            ("for raw_query in ('SELECT 1',):\n    session.execute(raw_query)\n"),
            "raw-sql-execute",
        ),
        (
            "statement = insert(Model).returning(Model)\n",
            "sqlalchemy-returning",
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_each_escape_hatch(
    tmp_path: Path,
    source: str,
    violation: str,
) -> None:
    fixture = tmp_path / "violation.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(violation in item for item in violations)


def test_sqlite_ast_contract_gate_allows_only_the_central_exact_pragma(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "engine.py"
    policy.write_text(
        "_SQLITE_FOREIGN_KEYS_PRAGMA = 'PRAGMA foreign_keys=ON'\n"
        "def _configure_sqlite_pragma_policy(connection):\n"
        "    cursor = connection.cursor()\n"
        "    try:\n"
        "        cursor.execute(_SQLITE_FOREIGN_KEYS_PRAGMA)\n"
        "    finally:\n"
        "        cursor.close()\n",
        encoding="utf-8",
    )

    assert (
        _sqlite_sql_contract_violations(
            (policy,),
            pragma_policy_path=policy,
        )
        == []
    )

    policy.write_text(
        "_SQLITE_FOREIGN_KEYS_PRAGMA = 'PRAGMA journal_mode=WAL'\n"
        "def _configure_sqlite_pragma_policy(connection):\n"
        "    cursor = connection.cursor()\n"
        "    cursor.execute(_SQLITE_FOREIGN_KEYS_PRAGMA)\n",
        encoding="utf-8",
    )
    assert any(
        "sqlite3-execute" in violation
        for violation in _sqlite_sql_contract_violations(
            (policy,),
            pragma_policy_path=policy,
        )
    )


def test_sqlite_ast_contract_gate_ignores_docstrings_and_messages(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "messages.py"
    fixture.write_text(
        '"""No sqlite3 cursor, raw SQL, or RETURNING is allowed here."""\n'
        "MESSAGE = \"session.execute('SELECT 1') is forbidden\"\n"
        "def describe():\n"
        "    return 'exec_driver_sql and sqlalchemy.text are forbidden'\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        "import sqlalchemy.sql as sa_sql\nstatement = sa_sql.select(Model)\n",
        "import sqlite3\nopen_db = sqlite3.connect\nlogger.info(open_db)\n",
        "label = 'readiness healthy'\ncopy = label\nlogger.info(copy)\n",
        "label = ''.join(('readiness', ' healthy'))\nworker.execute(label)\n",
        "statement = select(Model)\nsession.execute(statement)\n",
        "for statement in (select(Model),):\n    session.execute(statement)\n",
    ),
)
def test_sqlite_ast_contract_gate_allows_non_violation_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "allowed.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        "__import__('sqlite3').connect('database.sqlite3')\n",
        (
            "import importlib\n"
            "importlib.import_module('sqlite3').connect('database.sqlite3')\n"
        ),
        (
            "from importlib import import_module as load\n"
            "load('sqlite3').connect('database.sqlite3')\n"
        ),
        ("module = 'sqlite3'\n__import__(module).connect('database.sqlite3')\n"),
        (
            "import importlib\n"
            "loader = importlib.import_module\n"
            "loader('sqlite3').connect('database.sqlite3')\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_direct_dynamic_sqlite_connect(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "dynamic_connect.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("sqlite3-connect" in item for item in violations)


@pytest.mark.parametrize("alias_count", (1200, 2000))
def test_sqlite_ast_contract_gate_resolves_deep_aliases_without_recursion(
    tmp_path: Path,
    alias_count: int,
) -> None:
    fixture = tmp_path / "deep_alias.py"
    source = ["query_0 = 'SELECT 1'"]
    source.extend(
        f"query_{index} = query_{index - 1}" for index in range(1, alias_count + 1)
    )
    source.append(f"session.execute(query_{alias_count})")
    fixture.write_text("\n".join(source), encoding="utf-8")

    started_at = monotonic()
    try:
        violations = _sqlite_sql_contract_violations((fixture,))
    except RecursionError:
        pytest.fail("deep aliases must not exhaust Python recursion")
    elapsed = monotonic() - started_at

    assert any("raw-sql-execute" in item for item in violations)
    assert elapsed < 5.0


def test_sqlite_ast_contract_gate_fails_closed_within_node_budget(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "oversized.py"
    fixture.write_text(
        "\n".join(f"value_{index} = {index}" for index in range(7000)),
        encoding="utf-8",
    )

    started_at = monotonic()
    violations = _sqlite_sql_contract_violations((fixture,))
    elapsed = monotonic() - started_at

    assert any("analysis-budget" in item for item in violations)
    assert elapsed < 5.0


@pytest.mark.parametrize(
    "source",
    (
        (
            "import sqlite3 as client\n"
            "client = safe_client\n"
            "client.connect('database.sqlite3')\n"
        ),
        (
            "from sqlite3 import connect\n"
            "def connect(path):\n"
            "    return safe_connect(path)\n"
            "connect('database.sqlite3')\n"
        ),
        (
            "from sqlalchemy import text as make_text\n"
            "make_text = safe_text\n"
            "make_text('readiness healthy')\n"
        ),
        (
            "from importlib import import_module as load\n"
            "load = safe_loader\n"
            "load('sqlite3').connect('database.sqlite3')\n"
        ),
        ("def prepare():\n    statement = 'SELECT 1'\nworker.execute(statement)\n"),
        ("class Holder:\n    statement = 'SELECT 1'\nworker.execute(statement)\n"),
    ),
)
def test_sqlite_ast_contract_gate_respects_scope_and_safe_rebinding(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "scoped_control.py"
    fixture.write_text(
        _with_proven_non_sql_receivers(source),
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    ("source", "violation"),
    (
        (
            (
                "def run():\n"
                "    session.execute(statement)\n"
                "statement = 'SELECT 1'\n"
                "run()\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "def run():\n"
                "    client.connect('database.sqlite3')\n"
                "import sqlite3 as client\n"
                "run()\n"
            ),
            "sqlite3-connect",
        ),
        (
            ("def run():\n    text('SELECT 1')\nfrom sqlalchemy import text\nrun()\n"),
            "sqlalchemy-text",
        ),
        (
            (
                "async def run():\n"
                "    session.execute(statement)\n"
                "statement = 'SELECT 1'\n"
                "await run()\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "async def run():\n"
                "    client.connect('database.sqlite3')\n"
                "import sqlite3 as client\n"
                "await run()\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "async def run():\n"
                "    text('SELECT 1')\n"
                "from sqlalchemy import text\n"
                "await run()\n"
            ),
            "sqlalchemy-text",
        ),
    ),
)
def test_sqlite_ast_contract_gate_resolves_function_globals_at_call_time(
    tmp_path: Path,
    source: str,
    violation: str,
) -> None:
    fixture = tmp_path / "late_bound_function.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(violation in item for item in violations)


@pytest.mark.parametrize(
    ("source", "violation"),
    (
        (
            (
                "run = lambda: session.execute(statement)\n"
                "statement = 'SELECT 1'\n"
                "run()\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "run = lambda: client.connect('database.sqlite3')\n"
                "import sqlite3 as client\n"
                "run()\n"
            ),
            "sqlite3-connect",
        ),
        (
            ("run = lambda: text('SELECT 1')\nfrom sqlalchemy import text\nrun()\n"),
            "sqlalchemy-text",
        ),
    ),
)
def test_sqlite_ast_contract_gate_resolves_lambda_globals_at_call_time(
    tmp_path: Path,
    source: str,
    violation: str,
) -> None:
    fixture = tmp_path / "late_bound_lambda.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(violation in item for item in violations)


def test_sqlite_ast_contract_gate_class_body_falls_back_to_global_before_binding(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "class_global_fallback.py"
    fixture.write_text(
        "statement = 'SELECT 1'\n"
        "class Holder:\n"
        "    session.execute(statement)\n"
        "    statement = select(Model)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


def test_sqlite_ast_contract_gate_class_local_binding_shadows_global_in_order(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "class_local_binding.py"
    fixture.write_text(
        _with_sqlalchemy_proofs(
            "statement = 'SELECT 1'\n"
            "class Holder:\n"
            "    statement = select(Model)\n"
            "    session.execute(statement)\n"
        ),
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    ("source", "expect_raw_sql"),
    (
        (
            (
                "def run():\n"
                "    session.execute(statement)\n"
                "run()\n"
                "statement = 'SELECT 1'\n"
            ),
            False,
        ),
        (
            (
                "def run():\n"
                "    session.execute(statement)\n"
                "statement = 'SELECT 1'\n"
                "run()\n"
            ),
            True,
        ),
        (
            ("def run():\n    session.execute(statement)\nstatement = 'SELECT 1'\n"),
            True,
        ),
    ),
)
def test_sqlite_ast_contract_gate_defines_call_time_and_uncalled_policy(
    tmp_path: Path,
    source: str,
    expect_raw_sql: bool,
) -> None:
    fixture = tmp_path / "function_call_policy.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))
    has_raw_sql = any("raw-sql-execute" in item for item in violations)

    assert has_raw_sql is expect_raw_sql


@pytest.mark.parametrize(
    "source",
    (
        ("def run(statement='SELECT 1'):\n    session.execute(statement)\nrun()\n"),
        ("def run(*, statement='SELECT 1'):\n    session.execute(statement)\nrun()\n"),
        ("run = lambda statement='SELECT 1': session.execute(statement)\nrun()\n"),
        ("def run(statement='SELECT 1'):\n    session.execute(statement)\n"),
    ),
)
def test_sqlite_ast_contract_gate_binds_dangerous_defaults(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "dangerous_default.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    ("source", "expect_raw_sql"),
    (
        (
            (
                "def run(statement=select(Model)):\n"
                "    session.execute(statement)\n"
                "run()\n"
            ),
            False,
        ),
        (
            (
                "def run(statement='SELECT 1'):\n"
                "    session.execute(statement)\n"
                "run(select(Model))\n"
            ),
            False,
        ),
        (
            (
                "def run(statement=select(Model)):\n"
                "    session.execute(statement)\n"
                "run('SELECT 1')\n"
            ),
            True,
        ),
        (
            (
                "statement = 'SELECT 1'\n"
                "def run(value=statement):\n"
                "    session.execute(value)\n"
                "statement = select(Model)\n"
                "run()\n"
            ),
            True,
        ),
        (
            (
                "statement = select(Model)\n"
                "def run(value=statement):\n"
                "    session.execute(value)\n"
                "statement = 'SELECT 1'\n"
                "run()\n"
            ),
            False,
        ),
    ),
)
def test_sqlite_ast_contract_gate_defaults_capture_and_override_in_python_order(
    tmp_path: Path,
    source: str,
    expect_raw_sql: bool,
) -> None:
    fixture = tmp_path / "default_binding_order.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))
    has_raw_sql = any("raw-sql-execute" in item for item in violations)

    assert has_raw_sql is expect_raw_sql


@pytest.mark.parametrize(
    ("source", "violation"),
    (
        (
            (
                "statement = 'SELECT 1'\n"
                "def mutate():\n"
                "    global statement\n"
                "    session.execute(statement)\n"
                "    statement = select(Model)\n"
                "mutate()\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "def outer():\n"
                "    statement = 'SELECT 1'\n"
                "    def mutate():\n"
                "        nonlocal statement\n"
                "        session.execute(statement)\n"
                "        statement = select(Model)\n"
                "    mutate()\n"
                "outer()\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "statement = select(Model)\n"
                "def mutate():\n"
                "    global statement\n"
                "    statement = 'SELECT 1'\n"
                "mutate()\n"
                "session.execute(statement)\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "def outer():\n"
                "    statement = select(Model)\n"
                "    def mutate():\n"
                "        nonlocal statement\n"
                "        statement = 'SELECT 1'\n"
                "    mutate()\n"
                "    session.execute(statement)\n"
                "outer()\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "client = safe_client\n"
                "def mutate():\n"
                "    global client\n"
                "    import sqlite3 as client\n"
                "mutate()\n"
                "client.connect('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "def outer():\n"
                "    client = safe_client\n"
                "    def mutate():\n"
                "        nonlocal client\n"
                "        import sqlite3 as client\n"
                "    mutate()\n"
                "    client.connect('database.sqlite3')\n"
                "outer()\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "make_text = safe_text\n"
                "def mutate():\n"
                "    global make_text\n"
                "    from sqlalchemy import text as make_text\n"
                "mutate()\n"
                "make_text('SELECT 1')\n"
            ),
            "sqlalchemy-text",
        ),
        (
            (
                "def outer():\n"
                "    make_text = safe_text\n"
                "    def mutate():\n"
                "        nonlocal make_text\n"
                "        from sqlalchemy import text as make_text\n"
                "    mutate()\n"
                "    make_text('SELECT 1')\n"
                "outer()\n"
            ),
            "sqlalchemy-text",
        ),
    ),
)
def test_sqlite_ast_contract_gate_routes_global_and_nonlocal_to_owner_scope(
    tmp_path: Path,
    source: str,
    violation: str,
) -> None:
    fixture = tmp_path / "owner_scope_mutation.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(violation in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = 'SELECT 1'\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = select(Model)\n"
            "mutate()\n"
            "session.execute(statement)\n"
        ),
        (
            "def outer():\n"
            "    statement = 'SELECT 1'\n"
            "    def mutate():\n"
            "        nonlocal statement\n"
            "        statement = select(Model)\n"
            "    mutate()\n"
            "    session.execute(statement)\n"
            "outer()\n"
        ),
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "session.execute(statement)\n"
            "mutate()\n"
        ),
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "session.execute(statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_owner_scope_safe_overwrite_and_call_order(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "owner_scope_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    ("source", "violation"),
    (
        (
            (
                "def build_statement():\n"
                "    return 'SELECT 1'\n"
                "session.execute(build_statement())\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "async def build_statement():\n"
                "    return 'SELECT 1'\n"
                "session.execute(await build_statement())\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "build_statement = lambda: 'SELECT 1'\n"
                "session.execute(build_statement())\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "def outer():\n"
                "    def middle():\n"
                "        def inner():\n"
                "            return 'SELECT 1'\n"
                "        return inner\n"
                "    return middle\n"
                "session.execute(outer()()())\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "def database_module():\n"
                "    import sqlite3\n"
                "    return sqlite3\n"
                "database_module().connect('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "def text_factory():\n"
                "    from sqlalchemy import text\n"
                "    return text\n"
                "text_factory()('SELECT 1')\n"
            ),
            "sqlalchemy-text",
        ),
        (
            (
                "def connect_factory():\n"
                "    from sqlite3 import connect\n"
                "    return connect\n"
                "connect_factory()('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "def build_statement(enabled):\n"
                "    if enabled:\n"
                "        return select(Model)\n"
                "    return 'SELECT 1'\n"
                "session.execute(build_statement(configured()))\n"
            ),
            "raw-sql-execute",
        ),
    ),
)
def test_sqlite_ast_contract_gate_propagates_callable_return_values(
    tmp_path: Path,
    source: str,
    violation: str,
) -> None:
    fixture = tmp_path / "callable_return.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(violation in item for item in violations)


def test_sqlite_ast_contract_gate_bounds_recursive_return_analysis(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "recursive_return.py"
    fixture.write_text(
        "def direct():\n"
        "    return direct()\n"
        "def first():\n"
        "    return second()\n"
        "def second():\n"
        "    return first()\n"
        "session.execute(direct())\n"
        "session.execute(first())\n",
        encoding="utf-8",
    )

    started_at = monotonic()
    violations = _sqlite_sql_contract_violations((fixture,))
    elapsed = monotonic() - started_at

    assert any("analysis-budget" in item for item in violations)
    assert any("raw-sql-execute" in item for item in violations)
    assert elapsed < 5.0


def test_sqlite_ast_contract_gate_treats_no_return_as_safe_none(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "no_return.py"
    fixture.write_text(
        _with_sqlalchemy_proofs(
            "def notify():\n"
            "    worker.execute(select(Model))\n"
            "worker.execute(notify())\n"
            "logger.info(notify())\n"
        ),
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        ("def run(*args):\n    session.execute(args[0])\nrun(*('SELECT 1',))\n"),
        (
            "def run(**kwargs):\n"
            "    session.execute(kwargs['statement'])\n"
            "run(**{'statement': 'SELECT 1'})\n"
        ),
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "arguments = (('SELECT 1',),)\n"
            "run(*arguments[0])\n"
        ),
        (
            "def run(**kwargs):\n"
            "    session.execute(kwargs['payload']['statement'])\n"
            "run(**{'payload': {'statement': 'SELECT 1'}})\n"
        ),
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(*configured_arguments())\n"
        ),
        (
            "def run(*, statement):\n"
            "    session.execute(statement)\n"
            "run(**configured_arguments())\n"
        ),
        "session.execute(**{'statement': 'SELECT 1'})\n",
        "session.execute(*configured_arguments())\n",
        "session.execute(**configured_arguments())\n",
        (
            "arguments = (*configured_arguments(), select(Model))\n"
            "session.execute(arguments[0])\n"
        ),
        (
            "payload = {'statement': select(Model), **configured_arguments()}\n"
            "session.execute(payload['statement'])\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_binds_expanded_call_arguments(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "expanded_arguments.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(*('SELECT 1',), **{'statement': select(Model)})\n"
        ),
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(select(Model), **{'statement': 'SELECT 1'})\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_merges_duplicate_call_bindings_conservatively(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "duplicate_call_binding.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(statement=select(Model), 'SELECT 1')\n"
        ),
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(statement=select(Model), value for value in values)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_invalid_python_call_syntax(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "invalid_call_syntax.py"
    fixture.write_text(source, encoding="utf-8")

    with pytest.raises(SyntaxError):
        _sqlite_sql_contract_violations((fixture,))


@pytest.mark.parametrize(
    "source",
    (
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(*(select(Model),))\n"
        ),
        (
            "def run(*args, **kwargs):\n"
            "    worker.execute(args[0])\n"
            "    worker.execute(kwargs['statement'])\n"
            "run(*configured_arguments(), **configured_keywords())\n"
        ),
        (
            "worker.execute(*configured_arguments())\n"
            "logger.info(*configured_arguments(), **configured_keywords())\n"
        ),
        (
            "payload = {**configured_arguments(), 'statement': select(Model)}\n"
            "session.execute(payload['statement'])\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_safe_expansion_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "safe_expansion.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    ("source", "violation"),
    (
        (
            (
                "statement = select(Model)\n"
                "class Mutator:\n"
                "    @staticmethod\n"
                "    def mutate():\n"
                "        global statement\n"
                "        statement = 'SELECT 1'\n"
                "Mutator.mutate()\n"
                "session.execute(statement)\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "class Mutator:\n"
                "    @classmethod\n"
                "    def database_module(cls):\n"
                "        import sqlite3\n"
                "        return sqlite3\n"
                "Mutator.database_module().connect('database.sqlite3')\n"
            ),
            "sqlite3-connect",
        ),
        (
            (
                "statement = select(Model)\n"
                "class Mutator:\n"
                "    def mutate(self):\n"
                "        global statement\n"
                "        statement = 'SELECT 1'\n"
                "instance = Mutator()\n"
                "instance.mutate()\n"
                "session.execute(statement)\n"
            ),
            "raw-sql-execute",
        ),
        (
            (
                "class StatementFactory:\n"
                "    def build(self):\n"
                "        return 'SELECT 1'\n"
                "session.execute(StatementFactory().build())\n"
            ),
            "raw-sql-execute",
        ),
    ),
)
def test_sqlite_ast_contract_gate_models_class_and_method_calls(
    tmp_path: Path,
    source: str,
    violation: str,
) -> None:
    fixture = tmp_path / "method_call.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(violation in item for item in violations)


def test_sqlite_ast_contract_gate_isolates_never_called_method_side_effects(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "uncalled_method.py"
    fixture.write_text(
        _with_sqlalchemy_proofs(
            "statement = select(Model)\n"
            "class Mutator:\n"
            "    def mutate(self):\n"
            "        global statement\n"
            "        session.execute('SELECT inside method')\n"
            "        statement = 'SELECT outside method'\n"
            "session.execute(statement)\n"
        ),
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert sum("raw-sql-execute" in item for item in violations) == 1


def test_sqlite_ast_contract_gate_uses_lexical_nonlocal_owner(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "lexical_nonlocal.py"
    fixture.write_text(
        "def outer():\n"
        "    statement = select(Model)\n"
        "    def middle():\n"
        "        def mutate():\n"
        "            nonlocal statement\n"
        "            statement = 'SELECT 1'\n"
        "        mutate()\n"
        "    middle()\n"
        "    session.execute(statement)\n"
        "outer()\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "def outer():\n"
            "    statement = 'SELECT 1'\n"
            "    def middle():\n"
            "        def mutate():\n"
            "            nonlocal statement\n"
            "            statement = select(Model)\n"
            "        mutate()\n"
            "    middle()\n"
            "    session.execute(statement)\n"
            "outer()\n"
        ),
        (
            "def outer():\n"
            "    statement = select(Model)\n"
            "    def middle():\n"
            "        def mutate():\n"
            "            nonlocal statement\n"
            "            statement = 'SELECT 1'\n"
            "    middle()\n"
            "    session.execute(statement)\n"
            "outer()\n"
        ),
        (
            "def outer():\n"
            "    statement = select(Model)\n"
            "    def middle():\n"
            "        statement = 'SELECT shadow'\n"
            "        def read():\n"
            "            nonlocal statement\n"
            "            worker.execute(statement)\n"
            "        read()\n"
            "    middle()\n"
            "    session.execute(statement)\n"
            "outer()\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_lexical_nonlocal_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "lexical_nonlocal_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        ("statements = (select(Model), 'SELECT 1')\nsession.execute(statements[-1])\n"),
        (
            "index = 1\n"
            "statements = (select(Model), 'SELECT 1')\n"
            "session.execute(statements[index])\n"
        ),
        (
            "index = -1\n"
            "statements = (select(Model), 'SELECT 1')\n"
            "session.execute(statements[index])\n"
        ),
        "session.execute(False or 'SELECT 1')\n",
        "session.execute(True and 'SELECT 1')\n",
        "session.execute(configured() and 'SELECT 1')\n",
        (
            "statements = [value for value in ('SELECT 1',)]\n"
            "session.execute(statements[0])\n"
        ),
        (
            "payload = {key: value for key, value in "
            "(('statement', 'SELECT 1'),)}\n"
            "session.execute(payload.get('statement'))\n"
        ),
        (
            "parts = {value for value in ('SELECT 1',)}\n"
            "session.execute(''.join(parts))\n"
        ),
        (
            "statements = [value for value in ('SELECT 1',) if configured()]\n"
            "session.execute(statements[0])\n"
        ),
        (
            "statements = [prefix + suffix for prefix in ('SEL',) "
            "for suffix in ('ECT 1',)]\n"
            "session.execute(statements[0])\n"
        ),
        "session.execute({}.get('statement', 'SELECT 1'))\n",
        "session.execute(' select 1 '.strip())\n",
        "session.execute('select 1'.upper())\n",
        "session.execute('SAFE 1'.replace('SAFE', 'SELECT'))\n",
        "session.execute('xSELECT 1'.removeprefix('x'))\n",
        "session.execute('{} 1'.format('SELECT'))\n",
        "session.execute('%s 1' % 'SELECT')\n",
        "session.execute(configured_statement())\n",
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(configured_statement())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_interprets_general_expressions(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "general_expression.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        "worker.execute(False and 'SELECT 1')\n",
        "worker.execute(True or 'SELECT 1')\n",
        (
            "statements = [select(Model) for value in (1,)]\n"
            "session.execute(statements[0])\n"
        ),
        (
            "payload = {'statement': select(Model)}\n"
            "session.execute(payload.get('statement'))\n"
        ),
        (
            "payload = {**configured_payload(), 'statement': select(Model)}\n"
            "session.execute(payload.get('statement'))\n"
        ),
        "worker.execute(configured_statement())\n",
        "logger.info(configured_statement())\n",
        "session.execute(select(Model))\n",
    ),
)
def test_sqlite_ast_contract_gate_allows_safe_general_expression_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "general_expression_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "match configured():\n"
            "    case 1:\n"
            "        statement = 'SELECT 1'\n"
            "    case _:\n"
            "        statement = select(Model)\n"
            "session.execute(statement)\n"
        ),
        (
            "match 1:\n"
            "    case 1:\n"
            "        statement = 'SELECT 1'\n"
            "    case _:\n"
            "        statement = select(Model)\n"
            "session.execute(statement)\n"
        ),
        (
            "match {'statement': 'SELECT 1'}:\n"
            "    case {'statement': statement}:\n"
            "        session.execute(statement)\n"
        ),
        (
            "match ('SELECT 1',):\n"
            "    case (statement,):\n"
            "        session.execute(statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_analyzes_match_cases(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "match_case.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


def test_sqlite_ast_contract_gate_prunes_unreachable_match_case(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "match_case_control.py"
    fixture.write_text(
        _with_sqlalchemy_proofs(
            "match 1:\n"
            "    case 2:\n"
            "        statement = 'SELECT 1'\n"
            "    case _:\n"
            "        statement = select(Model)\n"
            "session.execute(statement)\n"
        ),
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "class Query:\n"
            "    statement = select(Model)\n"
            "Query.statement = 'SELECT 1'\n"
            "session.execute(Query.statement)\n"
        ),
        (
            "class Query:\n"
            "    pass\n"
            "query = Query()\n"
            "query.statement = 'SELECT 1'\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class Query:\n"
            "    def prepare(self):\n"
            "        self.statement = 'SELECT 1'\n"
            "query = Query()\n"
            "query.prepare()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class Query:\n"
            "    statement = select(Model)\n"
            "    @classmethod\n"
            "    def prepare(cls):\n"
            "        cls.statement = 'SELECT 1'\n"
            "Query.prepare()\n"
            "session.execute(Query.statement)\n"
        ),
        (
            "class Query:\n"
            "    statement = 'SELECT 1'\n"
            "    @classmethod\n"
            "    def run(cls):\n"
            "        session.execute(cls.statement)\n"
            "Query.run()\n"
        ),
        (
            "class Query:\n"
            "    pass\n"
            "class Holder:\n"
            "    def prepare(self):\n"
            "        self.query = Query()\n"
            "        self.query.statement = 'SELECT 1'\n"
            "holder = Holder()\n"
            "holder.prepare()\n"
            "session.execute(holder.query.statement)\n"
        ),
        (
            "class Query:\n"
            "    pass\n"
            "query = Query()\n"
            "query.statement = 'SEL'\n"
            "query.statement += 'ECT 1'\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class Query:\n"
            "    statement = select(Model)\n"
            "if configured():\n"
            "    Query.statement = 'SELECT 1'\n"
            "else:\n"
            "    Query.statement = select(Model)\n"
            "session.execute(Query.statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_tracks_attribute_assignment_state(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "attribute_state.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "class Base:\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return 'SELECT 1'\n"
            "class Child(Base):\n"
            "    pass\n"
            "session.execute(Child.build())\n"
        ),
        (
            "class Base:\n"
            "    statement = select(Model)\n"
            "    @classmethod\n"
            "    def prepare(cls):\n"
            "        cls.statement = 'SELECT 1'\n"
            "class Child(Base):\n"
            "    pass\n"
            "Child.prepare()\n"
            "session.execute(Child.statement)\n"
        ),
        (
            "class Base:\n"
            "    def prepare(self):\n"
            "        self.statement = 'SELECT 1'\n"
            "class Child(Base):\n"
            "    pass\n"
            "query = Child()\n"
            "query.prepare()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "class Base:\n"
            "    @staticmethod\n"
            "    def prepare():\n"
            "        global statement\n"
            "        statement = 'SELECT 1'\n"
            "class Child(Base):\n"
            "    pass\n"
            "Child.prepare()\n"
            "session.execute(statement)\n"
        ),
        (
            "def outer():\n"
            "    statement = select(Model)\n"
            "    class Base:\n"
            "        @staticmethod\n"
            "        def prepare():\n"
            "            nonlocal statement\n"
            "            statement = 'SELECT 1'\n"
            "    class Child(Base):\n"
            "        pass\n"
            "    Child.prepare()\n"
            "    session.execute(statement)\n"
            "outer()\n"
        ),
        (
            "class Root:\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return select(Model)\n"
            "class Left(Root):\n"
            "    pass\n"
            "class Right(Root):\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return 'SELECT 1'\n"
            "class Child(Left, Right):\n"
            "    pass\n"
            "session.execute(Child.build())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_resolves_inherited_methods_and_side_effects(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "inherited_method.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "class Query:\n"
            "    statement = 'SELECT 1'\n"
            "query = Query()\n"
            "query.statement = select(Model)\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class Query:\n"
            "    statement = select(Model)\n"
            "    @classmethod\n"
            "    def prepare(cls):\n"
            "        cls.statement = 'SELECT 1'\n"
            "session.execute(Query.statement)\n"
        ),
        (
            "class Base:\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return 'SELECT 1'\n"
            "class Child(Base):\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return select(Model)\n"
            "session.execute(Child.build())\n"
        ),
        (
            "class Raw:\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return 'SELECT 1'\n"
            "class Safe:\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return select(Model)\n"
            "class Child(Safe, Raw):\n"
            "    pass\n"
            "session.execute(Child.build())\n"
        ),
        (
            "class Query:\n"
            "    statement = select(Model)\n"
            "if configured():\n"
            "    Query.statement = select(Model)\n"
            "else:\n"
            "    Query.statement = select(OtherModel)\n"
            "session.execute(Query.statement)\n"
        ),
        (
            "class Query:\n"
            "    statement = select(Model)\n"
            "enabled = False\n"
            "if enabled:\n"
            "    Query.statement = 'SELECT 1'\n"
            "session.execute(Query.statement)\n"
        ),
        (
            "class Root:\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return 'SELECT 1'\n"
            "class Left(Root):\n"
            "    pass\n"
            "class Right(Root):\n"
            "    @staticmethod\n"
            "    def build():\n"
            "        return select(Model)\n"
            "class Child(Left, Right):\n"
            "    pass\n"
            "session.execute(Child.build())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_attribute_and_mro_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "attribute_state_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "index = 0 if 2 > 1 else 1\n"
            "statements = ('SELECT 1', select(Model))\n"
            "session.execute(statements[index])\n"
        ),
        (
            "index = configured_index()\n"
            "statements = (select(Model), 'SELECT 1')\n"
            "session.execute(statements[index])\n"
        ),
        (
            "key = configured_key()\n"
            "payload = {'safe': select(Model), 'raw': 'SELECT 1'}\n"
            "session.execute(payload[key])\n"
        ),
        (
            "key = configured_key()\n"
            "payload = {'safe': select(Model), 'raw': 'SELECT 1'}\n"
            "session.execute(payload.get(key, select(Model)))\n"
        ),
        (
            "parts = map(lambda value: value, ('SELECT 1',))\n"
            "session.execute(''.join(parts))\n"
        ),
        (
            "parts = map(lambda value: value.upper(), ('select 1',))\n"
            "session.execute(''.join(parts))\n"
        ),
        "from helpers import statement\nsession.execute(statement)\n",
        ("query = 'SELECT 1'\ndef run(query):\n    session.execute(query)\n"),
        ("query = 'SELECT 1'\nasync def run(query):\n    session.execute(query)\n"),
        ("query = 'SELECT 1'\nrunner = lambda query: session.execute(query)\n"),
        (
            "def run(statement):\n"
            "    session.execute(statement)\n"
            "run(configured_statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_merges_unknown_expression_risks(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "unknown_expression_risk.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = 'SELECT 1' if 2 < 1 else select(Model)\n"
            "session.execute(statement)\n"
        ),
        (
            "if 3 < 2:\n"
            "    statement = 'SELECT 1'\n"
            "else:\n"
            "    statement = select(Model)\n"
            "session.execute(statement)\n"
        ),
        (
            "index = configured_index()\n"
            "statements = (select(Model), select(OtherModel))\n"
            "session.execute(statements[index])\n"
        ),
        (
            "key = configured_key()\n"
            "payload = {'first': select(Model), 'second': select(OtherModel)}\n"
            "session.execute(payload.get(key, select(DefaultModel)))\n"
        ),
        (
            "parts = map(lambda value: value, ('readiness', ' healthy'))\n"
            "worker.execute(''.join(parts))\n"
        ),
        "from helpers import statement\nworker.execute(statement)\n",
        "from helpers import message\nlogger.info(message)\n",
    ),
)
def test_sqlite_ast_contract_gate_unknown_expression_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "unknown_expression_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "class SafeQuery:\n"
            "    statement = select(Model)\n"
            "class RawQuery:\n"
            "    statement = 'SELECT 1'\n"
            "if configured():\n"
            "    query = SafeQuery()\n"
            "else:\n"
            "    query = RawQuery()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class SafeQuery:\n"
            "    statement = select(Model)\n"
            "class RawQuery:\n"
            "    statement = 'SELECT 1'\n"
            "match configured():\n"
            "    case 1:\n"
            "        query = SafeQuery()\n"
            "    case _:\n"
            "        query = RawQuery()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class SafeQuery:\n"
            "    statement = select(Model)\n"
            "class RawQuery:\n"
            "    statement = 'SELECT 1'\n"
            "query = SafeQuery() if configured() else RawQuery()\n"
            "session.execute(query.statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_preserves_branch_instance_references(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "branch_instance_reference.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "class First:\n"
            "    statement = select(Model)\n"
            "class Second:\n"
            "    statement = select(OtherModel)\n"
            "query = First() if configured() else Second()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class SafeQuery:\n"
            "    statement = select(Model)\n"
            "class RawQuery:\n"
            "    statement = 'SELECT 1'\n"
            "if False:\n"
            "    query = RawQuery()\n"
            "else:\n"
            "    query = SafeQuery()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "class SafeQuery:\n"
            "    statement = select(Model)\n"
            "class RawQuery:\n"
            "    statement = 'SELECT 1'\n"
            "match 1:\n"
            "    case 1:\n"
            "        query = SafeQuery()\n"
            "    case _:\n"
            "        query = RawQuery()\n"
            "session.execute(query.statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_branch_instance_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "branch_instance_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = select(Model)\n"
            "class Base:\n"
            "    def mutate(self):\n"
            "        global statement\n"
            "        statement = 'SELECT 1'\n"
            "class Child(Base):\n"
            "    def mutate(self):\n"
            "        super().mutate()\n"
            "Child().mutate()\n"
            "session.execute(statement)\n"
        ),
        (
            "class Base:\n"
            "    statement = select(Model)\n"
            "    @classmethod\n"
            "    def mutate(cls):\n"
            "        cls.statement = 'SELECT 1'\n"
            "class Child(Base):\n"
            "    @classmethod\n"
            "    def mutate(cls):\n"
            "        super().mutate()\n"
            "Child.mutate()\n"
            "session.execute(Child.statement)\n"
        ),
        (
            "class Base:\n"
            "    def mutate(self):\n"
            "        self.statement = 'SELECT 1'\n"
            "class Child(Base):\n"
            "    def mutate(self):\n"
            "        super(Child, self).mutate()\n"
            "query = Child()\n"
            "query.mutate()\n"
            "session.execute(query.statement)\n"
        ),
        (
            "def outer():\n"
            "    statement = select(Model)\n"
            "    class Base:\n"
            "        def mutate(self):\n"
            "            nonlocal statement\n"
            "            statement = 'SELECT 1'\n"
            "    class Child(Base):\n"
            "        def mutate(self):\n"
            "            super().mutate()\n"
            "    Child().mutate()\n"
            "    session.execute(statement)\n"
            "outer()\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_dispatches_super_with_c3_mro(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "super_dispatch.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "class Base:\n"
            "    def build(self):\n"
            "        return select(Model)\n"
            "class Child(Base):\n"
            "    def build(self):\n"
            "        return super().build()\n"
            "session.execute(Child().build())\n"
        ),
        (
            "class Root:\n"
            "    def build(self):\n"
            "        return 'SELECT 1'\n"
            "class Left(Root):\n"
            "    def build(self):\n"
            "        return select(Model)\n"
            "class Right(Root):\n"
            "    pass\n"
            "class Child(Left, Right):\n"
            "    def build(self):\n"
            "        return super().build()\n"
            "session.execute(Child().build())\n"
        ),
        (
            "class Base:\n"
            "    statement = select(Model)\n"
            "    @classmethod\n"
            "    def read(cls):\n"
            "        return cls.statement\n"
            "class Child(Base):\n"
            "    @classmethod\n"
            "    def read(cls):\n"
            "        return super().read()\n"
            "session.execute(Child.read())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_super_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "super_dispatch_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "    return True\n"
            "True and mutate()\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "    return False\n"
            "False or mutate()\n"
            "session.execute(statement)\n"
        ),
        ("values = ('SELECT 1' for _ in (1,))\nsession.execute(''.join(values))\n"),
        (
            "values = map(lambda value: value, ('SELECT 1',))\n"
            "session.execute(''.join(values))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_consumes_lazy_expression_risks(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "lazy_expression.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("raw-sql-execute" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "    return True\n"
            "False and mutate()\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "    return False\n"
            "True or mutate()\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = 'SELECT 1'\n"
            "    return statement\n"
            "values = (mutate() for _ in (1,))\n"
            "session.execute(statement)\n"
        ),
        ("values = (select(Model) for _ in (1,))\nworker.execute(values)\n"),
        (
            "values = map(lambda value: value, ('readiness', ' healthy'))\n"
            "worker.execute(''.join(values))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_lazy_expression_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "lazy_expression_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        "session.execute(statement)\n",
        "session.execute(None)\n",
        "session.execute(1)\n",
        (
            "statement = select(Model) if configured() else candidate\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "for statement in configured_values():\n"
            "    pass\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "while configured():\n"
            "    statement = candidate\n"
            "session.execute(statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_requires_positive_orm_proof(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "positive_orm_proof.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "class CustomSession(Session):\n"
            "    pass\n"
            "class DerivedSession(CustomSession):\n"
            "    pass\n"
            "db = DerivedSession()\n"
            "db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "class CustomSession(AsyncSession):\n"
            "    pass\n"
            "async def run():\n"
            "    db = CustomSession()\n"
            "    await db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import AsyncConnection\n"
            "class CustomConnection(AsyncConnection):\n"
            "    pass\n"
            "async def run():\n"
            "    db = CustomConnection()\n"
            "    await db.execute(select(Model))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_sqlalchemy_receiver_subclass_mro(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "sqlalchemy_receiver_subclass_control.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        "session.execute(select(Model))\n",
        "session.execute(update(Model).where(Model.id == identity))\n",
        (
            "statement = select(Model) if configured() else select(OtherModel)\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "for statement in ():\n"
            "    statement = candidate\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = candidate\n"
            "for statement in (candidate, select(Model)):\n"
            "    pass\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "while False:\n"
            "    statement = candidate\n"
            "session.execute(statement)\n"
        ),
        "worker.execute(candidate)\nlogger.execute(candidate)\n",
    ),
)
def test_sqlite_ast_contract_gate_positive_orm_proof_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "positive_orm_proof_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = candidate\n"
            "    return (1,)\n"
            "values = (select(Model) for _ in mutate())\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = candidate\n"
            "    return select(Model)\n"
            "values = (mutate() for _ in (1,))\n"
            "for value in values:\n"
            "    worker.execute(value)\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "def unsafe():\n"
            "    global statement\n"
            "    statement = candidate\n"
            "    return select(Model)\n"
            "def safe():\n"
            "    global statement\n"
            "    statement = select(Model)\n"
            "    return select(Model)\n"
            "value = unsafe() if configured() else safe()\n"
            "worker.execute(value)\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "def unsafe():\n"
            "    global statement\n"
            "    statement = candidate\n"
            "    return select(Model)\n"
            "def safe():\n"
            "    return select(Model)\n"
            "builder = unsafe if configured() else safe\n"
            "worker.execute(builder())\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "class Mutator:\n"
            "    def unsafe(self):\n"
            "        global statement\n"
            "        statement = candidate\n"
            "    def safe(self):\n"
            "        pass\n"
            "mutator = Mutator()\n"
            "callback = mutator.unsafe if configured() else mutator.safe\n"
            "callback()\n"
            "session.execute(statement)\n"
        ),
        (
            "class Base:\n"
            "    def build(self):\n"
            "        return select(Model)\n"
            "class Child(Base):\n"
            "    def build(self):\n"
            "        return super(selected_class, self).build()\n"
            "session.execute(Child().build())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_unprovable_dynamic_effects(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "unprovable_dynamic_effect.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "statement = select(Model)\n"
            "def mutate():\n"
            "    global statement\n"
            "    statement = candidate\n"
            "    return statement\n"
            "values = (mutate() for _ in (1,))\n"
            "session.execute(statement)\n"
        ),
        (
            "statement = select(Model)\n"
            "values = (statement for _ in (1,))\n"
            "statement = select(OtherModel)\n"
            "for value in values:\n"
            "    session.execute(value)\n"
        ),
        (
            "statement = select(Model)\n"
            "value = select(OtherModel) if True else mutate(statement)\n"
            "session.execute(statement)\n"
            "session.execute(value)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_lazy_and_branch_safe_controls(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "lazy_branch_control.py"
    fixture.write_text(_with_sqlalchemy_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize("sink_method", ("execute", "scalar", "scalars"))
@pytest.mark.parametrize("production_path", _sql_contract_files())
def test_sqlite_ast_contract_gate_rejects_unproven_injection_in_every_module(
    tmp_path: Path,
    production_path: Path,
    sink_method: str,
) -> None:
    fixture = tmp_path / production_path.name
    fixture.write_text(
        production_path.read_text(encoding="utf-8")
        + "\n"
        + "from sqlalchemy.orm import Session as __InjectedSession\n"
        + "def __injected_sql_policy_probe(session: __InjectedSession):\n"
        + f"    session.{sink_method}(__injected_statement)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations(
        (fixture,),
        pragma_policy_path=fixture
        if production_path == SQLITE_PRAGMA_POLICY_PATH
        else Path(),
    )

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session, statement):\n"
            "    db.execute(statement)\n"
        ),
        (
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session, statement):\n"
            "    db.scalar(statement)\n"
        ),
        (
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session, statement):\n"
            "    db.scalars(statement)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "async def run(db: AsyncSession, statement):\n"
            "    await db.stream(statement)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "async def run(db: AsyncSession, statement):\n"
            "    await db.stream_scalars(statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_covers_sqlalchemy_statement_sinks(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "sqlalchemy_statement_sink.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "alias = db\n"
            "alias.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import Connection, select\n"
            "db: Connection\n"
            "alias = db\n"
            "alias.scalar(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db = Session()\n"
            "db.scalars(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import sessionmaker\n"
            "factory = sessionmaker()\n"
            "db = factory()\n"
            "db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "class Repository:\n"
            "    def __init__(self, db: Session):\n"
            "        self.db = db\n"
            "    def load(self):\n"
            "        return self.db.scalar(select(Model))\n"
            "db: Session\n"
            "Repository(db).load()\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_proves_receivers_from_sqlalchemy_sources(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "sqlalchemy_receiver_control.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        "client.execute(select(Model))\n",
        "service.database.execute(select(Model))\n",
        "repository.connection.scalars(select(Model))\n",
    ),
)
def test_sqlite_ast_contract_gate_rejects_unproven_sink_receivers(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "unproven_sink_receiver.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-receiver" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import *\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(select(Model))\n"
        ),
        (
            "from helpers import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(select(Model).compile())\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(select(Model).__str__())\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(select(Model).unknown_transform())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_requires_statement_source_and_safe_methods(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "sqlalchemy_statement_provenance.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "statement = select(Model).where(Model.id == identity).limit(1)\n"
            "db.execute(statement.execution_options(populate_existing=True))\n"
        ),
        (
            "from sqlalchemy import update\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "statement = update(Model).where(Model.id == identity).values(active=True)\n"
            "db.execute(statement)\n"
        ),
        (
            "from sqlalchemy.dialects.sqlite import insert\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "statement = insert(Model).values(identity=identity)\n"
            "db.execute(statement.on_conflict_do_nothing())\n"
        ),
        (
            "import sqlalchemy as sa\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.scalars(sa.select(Model))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_sourced_statement_methods(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "sqlalchemy_statement_method_control.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sink = db.execute\n"
            "sink(candidate)\n"
        ),
        (
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sink = getattr(db, 'scalar')\n"
            "sink(candidate)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "async def run(db: AsyncSession):\n"
            "    sink = db.scalars\n"
            "    await sink(candidate)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "async def run(db: AsyncSession):\n"
            "    sink = getattr(db, 'stream_scalars')\n"
            "    await sink(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_checks_statement_for_bound_sink_callables(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "bound_sink_callable.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sink = db.execute\n"
            "sink(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sink = getattr(db, 'scalar')\n"
            "sink(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "async def run(db: AsyncSession):\n"
            "    sink = db.scalars\n"
            "    await sink(select(Model))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_proven_bound_sink_callables(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "bound_sink_callable_control.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "binding",
    (
        "sink = client.execute\n",
        "sink = getattr(client, 'execute')\n",
    ),
)
def test_sqlite_ast_contract_gate_checks_receiver_for_bound_sink_callables(
    tmp_path: Path,
    binding: str,
) -> None:
    fixture = tmp_path / "bound_sink_receiver.py"
    fixture.write_text(
        "from sqlalchemy import select\n"
        "client = configured_client()\n" + binding + "sink(select(Model))\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-receiver" in item for item in violations)


@pytest.mark.parametrize("receiver_name", ("worker", "logger"))
def test_sqlite_ast_contract_gate_does_not_prove_receiver_from_variable_name(
    tmp_path: Path,
    receiver_name: str,
) -> None:
    fixture = tmp_path / "receiver_name_is_not_proof.py"
    fixture.write_text(
        f"from sqlalchemy import select\n{receiver_name}.execute(select(Model))\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-receiver" in item for item in violations)


@pytest.mark.parametrize("receiver_name", ("worker", "logger"))
def test_sqlite_ast_contract_gate_prefers_real_session_proof_over_variable_name(
    tmp_path: Path,
    receiver_name: str,
) -> None:
    fixture = tmp_path / "real_session_beats_receiver_name.py"
    fixture.write_text(
        "from sqlalchemy.orm import Session\n"
        f"{receiver_name}: Session\n"
        f"{receiver_name}.execute(candidate)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


def test_sqlite_ast_contract_gate_allows_proven_local_non_sql_receiver(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "proven_local_non_sql_receiver.py"
    fixture.write_text(
        "class Worker:\n"
        "    def execute(self, message):\n"
        "        return message\n"
        "worker = Worker()\n"
        "worker.execute(candidate)\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "from sqlalchemy.sql import Select\n"
            "db: Session\n"
            "statement: Select\n"
            "db.execute(statement)\n"
        ),
        (
            "from sqlalchemy.orm import Session\n"
            "from sqlalchemy.sql import Select\n"
            "db: Session\n"
            "statement: Select = build_statement()\n"
            "db.execute(statement)\n"
        ),
        (
            "from sqlalchemy.orm import Session\n"
            "from sqlalchemy.sql import Select\n"
            "def run(db: Session, statement: Select):\n"
            "    db.execute(statement)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_never_promotes_statement_from_annotation(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "statement_annotation_is_not_value_proof.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "from sqlalchemy.sql import Select\n"
            "db: Session\n"
            "statement: Select = select(Model)\n"
            "db.execute(statement)\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "from sqlalchemy.sql import Select\n"
            "db: Session\n"
            "def run(statement: Select):\n"
            "    db.execute(statement)\n"
            "run(select(Model))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_uses_expression_not_annotation_as_statement_proof(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "statement_expression_proof_control.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from decorators import decorate\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "@decorate\n"
            "def build_statement():\n"
            "    return select(Model)\n"
            "db.execute(build_statement())\n"
        ),
        (
            "from decorators import decorate\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "@decorate\n"
            "async def build_statement():\n"
            "    return select(Model)\n"
            "async def run(db: AsyncSession):\n"
            "    await db.execute(await build_statement())\n"
        ),
        (
            "from decorators import decorate\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "@decorate()\n"
            "def build_statement():\n"
            "    return select(Model)\n"
            "db.execute(build_statement())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_treats_external_decorated_result_as_unproven(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "external_decorator.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "class Builder:\n"
            "    @staticmethod\n"
            "    def statement():\n"
            "        return select(Model)\n"
            "db: Session\n"
            "db.execute(Builder.statement())\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "class Builder:\n"
            "    @staticmethod\n"
            "    async def statement():\n"
            "        return select(Model)\n"
            "async def run(db: AsyncSession):\n"
            "    await db.execute(await Builder.statement())\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_exact_safe_function_decorators(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "safe_decorator_control.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "builder_source",
    (
        (
            "from sqlalchemy import literal_column, select\n"
            "statement = select(literal_column('account_id'))\n"
        ),
        (
            "from sqlalchemy import literal_column, select\n"
            "textual = literal_column\n"
            "statement = select(textual('SELECT account_id FROM account'))\n"
        ),
        (
            "from sqlalchemy import select, text\n"
            "statement = select(text('SELECT account_id FROM account'))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_forbids_textual_sql_builders(
    tmp_path: Path,
    builder_source: str,
) -> None:
    fixture = tmp_path / "textual_sql_builder.py"
    fixture.write_text(
        builder_source
        + "from sqlalchemy.orm import Session\n"
        + "db: Session\n"
        + "db.execute(statement)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(
        "sqlalchemy-text" in item or "unproven-sql-statement" in item
        for item in violations
    )


def test_sqlite_ast_contract_gate_allows_bound_literal_expression(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "bound_literal_control.py"
    fixture.write_text(
        "from sqlalchemy import literal, select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        "db.execute(select(literal('SELECT account_id FROM account')))\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import AsyncConnection\n"
            "async def run(db: AsyncConnection):\n"
            "    await db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import async_sessionmaker\n"
            "factory = async_sessionmaker()\n"
            "async def run():\n"
            "    db = factory()\n"
            "    await db.execute(select(Model))\n"
        ),
        (
            "from sqlalchemy import select\n"
            "from sqlalchemy.ext.asyncio import async_sessionmaker\n"
            "factory = async_sessionmaker()\n"
            "async def run():\n"
            "    async with factory.begin() as db:\n"
            "        await db.execute(select(Model))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_supports_real_async_sqlalchemy_sources(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "real_async_sqlalchemy_source.py"
    fixture.write_text(_with_orm_model_proofs(source), encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy.orm import Session\n"
            "class CustomSession(Session):\n"
            "    pass\n"
            "class DerivedSession(CustomSession):\n"
            "    pass\n"
            "db = DerivedSession()\n"
            "db.execute(candidate)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "class CustomSession(AsyncSession):\n"
            "    pass\n"
            "async def run():\n"
            "    db = CustomSession()\n"
            "    await db.execute(candidate)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncConnection\n"
            "class CustomConnection(AsyncConnection):\n"
            "    pass\n"
            "async def run():\n"
            "    db = CustomConnection()\n"
            "    await db.execute(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_tracks_sqlalchemy_receiver_subclass_mro(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "sqlalchemy_receiver_subclass.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)
    assert not any("unproven-sql-receiver" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy.orm import AsyncSession\n"
            "db: AsyncSession\n"
            "db.execute(candidate)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import Session\n"
            "db: Session\n"
            "db.execute(candidate)\n"
        ),
        (
            "from sqlalchemy import AsyncConnection\n"
            "db: AsyncConnection\n"
            "db.execute(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_nonexistent_sqlalchemy_type_sources(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "nonexistent_sqlalchemy_type_source.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-receiver" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from functools import partial\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sink = partial(db.execute, candidate)\n"
            "sink()\n"
        ),
        (
            "from functools import partial\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "partial(db.scalar)(candidate)\n"
        ),
        (
            "from operator import methodcaller\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "methodcaller('execute', candidate)(db)\n"
        ),
        (
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "attrgetter('scalars')(db)(candidate)\n"
        ),
        (
            "import functools\n"
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "async def run(db: AsyncSession):\n"
            "    sink = functools.partial(db.execute, candidate)\n"
            "    await sink()\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_checks_higher_order_sink_statements(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "higher_order_sink.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from functools import partial\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sink = partial(db.execute, select(TenantModel))\n"
            "sink()\n"
        ),
        (
            "from functools import partial\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "partial(db.scalar)(select(TenantModel))\n"
        ),
        (
            "from operator import methodcaller\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "methodcaller('execute', select(TenantModel))(db)\n"
        ),
        (
            "from operator import attrgetter\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "attrgetter('scalars')(db)(select(TenantModel))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_proven_higher_order_sinks(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "higher_order_sink_control.py"
    fixture.write_text(source, encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from helpers import partial\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "wrapped = partial(db.execute, candidate)\n"
            "wrapped()\n"
        ),
        (
            "from operator import methodcaller\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "method_name = configured_method()\n"
            "methodcaller(method_name, candidate)(db)\n"
        ),
        (
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "attribute_name = configured_attribute()\n"
            "attrgetter(attribute_name)(db)(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_fails_closed_for_unproven_higher_order_sql_chain(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "unproven_higher_order_sql_chain.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-call-chain" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy.orm import Session\n"
            "class Repository:\n"
            "    def __init__(self, db: Session):\n"
            "        self.db = db\n"
            "    @property\n"
            "    def sink(self):\n"
            "        return self.db.execute\n"
            "db: Session\n"
            "Repository(db).sink(candidate)\n"
        ),
        (
            "from sqlalchemy.ext.asyncio import AsyncSession\n"
            "class Repository:\n"
            "    def __init__(self, db: AsyncSession):\n"
            "        self.db = db\n"
            "    @property\n"
            "    def sink(self):\n"
            "        return self.db.scalars\n"
            "async def run(db: AsyncSession):\n"
            "    await Repository(db).sink(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_checks_property_returned_sink(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "property_sink.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


def test_sqlite_ast_contract_gate_allows_property_returned_proven_sink(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "property_sink_control.py"
    fixture.write_text(
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "class Repository:\n"
        "    def __init__(self, db: Session):\n"
        "        self.db = db\n"
        "    @property\n"
        "    def sink(self):\n"
        "        return self.db.execute\n"
        "db: Session\n"
        "Repository(db).sink(select(TenantModel))\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "statement",
    (
        "select(candidate)",
        "select(TenantModel).where(candidate)",
        "select(TenantModel).join(candidate)",
        "select(TextClause('tenant_id = 1'))",
        "select(TenantModel).where(external_predicate())",
        "select(column('tenant_id', is_literal=True))",
    ),
)
def test_sqlite_ast_contract_gate_rejects_unproven_constructor_or_method_arguments(
    tmp_path: Path,
    statement: str,
) -> None:
    fixture = tmp_path / "unproven_statement_argument.py"
    fixture.write_text(
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import column, select\n"
        "from sqlalchemy.orm import Session\n"
        "from sqlalchemy.sql.elements import TextClause\n"
        "db: Session\n"
        f"db.execute({statement})\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(
        "unproven-sql-statement" in item or "sqlalchemy-textual-builder" in item
        for item in violations
    )


@pytest.mark.parametrize(
    "statement",
    (
        "select(TenantModel)",
        "select(TenantModel.tenant_id)",
        "select(TenantModel).where(TenantModel.tenant_id == 'tenant')",
        (
            "select(TenantModel)"
            ".where(TenantModel.tenant_id == 'tenant')"
            ".order_by(TenantModel.tenant_id)"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_proven_model_column_and_predicate_arguments(
    tmp_path: Path,
    statement: str,
) -> None:
    fixture = tmp_path / "proven_statement_argument_control.py"
    fixture.write_text(
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        f"db.execute({statement})\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from hermes_cloud.domain.contract_models import CloudEnvelope\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session, envelope: CloudEnvelope):\n"
            "    db.scalar(select(TenantModel).where(\n"
            "        TenantModel.tenant_id == envelope.tenant_id\n"
            "    ))\n"
        ),
        (
            "from datetime import UTC, datetime\n"
            "from hermes_cloud.platform.postgres.models import (\n"
            "    TenantModel, WorkspaceMembershipModel\n"
            ")\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session):\n"
            "    now = datetime.now(UTC)\n"
            "    db.scalars(\n"
            "        select(TenantModel)\n"
            "        .outerjoin(\n"
            "            WorkspaceMembershipModel,\n"
            "            TenantModel.tenant_id == WorkspaceMembershipModel.tenant_id,\n"
            "        )\n"
            "        .where(TenantModel.created_at <= now)\n"
            "        .order_by(TenantModel.created_at)\n"
            "        .limit(1)\n"
            "    ).all()\n"
        ),
        (
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session, profile: str | None):\n"
            "    statement = select(TenantModel)\n"
            "    if profile is not None:\n"
            "        statement = statement.where(TenantModel.slug == profile)\n"
            "    db.execute(statement)\n"
        ),
        (
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select, update\n"
            "from sqlalchemy.orm import Session\n"
            "def load(db: Session, identity: str):\n"
            "    row = db.get(TenantModel, identity)\n"
            "    if configured():\n"
            "        return None\n"
            "    return row\n"
            "def run(db: Session, identity: str):\n"
            "    row = load(db, identity)\n"
            "    if row is None:\n"
            "        return\n"
            "    current: int = row.revision\n"
            "    statement = (\n"
            "        update(TenantModel)\n"
            "        .where(TenantModel.revision == current)\n"
            "        .values(revision=current + 1)\n"
            "    )\n"
            "    db.execute(statement)\n"
            "    result = db.execute(select(TenantModel))\n"
            "    selected = result.scalar_one_or_none()\n"
            "    if selected is not None:\n"
            "        db.scalars(\n"
            "            select(TenantModel).where(\n"
            "                TenantModel.tenant_id == selected.tenant_id\n"
            "            )\n"
            "        ).all()\n"
        ),
        (
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session, identity: str):\n"
            "    row = db.scalar(\n"
            "        select(TenantModel).where(TenantModel.tenant_id == identity)\n"
            "    )\n"
            "    if row is None:\n"
            "        row = TenantModel(tenant_id=identity)\n"
            "    db.execute(\n"
            "        select(TenantModel).where(\n"
            "            TenantModel.tenant_id == row.tenant_id\n"
            "        )\n"
            "    )\n"
        ),
        (
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import delete, select\n"
            "from sqlalchemy.orm import Session\n"
            "def run(db: Session):\n"
            "    rows = db.scalars(select(TenantModel)).all()\n"
            "    identities = tuple(row.tenant_id for row in rows)\n"
            "    for identity in identities:\n"
            "        db.execute(\n"
            "            delete(TenantModel).where(TenantModel.tenant_id == identity)\n"
            "        )\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_propagates_orm_bound_values_and_results(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "orm_bound_value_control.py"
    fixture.write_text(source, encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from hermes_cloud.domain.contract_models import CloudEnvelope\n"
            "def run(db: Session, envelope: CloudEnvelope):\n"
            "    db.execute(envelope.statement)\n"
        ),
        ("from datetime import UTC, datetime\ndb.execute(datetime.now(UTC))\n"),
        ("row = db.get(TenantModel, identity)\ndb.execute(row.statement)\n"),
        (
            "result = db.execute(select(TenantModel))\n"
            "db.execute(result.scalar_one_or_none())\n"
        ),
        (
            "def raw_statement() -> str:\n"
            "    return 'SELECT 1'\n"
            "db.execute(raw_statement())\n"
        ),
        "db.execute(TenantModel(tenant_id=identity))\n",
        "db.execute(tuple((identity,)))\n",
    ),
)
def test_sqlite_ast_contract_gate_never_promotes_bound_values_to_statements(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "orm_bound_value_rejection.py"
    fixture.write_text(
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        "identity = 'tenant'\n" + source,
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from typing import Optional\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Optional[Session]\n"
            "db.execute(select(TenantModel))\n"
        ),
        (
            "from typing import Annotated\n"
            "from sqlalchemy.orm import Session\n"
            "from sqlalchemy.sql import Select\n"
            "db: Session\n"
            "statement: Annotated[Select, 'ORM_CORE']\n"
            "db.execute(statement)\n"
        ),
        (
            "from typing import Annotated, Optional\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Annotated[Optional[Session], 'receiver']\n"
            "db.execute(select(TenantModel))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_optional_or_metadata_statement_proof(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "annotation_union_rejection.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(
        "unproven-sql-receiver" in item or "unproven-sql-statement" in item
        for item in violations
    )


@pytest.mark.parametrize(
    "source",
    (
        (
            "from typing import Annotated\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Annotated[Session, 'metadata', object]\n"
            "db.execute(select(TenantModel))\n"
        ),
        (
            "from typing import Union\n"
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import Connection, select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Union[Session, Connection]\n"
            "db.execute(select(TenantModel))\n"
        ),
        (
            "from hermes_cloud.platform.postgres.models import TenantModel\n"
            "from sqlalchemy import Connection, select\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session | Connection\n"
            "db.execute(select(TenantModel))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_handles_annotated_and_sql_receiver_unions(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "annotation_union_control.py"
    fixture.write_text(source, encoding="utf-8")

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "function_name",
    (
        "_ensure_model",
        "_one_or_none",
        "_read_unique_model",
        "_insert_or_compare",
    ),
)
def test_sqlite_ast_contract_gate_does_not_trust_internal_contract_names_elsewhere(
    tmp_path: Path,
    function_name: str,
) -> None:
    fixture = tmp_path / "same_named_external_helper.py"
    fixture.write_text(
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        f"def {function_name}(session: Session, model: type[object], "
        "identity: object):\n"
        "    session.execute(select(model).where(identity))\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from functools import partial\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "partial(getattr, db, 'execute')()(candidate)\n"
        ),
        (
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "attrgetter('execute.__call__')(db)(candidate)\n"
        ),
        (
            "from operator import methodcaller\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "methodcaller('__getattribute__', 'execute')(db)(candidate)\n"
        ),
        (
            "from functools import partial\n"
            "from operator import methodcaller\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "partial(methodcaller('execute', candidate), db)()\n"
        ),
        (
            "from functools import partial\n"
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "attrgetter('__call__')(partial(db.execute, candidate))()\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_nested_higher_order_sink_statement(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "nested_higher_order_sink.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "invocation",
    (
        "partial(getattr, db, 'execute')()(select(TenantModel))",
        "attrgetter('execute.__call__')(db)(select(TenantModel))",
        ("methodcaller('__getattribute__', 'execute')(db)(select(TenantModel))"),
        "partial(methodcaller('execute', select(TenantModel)), db)()",
        ("attrgetter('__call__')(partial(db.execute, select(TenantModel)))()"),
    ),
)
def test_sqlite_ast_contract_gate_allows_nested_higher_order_safe_statement(
    tmp_path: Path,
    invocation: str,
) -> None:
    fixture = tmp_path / "nested_higher_order_control.py"
    fixture.write_text(
        "from functools import partial\n"
        "from operator import attrgetter, methodcaller\n"
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        f"{invocation}\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from helpers import partial\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "partial(db.execute, candidate)()\n"
        ),
        (
            "from helpers import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "attrgetter('execute')(db)(candidate)\n"
        ),
        (
            "from helpers import wrap\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "wrap(db)(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_external_higher_order_call_chain(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "external_higher_order.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-call-chain" in item for item in violations)


@pytest.mark.parametrize(
    "decorator_import, decorator",
    (
        ("import builtins as builtin_alias\n", "builtin_alias.property"),
        ("from builtins import property as proven_property\n", "proven_property"),
        ("import functools as functools_alias\n", "functools_alias.cached_property"),
        (
            "from functools import cached_property as proven_cached_property\n",
            "proven_cached_property",
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_descriptor_returned_nested_sink(
    tmp_path: Path,
    decorator_import: str,
    decorator: str,
) -> None:
    fixture = tmp_path / "descriptor_nested_sink.py"
    fixture.write_text(
        decorator_import
        + "from functools import partial\n"
        + "from sqlalchemy.orm import Session\n"
        + "class Repository:\n"
        + "    def __init__(self, db: Session):\n"
        + "        self.db = db\n"
        + f"    @{decorator}\n"
        + "    def sink(self):\n"
        + "        return partial(getattr, self.db, 'execute')()\n"
        + "db: Session\n"
        + "Repository(db).sink(candidate)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "decorator_import, decorator",
    (
        ("import builtins as builtin_alias\n", "builtin_alias.property"),
        ("from builtins import property as proven_property\n", "proven_property"),
        ("import functools as functools_alias\n", "functools_alias.cached_property"),
        (
            "from functools import cached_property as proven_cached_property\n",
            "proven_cached_property",
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_descriptor_returned_nested_sink_control(
    tmp_path: Path,
    decorator_import: str,
    decorator: str,
) -> None:
    fixture = tmp_path / "descriptor_nested_sink_control.py"
    fixture.write_text(
        decorator_import
        + "from functools import partial\n"
        + "from hermes_cloud.platform.postgres.models import TenantModel\n"
        + "from sqlalchemy import select\n"
        + "from sqlalchemy.orm import Session\n"
        + "class Repository:\n"
        + "    def __init__(self, db: Session):\n"
        + "        self.db = db\n"
        + f"    @{decorator}\n"
        + "    def sink(self):\n"
        + "        return partial(getattr, self.db, 'execute')()\n"
        + "db: Session\n"
        + "Repository(db).sink(select(TenantModel))\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "is_literal",
    (
        "True",
        "1",
        "-1",
        "'yes'",
        "candidate",
    ),
)
def test_sqlite_ast_contract_gate_rejects_unproven_or_enabled_literal_column_flag(
    tmp_path: Path,
    is_literal: str,
) -> None:
    fixture = tmp_path / "literal_column_flag.py"
    fixture.write_text(
        "from sqlalchemy import column, select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        f"db.execute(select(column('tenant id', is_literal={is_literal})))\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)
    assert any("sqlalchemy-textual-builder" in item for item in violations)


@pytest.mark.parametrize("is_literal", ("False", "0", "0.0", "''", "None"))
def test_sqlite_ast_contract_gate_allows_statically_disabled_literal_column_flag(
    tmp_path: Path,
    is_literal: str,
) -> None:
    fixture = tmp_path / "disabled_literal_column_flag_control.py"
    fixture.write_text(
        "from sqlalchemy import column, select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        f"db.execute(select(column('tenant id', is_literal={is_literal})))\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


def test_sqlalchemy_column_literal_flag_compile_semantics() -> None:
    from sqlalchemy import column, select

    disabled = [
        str(select(column("tenant id", is_literal=flag)).compile())
        for flag in (False, 0, 0.0, "", None)
    ]
    enabled = str(select(column("tenant id", is_literal=True)).compile())

    assert len(set(disabled)) == 1
    assert '"tenant id"' in disabled[0]
    assert enabled != disabled[0]
    assert '"tenant id"' not in enabled


@pytest.mark.parametrize(
    "model_import, model_expression",
    (
        (
            "import hermes_cloud.platform.postgres.models as model_module\n",
            "model_module.TenantModel",
        ),
        (
            "from hermes_cloud.platform.postgres import models as model_module\n",
            "model_module.TenantModel",
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_exact_model_module_alias(
    tmp_path: Path,
    model_import: str,
    model_expression: str,
) -> None:
    fixture = tmp_path / "model_module_alias_control.py"
    fixture.write_text(
        model_import
        + "from sqlalchemy import select\n"
        + "from sqlalchemy.orm import Session\n"
        + "db: Session\n"
        + f"db.execute(select({model_expression}))\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


def test_sqlite_ast_contract_gate_rejects_external_model_module_alias(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "external_model_module_alias.py"
    fixture.write_text(
        "import helpers.models as model_module\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        "db.execute(select(model_module.TenantModel))\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import Column, Integer, MetaData, Table, select\n"
            "metadata = MetaData()\n"
            "tenant = Table('tenant', metadata, Column('id', Integer))\n"
            "statement = select(tenant).where(tenant.c.id == 1)\n"
        ),
        (
            "import sqlalchemy as sa\n"
            "metadata = sa.MetaData()\n"
            "tenant = sa.Table('tenant', metadata, sa.Column('id', sa.Integer))\n"
            "statement = sa.select(tenant).where(tenant.c.id == 1)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_official_core_table_column_sources(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "official_core_source_control.py"
    fixture.write_text(
        source
        + "from sqlalchemy.orm import Session\n"
        + "db: Session\n"
        + "db.execute(statement)\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import Column, Integer, MetaData, Table, select\n"
            "statement = select(Table(candidate, MetaData(), Column('id', Integer)))\n"
        ),
        (
            "from sqlalchemy import Column, Integer, Table, select\n"
            "statement = select(Table('tenant', candidate, Column('id', Integer)))\n"
        ),
        (
            "from sqlalchemy import Column, Integer, MetaData, Table, select\n"
            "statement = select(Table('tenant', MetaData(), Column(candidate, Integer)))\n"
        ),
        (
            "from sqlalchemy import Column, MetaData, Table, select\n"
            "statement = select(Table('tenant', MetaData(), Column('id', candidate)))\n"
        ),
        (
            "from sqlalchemy import MetaData, Table, select\n"
            "statement = select(Table('tenant', MetaData(), candidate))\n"
        ),
        (
            "from sqlalchemy import Column, MetaData, String, Table, select, text\n"
            "statement = select(Table('tenant', MetaData(), "
            "Column('id', String, server_default=text('CURRENT_USER'))))\n"
        ),
        (
            "from helpers import Column, MetaData, Table\n"
            "from sqlalchemy import select\n"
            "statement = select(Table('tenant', MetaData(), Column('id')))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_unproven_core_constructor_arguments(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "unproven_core_constructor_argument.py"
    fixture.write_text(
        source
        + "from sqlalchemy.orm import Session\n"
        + "db: Session\n"
        + "db.execute(statement)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "execute, scalar = attrgetter('execute', 'scalar')(db)\n"
            "execute(candidate)\n"
        ),
        (
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "sinks = attrgetter('execute', 'scalars')(db)\n"
            "sinks[1](candidate)\n"
        ),
        (
            "from operator import attrgetter\n"
            "from sqlalchemy.orm import Session\n"
            "class Repository:\n"
            "    def __init__(self, db: Session):\n"
            "        self.db = db\n"
            "db: Session\n"
            "repository = Repository(db)\n"
            "execute, scalars = attrgetter('db.execute', 'db.scalars')(repository)\n"
            "scalars(candidate)\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_tuple_attrgetter_sink_invocation(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "tuple_attrgetter_sink.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "invocation",
    (
        (
            "execute, scalar = attrgetter('execute', 'scalar')(db)\n"
            "execute(select(TenantModel))\n"
            "scalar(select(TenantModel))\n"
        ),
        (
            "sinks = attrgetter('execute', 'scalars')(db)\n"
            "sinks[0](select(TenantModel))\n"
            "sinks[1](select(TenantModel))\n"
        ),
        (
            "repository = Repository(db)\n"
            "execute, scalars = attrgetter('db.execute', 'db.scalars')(repository)\n"
            "execute(select(TenantModel))\n"
            "scalars(select(TenantModel))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_tuple_attrgetter_sink_controls(
    tmp_path: Path,
    invocation: str,
) -> None:
    fixture = tmp_path / "tuple_attrgetter_control.py"
    fixture.write_text(
        "from operator import attrgetter\n"
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "class Repository:\n"
        "    def __init__(self, db: Session):\n"
        "        self.db = db\n"
        "db: Session\n"
        f"{invocation}",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "chain",
    (
        ("    @sink.setter\n    def sink(self, value):\n        self.value = value\n"),
        ("    @sink.deleter\n    def sink(self):\n        pass\n"),
        (
            "    @sink.setter\n"
            "    def sink(self, value):\n"
            "        self.value = value\n"
            "    @sink.deleter\n"
            "    def sink(self):\n"
            "        pass\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_preserves_property_getter_across_mutators(
    tmp_path: Path,
    chain: str,
) -> None:
    fixture = tmp_path / "property_mutator_chain.py"
    fixture.write_text(
        "from sqlalchemy.orm import Session\n"
        "class Repository:\n"
        "    def __init__(self, db: Session):\n"
        "        self.db = db\n"
        "    @property\n"
        "    def sink(self):\n"
        "        return self.db.execute\n"
        + chain
        + "db: Session\n"
        + "Repository(db).sink(candidate)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


def test_sqlite_ast_contract_gate_replaces_property_getter_proof(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "property_getter_replacement.py"
    fixture.write_text(
        "from sqlalchemy.orm import Session\n"
        "class Repository:\n"
        "    def __init__(self, db: Session):\n"
        "        self.db = db\n"
        "    @property\n"
        "    def sink(self):\n"
        "        return self.db\n"
        "    @sink.getter\n"
        "    def sink(self):\n"
        "        return self.db.execute\n"
        "db: Session\n"
        "Repository(db).sink(candidate)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


def test_sqlite_ast_contract_gate_allows_property_mutator_chain_control(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "property_mutator_control.py"
    fixture.write_text(
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "class Repository:\n"
        "    def __init__(self, db: Session):\n"
        "        self.db = db\n"
        "    @property\n"
        "    def sink(self):\n"
        "        return self.db.execute\n"
        "    @sink.setter\n"
        "    def sink(self, value):\n"
        "        self.value = value\n"
        "    @sink.deleter\n"
        "    def sink(self):\n"
        "        pass\n"
        "db: Session\n"
        "Repository(db).sink(select(TenantModel))\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "source",
    (
        (
            "from sqlalchemy import DDL\n"
            "listener = DDL('CREATE TABLE injected (id INTEGER)')\n"
        ),
        (
            "from sqlalchemy import DDL\n"
            "from sqlalchemy.orm import Session\n"
            "db: Session\n"
            "db.execute(DDL('DROP TABLE tenant'))\n"
        ),
        (
            "from sqlalchemy import DDL, MetaData, event\n"
            "metadata = MetaData()\n"
            "event.listen(metadata, 'after_create', "
            "DDL('CREATE TABLE injected (id INTEGER)'))\n"
        ),
        (
            "from sqlalchemy import DDL, MetaData, event\n"
            "metadata = MetaData()\n"
            "event.listens_for(metadata, 'after_create')"
            "(DDL('CREATE TABLE injected (id INTEGER)'))\n"
        ),
        (
            "from sqlalchemy import MetaData, event, text\n"
            "metadata = MetaData()\n"
            "event.listen(metadata, 'after_create', "
            "text('CREATE TABLE injected (id INTEGER)'))\n"
        ),
        (
            "from sqlalchemy import MetaData, event\n"
            "from sqlalchemy.schema import ExecutableDDLElement\n"
            "metadata = MetaData()\n"
            "listener: ExecutableDDLElement\n"
            "event.listen(metadata, 'after_create', listener)\n"
        ),
        (
            "from sqlalchemy.schema import CreateSchema\n"
            "listener = CreateSchema('tenant')\n"
        ),
        (
            "from sqlalchemy import MetaData, event\n"
            "from sqlalchemy.schema import ExecutableDDLElement\n"
            "class CustomDDL(ExecutableDDLElement):\n"
            "    pass\n"
            "metadata = MetaData()\n"
            "event.listen(metadata, 'after_create', CustomDDL())\n"
        ),
        (
            "from sqlalchemy.schema import ExecutableDDLElement\n"
            "class CustomDDL(ExecutableDDLElement):\n"
            "    pass\n"
            "listener = CustomDDL()\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_rejects_ddl_and_textual_event_execution(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "ddl_event_execution.py"
    fixture.write_text(source, encoding="utf-8")

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any(
        "sqlalchemy-ddl" in item or "sqlalchemy-event-textual-listener" in item
        for item in violations
    )


def test_sqlite_ast_contract_gate_allows_non_textual_event_listener(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "safe_event_listener_control.py"
    fixture.write_text(
        "from sqlalchemy import MetaData, event\n"
        "metadata = MetaData()\n"
        "def listener(target, connection, **kwargs):\n"
        "    return None\n"
        "event.listen(metadata, 'after_create', listener)\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


def test_sqlalchemy_ddl_event_executes_during_real_create_all() -> None:
    from sqlalchemy import DDL, Column, Integer, MetaData, Table, create_engine, event
    from sqlalchemy import inspect as sqlalchemy_inspect

    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("ordinary", metadata, Column("id", Integer, primary_key=True))
    event.listen(
        metadata,
        "after_create",
        DDL("CREATE TABLE ddl_side_effect (id INTEGER)"),
    )

    metadata.create_all(engine)

    assert sqlalchemy_inspect(engine).has_table("ddl_side_effect")
    engine.dispose()


@pytest.mark.parametrize(
    "column_expression",
    (
        "Column('id', Integer, primary_key=True)",
        "Column('id', type_=Integer)",
        "Column(name='id', type_=Integer)",
        "Column(Integer, name='id')",
    ),
)
def test_sqlite_ast_contract_gate_allows_legal_column_argument_forms(
    tmp_path: Path,
    column_expression: str,
) -> None:
    fixture = tmp_path / "legal_column_arguments_control.py"
    fixture.write_text(
        "from sqlalchemy import Column, Integer, MetaData, Table, select\n"
        f"tenant = Table('tenant', MetaData(), {column_expression})\n"
        "statement = select(tenant).where(tenant.c.id == 1)\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        "db.execute(statement)\n",
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []


@pytest.mark.parametrize(
    "column_expression",
    (
        "Column(name=candidate, type_=Integer)",
        "Column(name='id', type_=candidate)",
        "Column(candidate, name='id')",
    ),
)
def test_sqlite_ast_contract_gate_rejects_unproven_column_argument_forms(
    tmp_path: Path,
    column_expression: str,
) -> None:
    fixture = tmp_path / "unproven_column_arguments.py"
    fixture.write_text(
        "from sqlalchemy import Column, Integer, MetaData, Table, select\n"
        f"statement = select(Table('tenant', MetaData(), {column_expression}))\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n"
        "db.execute(statement)\n",
        encoding="utf-8",
    )

    violations = _sqlite_sql_contract_violations((fixture,))

    assert any("unproven-sql-statement" in item for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        (
            "identity = lambda statement: statement\n"
            "db.execute(identity(select(TenantModel)))\n"
        ),
        (
            "def apply(wrapper, statement):\n"
            "    return wrapper(statement)\n"
            "db.execute(apply(lambda value: value, select(TenantModel)))\n"
        ),
    ),
)
def test_sqlite_ast_contract_gate_allows_lambda_statement_passthrough(
    tmp_path: Path,
    source: str,
) -> None:
    fixture = tmp_path / "lambda_statement_passthrough_control.py"
    fixture.write_text(
        "from hermes_cloud.platform.postgres.models import TenantModel\n"
        "from sqlalchemy import select\n"
        "from sqlalchemy.orm import Session\n"
        "db: Session\n" + source,
        encoding="utf-8",
    )

    assert _sqlite_sql_contract_violations((fixture,)) == []
