from __future__ import annotations

import ast
import asyncio
import threading
import time
import unittest
from pathlib import Path

from hermes_connector.adapters.sqlite_policy import SQLiteConnectionPolicy
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig

CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = CONNECTOR_ROOT / "src" / "hermes_connector"
TEST_ROOT = CONNECTOR_ROOT / "tests"
ADAPTERS = CONNECTOR_ROOT / "src" / "hermes_connector" / "adapters"
SQLITE_POLICY = ADAPTERS / "sqlite_policy.py"
ALLOWED_SQLITE_PRAGMAS = frozenset(
    {
        "PRAGMA busy_timeout",
        "PRAGMA foreign_keys",
        "PRAGMA foreign_keys = ON",
        "PRAGMA journal_mode",
        "PRAGMA journal_mode = WAL",
        "PRAGMA synchronous",
        "PRAGMA synchronous = FULL",
    }
)


def _python_files_under(root: Path) -> tuple[Path, ...]:
    return tuple(
        path for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts
    )


def _is_safe_busy_timeout_pragma(node: ast.JoinedStr) -> bool:
    if len(node.values) != 2:
        return False
    prefix, value = node.values
    return (
        isinstance(prefix, ast.Constant)
        and prefix.value == "PRAGMA busy_timeout = "
        and isinstance(value, ast.FormattedValue)
        and isinstance(value.value, ast.Attribute)
        and value.value.attr == "_busy_timeout_ms"
        and isinstance(value.value.value, ast.Name)
        and value.value.value.id == "self"
    )


def _is_safe_policy_execute(node: ast.Call) -> bool:
    function = node.func
    if (
        not isinstance(function, ast.Attribute)
        or function.attr != "execute"
        or len(node.args) != 1
    ):
        return False
    statement = node.args[0]
    if isinstance(statement, ast.Constant):
        return statement.value in ALLOWED_SQLITE_PRAGMAS
    return isinstance(statement, ast.JoinedStr) and _is_safe_busy_timeout_pragma(
        statement
    )


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


def _constant_string_value(
    node: ast.AST,
    string_values: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return string_values.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string_value(node.left, string_values)
        right = _constant_string_value(node.right, string_values)
        if left is not None and right is not None:
            return left + right
    return None


def _is_string_expression(node: ast.AST, string_names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Name):
        return node.id in string_names
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod):
            return _is_string_expression(node.left, string_names)
        if isinstance(node.op, ast.Add):
            return _is_string_expression(
                node.left,
                string_names,
            ) or _is_string_expression(node.right, string_names)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"format", "format_map", "join"}
        and _is_string_expression(node.func.value, string_names)
    )


def _receiver_name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _looks_like_database_receiver(name: str | None) -> bool:
    if name is None:
        return False
    normalized = name.lower()
    return normalized in {
        "connection",
        "conn",
        "cursor",
        "database",
        "db",
        "session",
    } or normalized.endswith(("_connection", "_cursor", "_session"))


