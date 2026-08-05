from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import SQLiteDatabaseProbe
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata


def test_sqlite_readiness_probe_queries_real_file_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{database}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    database.chmod(0o660)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    probe = SQLiteDatabaseProbe(factory)
    try:
        asyncio.run(probe.check())
        assert probe.name == "sqlite"
        assert probe.critical is True
        assert 0 < probe.deadline_seconds <= 3
    finally:
        engine.dispose()
