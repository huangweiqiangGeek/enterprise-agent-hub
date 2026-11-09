"""Knowledge bases and atomic, idempotent file acceptance."""

from alembic import op

revision = "0004_knowledge"
down_revision = "0003_model_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.knowledge_bases (
            id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES app.organizations(id),
            name text NOT NULL, normalized_name text NOT NULL, description text NOT NULL,
            created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
            revision bigint NOT NULL CHECK(revision > 0), deleted_at timestamptz,
            UNIQUE(organization_id,id)
        );
        CREATE UNIQUE INDEX uq_knowledge_name ON app.knowledge_bases
            (organization_id,normalized_name) WHERE deleted_at IS NULL;
        CREATE INDEX ix_knowledge_page ON app.knowledge_bases(organization_id,created_at,id);
        CREATE TABLE app.documents (
            id uuid PRIMARY KEY, organization_id uuid NOT NULL, knowledge_base_id uuid NOT NULL,
            original_name text NOT NULL, content_type text NOT NULL,
            size_bytes bigint NOT NULL CHECK(size_bytes > 0), sha256 text NOT NULL,
            object_key text NOT NULL UNIQUE, status text NOT NULL
                CHECK(status IN ('queued','processing','ready','failed')),
            current_attempt_id uuid NOT NULL, created_at timestamptz NOT NULL,
            deleted_at timestamptz, ready_at timestamptz,
            cleanup_status text NOT NULL
                CHECK(cleanup_status IN ('none','pending','done','failed')),
            cleanup_error text, UNIQUE(organization_id,id),
            FOREIGN KEY(organization_id,knowledge_base_id)
                REFERENCES app.knowledge_bases(organization_id,id)
        );
        CREATE INDEX ix_documents_page ON app.documents
            (organization_id,knowledge_base_id,created_at,id);
        CREATE INDEX ix_documents_status ON app.documents(organization_id,status);
        CREATE TABLE app.document_attempts (
            id uuid PRIMARY KEY, organization_id uuid NOT NULL, document_id uuid NOT NULL,
            attempt_no integer NOT NULL CHECK(attempt_no > 0), pipeline_version text NOT NULL,
            embedding_connection_id uuid NOT NULL, embedding_profile_id uuid NOT NULL,
            model_connection_version_id uuid NOT NULL, limits_snapshot jsonb NOT NULL,
            status text NOT NULL
                CHECK(status IN ('queued','processing','ready','failed','cancelled')),
            error_code text, created_at timestamptz NOT NULL, started_at timestamptz,
            finished_at timestamptz, lease_owner text, lease_generation bigint NOT NULL,
            lease_until timestamptz, extracted_object_key text, content_hash text,
            chunk_count integer NOT NULL CHECK(chunk_count >= 0),
            UNIQUE(document_id,attempt_no), UNIQUE(organization_id,document_id,id),
            FOREIGN KEY(organization_id,document_id) REFERENCES app.documents(organization_id,id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(organization_id,embedding_connection_id,embedding_profile_id)
                REFERENCES app.model_profiles(organization_id,connection_id,id),
            FOREIGN KEY(organization_id,embedding_connection_id,model_connection_version_id)
                REFERENCES app.model_connection_versions(organization_id,connection_id,id)
        );
        CREATE INDEX ix_document_attempts_lease ON app.document_attempts(status,lease_until);
        ALTER TABLE app.documents ADD CONSTRAINT fk_document_current_attempt
            FOREIGN KEY(organization_id,id,current_attempt_id)
                REFERENCES app.document_attempts(organization_id,document_id,id)
                DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE app.outbox_jobs (
            id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES app.organizations(id),
            job_type text NOT NULL, resource_id uuid NOT NULL, resource_version uuid NOT NULL,
            payload jsonb NOT NULL, created_at timestamptz NOT NULL,
            available_at timestamptz NOT NULL,
            publish_attempts integer NOT NULL, published_at timestamptz, lease_until timestamptz,
            lease_token uuid, last_error text,
            UNIQUE(job_type,resource_id,resource_version)
        );
        CREATE INDEX ix_outbox_pending ON app.outbox_jobs(available_at,lease_until)
            WHERE published_at IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE app.outbox_jobs;
        ALTER TABLE app.documents DROP CONSTRAINT fk_document_current_attempt;
        DROP TABLE app.document_attempts;
        DROP TABLE app.documents;
        DROP TABLE app.knowledge_bases;
    """)
