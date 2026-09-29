"""The framework's own tables, and the service's migrations leaving them alone.

The plugins create ``jfast_*`` tables at startup. They are not in the
service's models, so without a filter ``alembic revision --autogenerate``
reads them as tables the service deleted and writes a migration dropping them
-- the queue's jobs included, which is how a routine migration empties it.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from jfastframework.cli.scaffold import Scaffolder
from jfastframework.db.framework import include_name, is_framework_table


def _dropped(include: object | None) -> set[str]:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        for name in ("jfast_jobs", "jfast_outbox", "jfast_users", "leftover"):
            conn.execute(sa.text(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)"))
        opts = {"include_name": include} if include else {}
        context = MigrationContext.configure(conn, opts=opts)
        diff = compare_metadata(context, sa.MetaData())
    return {entry[1].name for entry in diff if entry[0] == "remove_table"}


def test_without_the_filter_autogenerate_drops_the_framework_tables() -> None:
    assert {"jfast_jobs", "jfast_outbox", "jfast_users"} <= _dropped(None)


def test_with_it_only_the_service_s_own_tables_are_compared() -> None:
    assert _dropped(include_name) == {"leftover"}


def test_the_generated_env_installs_it_for_both_modes() -> None:
    source = (
        Scaffolder()
        .env.get_template("service_base/migrations/env.py.j2")
        .render(service_title="Test")
    )
    assert "from jfastframework.db.framework import include_name" in source
    assert source.count("include_name=include_name") == 2


def test_the_prefix_is_the_rule() -> None:
    assert is_framework_table("jfast_idempotency")
    assert not is_framework_table("jfastish")
    assert not is_framework_table(None)


def test_every_framework_table_carries_the_prefix(tmp_path: Path) -> None:
    import jfastframework.accounts.models
    import jfastframework.idempotency
    import jfastframework.outbox  # noqa: F401 - registers the tables
    from jfastframework.db.framework import framework_metadata

    assert framework_metadata.tables
    assert all(is_framework_table(name) for name in framework_metadata.tables)
