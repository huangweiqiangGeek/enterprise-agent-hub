"""Version-bound chunks and extracted text size; no changes to accepted user content."""

from alembic import op

revision = "0005_ingestion"
down_revision = "0004_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE app.document_attempts ADD COLUMN extracted_size integer;
        ALTER TABLE app.documents ADD CONSTRAINT uq_document_kb
            UNIQUE(organization_id,knowledge_base_id,id);
        ALTER TABLE app.model_profiles ADD CONSTRAINT uq_embedding_shape
            UNIQUE(organization_id,id,dimension);
        ALTER TABLE app.document_attempts ADD CONSTRAINT uq_attempt_profile
            UNIQUE(organization_id,document_id,id,embedding_profile_id);
        CREATE TABLE app.document_chunks (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL, knowledge_base_id uuid NOT NULL,
            document_id uuid NOT NULL, attempt_id uuid NOT NULL,
            ordinal integer NOT NULL CHECK(ordinal >= 0), text text, text_hash text NOT NULL,
            locator jsonb NOT NULL, lexemes tsvector NOT NULL,
            embedding public.vector, embedding_profile_id uuid NOT NULL,
            embedding_dimensions integer NOT NULL CHECK(embedding_dimensions > 0),
            UNIQUE(attempt_id,ordinal),
            FOREIGN KEY(organization_id,knowledge_base_id,document_id)
                REFERENCES app.documents(organization_id,knowledge_base_id,id),
            FOREIGN KEY(organization_id,document_id,attempt_id,embedding_profile_id)
                REFERENCES app.document_attempts
                    (organization_id,document_id,id,embedding_profile_id),
            FOREIGN KEY(organization_id,embedding_profile_id,embedding_dimensions)
                REFERENCES app.model_profiles(organization_id,id,dimension),
            CHECK((text IS NULL AND embedding IS NULL) OR
                  (text IS NOT NULL AND length(text)>0 AND embedding IS NOT NULL
                   AND public.vector_dims(embedding)=embedding_dimensions))
        );
        CREATE INDEX ix_chunks_scope ON app.document_chunks
            (organization_id,knowledge_base_id,document_id,attempt_id,ordinal);
        CREATE INDEX ix_chunks_lexemes ON app.document_chunks USING gin(lexemes);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE app.document_chunks;
        ALTER TABLE app.document_attempts DROP CONSTRAINT uq_attempt_profile;
        ALTER TABLE app.model_profiles DROP CONSTRAINT uq_embedding_shape;
        ALTER TABLE app.documents DROP CONSTRAINT uq_document_kb;
        ALTER TABLE app.document_attempts DROP COLUMN extracted_size;
    """)
