from unittest.mock import AsyncMock, patch

import pytest

from backend.checkpoints import setup_checkpoints
from backend.config import Settings


@pytest.mark.asyncio
async def test_checkpoint_setup_refuses_unmigrated_app_without_creating_adapter_tables() -> None:
    with (
        patch("backend.checkpoints.application_schema_ready", new=AsyncMock(return_value=False)),
        patch("backend.checkpoints.checkpoint_saver") as saver,
    ):
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            await setup_checkpoints(Settings())
        saver.assert_not_called()
