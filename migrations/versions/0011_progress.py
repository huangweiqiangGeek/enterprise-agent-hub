"""D14 progress and tenant-safe GitHub run evidence."""

from alembic import op

revision = "0011_progress"
down_revision = "0010_github"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.conversations DROP CONSTRAINT conversations_task_type_check;
        ALTER TABLE app.conversations ADD CONSTRAINT conversations_task_type_check CHECK(task_type IN ('qa','compare','progress'));
        ALTER TABLE app.run_sources ALTER COLUMN document_id DROP NOT NULL;
        ALTER TABLE app.run_sources ALTER COLUMN attempt_id DROP NOT NULL;
        ALTER TABLE app.run_sources ALTER COLUMN chunk_id DROP NOT NULL;
        ALTER TABLE app.run_sources ADD COLUMN source_kind text NOT NULL DEFAULT 'document',
            ADD COLUMN repository_id uuid, ADD COLUMN external_type text,
            ADD COLUMN external_number integer, ADD COLUMN external_url text;
        ALTER TABLE app.run_sources ADD CONSTRAINT source_repository_fk FOREIGN KEY(organization_id,repository_id) REFERENCES app.github_repositories(organization_id,id);
        ALTER TABLE app.run_sources ADD CONSTRAINT source_kind_check CHECK(
            (source_kind='document' AND document_id IS NOT NULL AND attempt_id IS NOT NULL AND chunk_id IS NOT NULL AND repository_id IS NULL AND external_type IS NULL AND external_number IS NULL AND external_url IS NULL)
            OR (source_kind='github' AND document_id IS NULL AND attempt_id IS NULL AND chunk_id IS NULL AND repository_id IS NOT NULL AND external_type IS NOT NULL AND external_number IS NOT NULL AND external_type IN ('issue','pr') AND external_number>0 AND external_url IS NOT NULL));
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE app.conversations DROP CONSTRAINT conversations_task_type_check;
        ALTER TABLE app.conversations ADD CONSTRAINT conversations_task_type_check CHECK(task_type IN ('qa','compare'));
        ALTER TABLE app.run_sources ALTER COLUMN document_id SET NOT NULL;
        ALTER TABLE app.run_sources ALTER COLUMN attempt_id SET NOT NULL;
        ALTER TABLE app.run_sources ALTER COLUMN chunk_id SET NOT NULL;
        ALTER TABLE app.run_sources DROP CONSTRAINT source_kind_check;
        ALTER TABLE app.run_sources DROP CONSTRAINT source_repository_fk;
        ALTER TABLE app.run_sources DROP COLUMN source_kind, DROP COLUMN repository_id,
            DROP COLUMN external_type, DROP COLUMN external_number, DROP COLUMN external_url;
    """)
