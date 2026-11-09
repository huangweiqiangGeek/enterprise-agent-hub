"""Downgrade/re-upgrade only in an empty fixture database, never the experience database."""

import pytest
from alembic import command
from test_identity import connection
from test_identity import database_url as database_url

from backend.db import migration_config

pytestmark = pytest.mark.integration


def test_empty_ingestion_migration_roundtrip(database_url: str) -> None:
    command.downgrade(migration_config(), "0004_knowledge")
    with connection(database_url) as db:
        assert db.execute("SELECT to_regclass('app.document_chunks')").fetchone() == (None,)
    command.upgrade(migration_config(), "head")
    with connection(database_url) as db:
        assert db.execute("SELECT to_regclass('app.document_chunks')").fetchone() == (
            "app.document_chunks",
        )
