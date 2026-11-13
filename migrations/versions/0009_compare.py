"""D12 allow compare conversations without changing existing QA runs."""

from alembic import op

revision = "0009_compare"
down_revision = "0008_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.conversations DROP CONSTRAINT conversations_task_type_check")
    op.execute(
        "ALTER TABLE app.conversations ADD CONSTRAINT conversations_task_type_check CHECK (task_type IN ('qa','compare'))"
    )


def downgrade() -> None:
    # PostgreSQL rejects this atomically if compare conversations exist: no implicit deletion.
    op.execute("ALTER TABLE app.conversations DROP CONSTRAINT conversations_task_type_check")
    op.execute(
        "ALTER TABLE app.conversations ADD CONSTRAINT conversations_task_type_check CHECK (task_type='qa')"
    )
