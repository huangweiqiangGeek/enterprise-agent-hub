"""D01: infrastructure only; business tables start in D02."""

from alembic import op

revision = "0001_infrastructure"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.execute("CREATE SCHEMA app")
    op.execute("CREATE SCHEMA checkpoints")


def downgrade() -> None:
    # Do not drop adapter-owned tables or shared extensions implicitly.
    op.execute("DROP SCHEMA checkpoints")
    op.execute("DROP SCHEMA app")
