"""D09 optimistic conversation titles; existing conversations retain their identity."""

from alembic import op

revision = "0008_conversations"
down_revision = "0007_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.conversations ADD COLUMN revision bigint NOT NULL DEFAULT 1 "
        "CHECK (revision > 0)"
    )
    op.execute(
        "CREATE INDEX ix_conversations_owner_page ON app.conversations"
        "(organization_id, owner_user_id, updated_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.ix_conversations_owner_page")
    op.execute("ALTER TABLE app.conversations DROP COLUMN revision")