def _database_access_violations(path: Path, source: str) -> set[str]:
    tree = ast.parse(source, filename=str(path))
    policy_file = path == SQLITE_POLICY
    violations: set[str] = set()
    sqlite_modules: set[str] = set()
    sqlalchemy_modules: set[str] = set()
    importlib_modules: set[str] = set()
    import_module_functions: set[str] = set()
    builtin_import_functions: set[str] = {"__import__"}
    sqlite_connect_functions: set[str] = set()
    sqlalchemy_text_functions: set[str] = set()
    string_names: set[str] = set()
    string_values: dict[str, str] = {}
    sqlite_connections: set[str] = set()
    sqlite_cursors: set[str] = set()
    bound_dbapi_execute: set[str] = set()
    bound_raw_execute: set[str] = set()
    bound_driver_execute: set[str] = set()
    bound_executemany: set[str] = set()
    bound_executescript: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                if alias.name == "sqlite3":
                    sqlite_modules.add(name)
                    if not policy_file:
                        violations.add("sqlite3-import")
                elif alias.name == "importlib":
                    importlib_modules.add(name)
                elif alias.name == "sqlalchemy" or alias.name.startswith("sqlalchemy."):
                    sqlalchemy_modules.add(name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "sqlite3":
                if not policy_file:
                    violations.add("sqlite3-import")
                sqlite_connect_functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "connect"
                )
            elif node.module == "importlib":
                import_module_functions.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "import_module"
                )
            elif node.module is not None and node.module.startswith("sqlalchemy"):
                imported_text = {
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "text"
                }
                if imported_text:
                    sqlalchemy_text_functions.update(imported_text)
                    violations.add("sqlalchemy-text")

    def is_dynamic_sqlite_import(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or not node.args:
            return False
        if _constant_string_value(node.args[0], string_values) != "sqlite3":
            return False
        function = node.func
        if isinstance(function, ast.Name):
            return (
                function.id in import_module_functions
                or function.id in builtin_import_functions
            )
        return (
            isinstance(function, ast.Attribute)
            and function.attr == "import_module"
            and _receiver_name(function.value) in importlib_modules
        )

    def is_sqlite_module(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Name) and node.id in sqlite_modules
        ) or is_dynamic_sqlite_import(node)

    def is_sqlite_connection_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        function = node.func
        return (
            isinstance(function, ast.Name) and function.id in sqlite_connect_functions
        ) or (
            isinstance(function, ast.Attribute)
            and function.attr == "connect"
            and is_sqlite_module(function.value)
        )

    def is_sqlite_connection(node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id in sqlite_connections:
            return True
        if (
            policy_file
            and isinstance(node, ast.Name)
            and _looks_like_database_receiver(node.id)
        ):
            return True
        return is_sqlite_connection_call(node)

    def is_sqlite_cursor_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cursor"
            and is_sqlite_connection(node.func.value)
        )

    def is_sqlite_cursor(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Name) and node.id in sqlite_cursors
        ) or is_sqlite_cursor_call(node)

    def is_sqlite_dbapi_receiver(node: ast.AST) -> bool:
        return is_sqlite_connection(node) or is_sqlite_cursor(node)

    assignments = tuple(
        node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))
    )
    changed = True
    while changed:
        before = (
            len(import_module_functions),
            len(builtin_import_functions),
            len(importlib_modules),
            len(sqlite_modules),
            len(sqlite_connect_functions),
            len(sqlalchemy_modules),
            len(sqlalchemy_text_functions),
            len(string_names),
            len(string_values),
            len(sqlite_connections),
            len(sqlite_cursors),
            len(bound_dbapi_execute),
            len(bound_raw_execute),
            len(bound_driver_execute),
            len(bound_executemany),
            len(bound_executescript),
        )
        for assignment in assignments:
            names = _assigned_names(assignment)
            value = assignment.value
            if _is_string_expression(value, string_names):
                string_names.update(names)
            constant_string = _constant_string_value(value, string_values)
            if constant_string is not None:
                string_values.update(dict.fromkeys(names, constant_string))
            if isinstance(value, ast.Name):
                alias_sets = (
                    import_module_functions,
                    builtin_import_functions,
                    importlib_modules,
                    sqlite_modules,
                    sqlite_connect_functions,
                    sqlalchemy_modules,
                    sqlalchemy_text_functions,
                    sqlite_connections,
                    sqlite_cursors,
                    bound_dbapi_execute,
                    bound_raw_execute,
                    bound_driver_execute,
                    bound_executemany,
                    bound_executescript,
                )
                for aliases in alias_sets:
                    if value.id in aliases:
                        aliases.update(names)
            if isinstance(value, ast.Attribute):
                owner = _receiver_name(value.value)
                if value.attr == "import_module" and owner in importlib_modules:
                    import_module_functions.update(names)
                elif value.attr == "connect" and owner in sqlite_modules:
                    sqlite_connect_functions.update(names)
                elif value.attr == "text" and owner in sqlalchemy_modules:
                    sqlalchemy_text_functions.update(names)
                elif value.attr == "exec_driver_sql":
                    bound_driver_execute.update(names)
                elif value.attr == "executemany":
                    bound_executemany.update(names)
                elif value.attr == "executescript":
                    bound_executescript.update(names)
                elif value.attr == "execute":
                    if is_sqlite_dbapi_receiver(value.value):
                        bound_dbapi_execute.update(names)
                    elif _looks_like_database_receiver(owner):
                        bound_raw_execute.update(names)
            if not isinstance(value, ast.Call):
                continue
            if is_dynamic_sqlite_import(value):
                sqlite_modules.update(names)
            if is_sqlite_connection_call(value):
                sqlite_connections.update(names)
            if is_sqlite_cursor_call(value):
                sqlite_cursors.update(names)

        after = (
            len(import_module_functions),
            len(builtin_import_functions),
            len(importlib_modules),
            len(sqlite_modules),
            len(sqlite_connect_functions),
            len(sqlalchemy_modules),
            len(sqlalchemy_text_functions),
            len(string_names),
            len(string_values),
            len(sqlite_connections),
            len(sqlite_cursors),
            len(bound_dbapi_execute),
            len(bound_raw_execute),
            len(bound_driver_execute),
            len(bound_executemany),
            len(bound_executescript),
        )
        changed = after != before

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            if is_dynamic_sqlite_import(node):
                violations.add("sqlite3-dynamic-import")
            if function.id in sqlite_connect_functions:
                violations.add("sqlite3-connect")
            if function.id in sqlalchemy_text_functions:
                violations.add("sqlalchemy-text")
            if function.id in bound_driver_execute:
                violations.add("exec-driver-sql")
            if function.id in bound_executemany:
                violations.add("sqlite3-executemany")
            if function.id in bound_executescript:
                violations.add("sqlite3-executescript")
            if function.id in bound_dbapi_execute:
                violations.add("raw-db-execute")
            if (
                function.id in bound_raw_execute
                and node.args
                and _is_string_expression(node.args[0], string_names)
            ):
                violations.add("raw-db-execute")
            continue
        if not isinstance(function, ast.Attribute):
            continue
        owner = _receiver_name(function.value)
        if is_dynamic_sqlite_import(node):
            violations.add("sqlite3-dynamic-import")
        if function.attr == "connect" and is_sqlite_module(function.value):
            violations.add("sqlite3-connect")
        elif function.attr == "text" and owner in sqlalchemy_modules:
            violations.add("sqlalchemy-text")
        elif function.attr == "exec_driver_sql":
            violations.add("exec-driver-sql")
        elif function.attr == "executemany":
            violations.add("sqlite3-executemany")
        elif function.attr == "executescript":
            violations.add("sqlite3-executescript")
        elif function.attr == "cursor":
            if not policy_file and is_sqlite_connection(function.value):
                violations.add("sqlite3-cursor")
        elif function.attr == "execute":
            if policy_file and _is_safe_policy_execute(node):
                continue
            if is_sqlite_dbapi_receiver(function.value) or (
                bool(node.args)
                and _is_string_expression(node.args[0], string_names)
                and _looks_like_database_receiver(owner)
            ):
                violations.add("raw-db-execute")
    return violations


