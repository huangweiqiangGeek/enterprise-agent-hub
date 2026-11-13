"""D08 empty database migration roundtrip; never downgrade the experience database."""

import pytest
from alembic import command
from test_identity import connection
from test_identity import database_url as database_url

from backend.db import migration_config

pytestmark = pytest.mark.integration


def test_run_migration_roundtrip(database_url: str) -> None:
    command.downgrade(migration_config(), "0006_agents")
    with connection(database_url) as db:
        assert db.execute("SELECT to_regclass('app.runs')").fetchone() == (None,)
    command.upgrade(migration_config(), "head")
    with connection(database_url) as db:
        assert db.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0011_progress",
        )
        assert db.execute("SELECT count(*) FROM app.runs").fetchone() == (0,)
