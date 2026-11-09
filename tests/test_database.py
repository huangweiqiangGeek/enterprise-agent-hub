import os

import pytest

from scripts.probe_database import run_smoke


@pytest.mark.integration
def test_empty_database_migration_vector_and_checkpoint_resume() -> None:
    admin_url = os.getenv("EAH_TEST_ADMIN_URL")
    if not admin_url:
        pytest.skip("UNVERIFIED: set EAH_TEST_ADMIN_URL to an isolated PostgreSQL admin connection")
    result = run_smoke(admin_url)
    assert result["checkpointResume"] == "PASS"