class SQLAlchemyStorageArchitectureTest(unittest.TestCase):
    def test_ast_gate_rejects_raw_database_alias_escape_hatches(self) -> None:
        fixtures = {
            "sqlite_aliases.py": (
                "import sqlite3 as database_module\n"
                "open_database = database_module.connect\n"
                "connection = open_database('connector.sqlite3')\n"
                "cursor = connection.cursor()\n"
                "query = 'SELECT 1'\n"
                "aliased_query = query\n"
                "cursor.execute(aliased_query)\n"
            ),
            "sqlalchemy_text_alias.py": (
                "import sqlalchemy.sql as sql\n"
                "compile_text = sql.text\n"
                "statement = compile_text('SELECT 1')\n"
            ),
            "bound_driver_calls.py": (
                "run_driver_sql = connection.exec_driver_sql\n"
                "run_many = cursor.executemany\n"
                "run_script = cursor.executescript\n"
                "run_driver_sql('SELECT 1')\n"
                "run_many('SELECT 1', ())\n"
                "run_script('SELECT 1')\n"
            ),
            "dynamic_sqlite.py": (
                "import importlib as loader\n"
                "load_module = loader.import_module\n"
                "sqlite_module = load_module('sqlite3')\n"
                "open_database = sqlite_module.connect\n"
                "connection = open_database('connector.sqlite3')\n"
                "bound_execute = connection.execute\n"
                "query = 'SELECT 1'\n"
                "aliased_query = query\n"
                "bound_execute(aliased_query)\n"
            ),
        }

        violations = {
            name: _database_access_violations(
                Path(name),
                source,
            )
            for name, source in fixtures.items()
        }

        self.assertIn("sqlite3-import", violations["sqlite_aliases.py"])
        self.assertIn("sqlite3-connect", violations["sqlite_aliases.py"])
        self.assertIn("sqlite3-cursor", violations["sqlite_aliases.py"])
        self.assertIn("raw-db-execute", violations["sqlite_aliases.py"])
        self.assertIn(
            "sqlalchemy-text",
            violations["sqlalchemy_text_alias.py"],
        )
        self.assertEqual(
            violations["bound_driver_calls.py"],
            {
                "exec-driver-sql",
                "sqlite3-executemany",
                "sqlite3-executescript",
            },
        )
        self.assertIn(
            "sqlite3-dynamic-import",
            violations["dynamic_sqlite.py"],
        )
        self.assertIn("sqlite3-connect", violations["dynamic_sqlite.py"])
        self.assertIn("raw-db-execute", violations["dynamic_sqlite.py"])

    def test_ast_gate_allows_orm_core_and_unrelated_execute_calls(self) -> None:
        source = (
            '"""session.execute("SELECT secret") is documentation only."""\n'
            "from sqlalchemy import select\n"
            "def inspect(connection, session, worker, stream, statement):\n"
            "    connection.execute(statement)\n"
            "    session.execute(select(Model))\n"
            "    worker.execute('ordinary-worker-command')\n"
            "    stream.cursor()\n"
            "    return 'sqlite3.connect and text are forbidden messages'\n"
            "raise AssertionError('the scanner must not execute source')\n"
        )

        self.assertEqual(
            _database_access_violations(Path("safe_fixture.py"), source),
            set(),
        )

    def test_ast_gate_rejects_dynamic_sql_and_import_aliases(self) -> None:
        fixtures = {
            "percent_sql.py": (
                "def run(cursor, value):\n    cursor.execute('SELECT %s' % value)\n"
            ),
            "format_sql.py": (
                "def run(connection, value):\n"
                "    connection.execute('SELECT {}'.format(value))\n"
            ),
            "import_name_alias.py": (
                "import importlib\n"
                "module_name = 'sqlite3'\n"
                "database = importlib.import_module(module_name)\n"
                "connection = database.connect('connector.sqlite3')\n"
                "connection.execute(object())\n"
            ),
            "builtin_nested_alias.py": (
                "load_module = __import__\n"
                "module_name = 'sqlite3'\n"
                "database = load_module(module_name)\n"
                "open_database = database.connect\n"
                "connection = open_database('connector.sqlite3')\n"
                "cursor = connection.cursor()\n"
                "run_query = cursor.execute\n"
                "run_query('SELECT {}'.format(object()))\n"
            ),
            "fully_nested.py": (
                "import importlib as loader\n"
                "module_name = 'sqlite3'\n"
                "loader.import_module(module_name)"
                ".connect('connector.sqlite3')"
                ".cursor()"
                ".execute('SELECT %s' % object())\n"
                "raise AssertionError('the scanner must not execute source')\n"
            ),
        }
        expected = {
            "percent_sql.py": {"raw-db-execute"},
            "format_sql.py": {"raw-db-execute"},
            "import_name_alias.py": {
                "sqlite3-dynamic-import",
                "sqlite3-connect",
                "raw-db-execute",
            },
            "builtin_nested_alias.py": {
                "sqlite3-dynamic-import",
                "sqlite3-connect",
                "sqlite3-cursor",
                "raw-db-execute",
            },
            "fully_nested.py": {
                "sqlite3-dynamic-import",
                "sqlite3-connect",
                "sqlite3-cursor",
                "raw-db-execute",
            },
        }

        violations = {
            name: _database_access_violations(Path(name), source)
            for name, source in fixtures.items()
        }

        self.assertEqual(violations, expected)

    def test_ast_gate_allows_only_exact_central_sqlite_pragmas(self) -> None:
        allowed = (
            "def configure(connection):\n"
            "    cursor = connection.cursor()\n"
            "    cursor.execute('PRAGMA journal_mode')\n"
            "    cursor.execute('PRAGMA foreign_keys = ON')\n"
        )
        forbidden = (
            "def configure(connection):\n"
            "    cursor = connection.cursor()\n"
            "    cursor.execute('PRAGMA foreign_keys = OFF')\n"
        )

        self.assertEqual(
            _database_access_violations(SQLITE_POLICY, allowed),
            set(),
        )
        self.assertIn(
            "raw-db-execute",
            _database_access_violations(SQLITE_POLICY, forbidden),
        )

    def test_declarative_models_own_every_persisted_table(self) -> None:
        from sqlalchemy.orm import DeclarativeBase

        from hermes_connector.adapters.sqlite_models import Base

        self.assertTrue(issubclass(Base, DeclarativeBase))
        self.assertEqual(
            set(Base.metadata.tables),
            {
                "schema_migrations",
                "inbox_messages",
                "outbox_messages",
                "stream_cursors",
                "cloud_session_checkpoint",
                "control_commands",
                "observer_outbox",
                "transport_frame_journal",
                "owner_control_results",
                "session_catalog_outbox",
                "session_catalog_ack_receipts",
            },
        )

    def test_database_access_uses_orm_except_centralized_pragmas(self) -> None:
        files = (
            *_python_files_under(SOURCE_ROOT),
            *(
                path
                for path in _python_files_under(TEST_ROOT)
                if path != Path(__file__)
            ),
        )
        violations: dict[str, tuple[str, ...]] = {}
        for path in files:
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(CONNECTOR_ROOT)
            file_violations = _database_access_violations(path, source)
            if file_violations:
                violations[str(relative)] = tuple(sorted(file_violations))

        self.assertEqual(violations, {})

    def test_sqlite_policy_rejects_dynamic_pragma_injection_values(self) -> None:
        for value in (True, "1; PRAGMA foreign_keys = OFF", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SQLiteConnectionPolicy(busy_timeout_ms=value)  # type: ignore[arg-type]

    def test_migrations_use_versioned_alembic_operations(self) -> None:
        source = (ADAPTERS / "sqlite_migrations.py").read_text(encoding="utf-8")

        self.assertIn("Operations(", source)
        self.assertIn(".create_table(", source)
        self.assertIn(".create_index(", source)

    def test_blocking_sqlite_work_does_not_block_the_asyncio_loop(self) -> None:
        async def scenario(path: Path) -> None:
            release = threading.Event()

            def block_writer(_: str) -> None:
                release.wait(timeout=0.25)

            storage = SQLiteStorageComponent(
                path,
                ConnectorConfig(storage_write_deadline_seconds=1.0),
                write_fault=block_writer,
            )
            await storage.start()
            runner = asyncio.create_task(storage.run())
            self.assertTrue(await storage.ready())

            started = time.monotonic()
            write = asyncio.create_task(
                storage.put_inbox(
                    message_id="executor-proof",
                    digest="a" * 64,
                    state="received",
                    payload=b"{}",
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            loop_delay = time.monotonic() - started

            release.set()
            await write
            await storage.drain()
            await storage.stop()
            await runner

            self.assertLess(loop_delay, 0.1)

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_started_write_timeout_reports_effect_unknown_and_may_commit(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            from hermes_connector.domain.storage import StorageEffectUnknown

            entered = threading.Event()
            release = threading.Event()

            def block_writer(_: str) -> None:
                entered.set()
                release.wait(timeout=0.25)

            storage = SQLiteStorageComponent(
                path,
                ConnectorConfig(storage_write_deadline_seconds=0.02),
                write_fault=block_writer,
            )
            await storage.start()
            runner = asyncio.create_task(storage.run())
            self.assertTrue(await storage.ready())

            write = asyncio.create_task(
                storage.put_inbox(
                    message_id="effect-unknown",
                    digest="b" * 64,
                    state="received",
                    payload=b"{}",
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 0.1))
            with self.assertRaises(StorageEffectUnknown) as raised:
                await write
            self.assertEqual(raised.exception.code, 4307)

            release.set()
            await storage.drain()
            durable = await storage.get_inbox("effect-unknown")
            self.assertIsNotNone(durable)
            await storage.stop()
            await runner

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_reads_have_a_bounded_no_effect_deadline(self) -> None:
        async def scenario(path: Path) -> None:
            from hermes_connector.domain.storage import (
                StorageDeadlineExceeded,
                StorageEffectUnknown,
            )

            entered = threading.Event()
            release = threading.Event()

            def block_writer(_: str) -> None:
                entered.set()
                release.wait(timeout=0.25)

            storage = SQLiteStorageComponent(
                path,
                ConnectorConfig(storage_write_deadline_seconds=0.02),
                write_fault=block_writer,
            )
            await storage.start()
            runner = asyncio.create_task(storage.run())
            self.assertTrue(await storage.ready())

            write = asyncio.create_task(
                storage.put_inbox(
                    message_id="busy-writer",
                    digest="c" * 64,
                    state="received",
                    payload=b"{}",
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 0.1))
            with self.assertRaises(StorageDeadlineExceeded) as raised:
                await storage.get_inbox("not-present")
            self.assertEqual(raised.exception.code, 4306)

            release.set()
            with self.assertRaises(StorageEffectUnknown):
                await write
            await storage.drain()
            await storage.stop()
            await runner

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_cancelled_read_propagates_without_poisoning_storage(self) -> None:
        async def scenario(path: Path) -> None:
            entered = threading.Event()
            release = threading.Event()

            def block_writer(_: str) -> None:
                entered.set()
                release.wait(timeout=0.25)

            storage = SQLiteStorageComponent(
                path,
                ConnectorConfig(storage_write_deadline_seconds=1.0),
                write_fault=block_writer,
            )
            await storage.start()
            runner = asyncio.create_task(storage.run())
            self.assertTrue(await storage.ready())

            write = asyncio.create_task(
                storage.put_inbox(
                    message_id="after-cancel",
                    digest="e" * 64,
                    state="received",
                    payload=b"{}",
                )
            )
            self.assertTrue(await asyncio.to_thread(entered.wait, 0.1))

            read = asyncio.create_task(storage.get_inbox("after-cancel"))
            await asyncio.sleep(0)
            read.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await read

            release.set()
            await write
            durable = await storage.get_inbox("after-cancel")
            self.assertIsNotNone(durable)
            await storage.drain()
            await storage.stop()
            await runner

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_file_database_reuses_one_bounded_connection(self) -> None:
        async def scenario(path: Path) -> None:
            from sqlalchemy import event

            storage = SQLiteStorageComponent(path, ConnectorConfig())
            await storage.start()
            runner = asyncio.create_task(storage.run())
            self.assertTrue(await storage.ready())

            additional_connections = 0

            def connected(_: object, __: object) -> None:
                nonlocal additional_connections
                additional_connections += 1

            engine = storage._engine  # type: ignore[attr-defined]
            self.assertIsNotNone(engine)
            event.listen(engine, "connect", connected)

            await storage.put_inbox(
                message_id="pooled",
                digest="d" * 64,
                state="received",
                payload=b"{}",
            )
            await storage.get_inbox("pooled")
            await storage.diagnostics()

            self.assertEqual(additional_connections, 0)
            await storage.drain()
            await storage.stop()
            await runner

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_outbox_stream_sequence_conflict_is_not_fatal(self) -> None:
        async def scenario(path: Path) -> None:
            from hermes_connector.domain.storage import IdempotencyConflict

            storage = SQLiteStorageComponent(path, ConnectorConfig())
            await storage.start()
            runner = asyncio.create_task(storage.run())
            self.assertTrue(await storage.ready())

            await storage.append_outbox(
                message_id="out-1",
                stream="up",
                sequence=1,
                payload=b"{}",
            )
            with self.assertRaises(IdempotencyConflict) as raised:
                await storage.append_outbox(
                    message_id="out-2",
                    stream="up",
                    sequence=1,
                    payload=b"{}",
                )
            self.assertEqual(raised.exception.code, 4308)

            await storage.append_outbox(
                message_id="out-3",
                stream="up",
                sequence=2,
                payload=b"{}",
            )
            pending = await storage.pending_outbox(limit=8)
            self.assertEqual(
                [record.message_id for record in pending],
                ["out-1", "out-3"],
            )
            await storage.drain()
            await storage.stop()
            await runner

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))


if __name__ == "__main__":
    unittest.main()
