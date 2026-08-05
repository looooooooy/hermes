from __future__ import annotations

from pathlib import Path

from hermes_cloud.platform.postgres.catalog import PUBLISHED_POSTGRES_MIGRATIONS

README = Path(__file__).resolve().parents[1] / "README.md"


def test_module_readme_is_the_orm_only_cloud_entrypoint() -> None:
    readme = README.read_text()
    postgres_head = max(PUBLISHED_POSTGRES_MIGRATIONS, key=lambda m: m.version)

    assert "ORM-only" in readme
    assert f"SQLite v{postgres_head.version}" in readme
    assert f"PostgreSQL v{postgres_head.version}: `{postgres_head.name}`" in readme
    assert "deploy/test_server/README.md" in readme
    assert "deploy/test_server/sqlite/README.md" in readme
    assert "cleanup_test_seed_session.py" in readme
    assert "metadata.create_all" not in readme
