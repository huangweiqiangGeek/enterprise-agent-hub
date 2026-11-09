"""D03 immutable model versions, organization limits and metering."""

from alembic import op

revision = "0003_model_settings"
down_revision = "0002_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.org_settings (
            organization_id uuid PRIMARY KEY REFERENCES app.organizations(id),
            limits jsonb NOT NULL, revision bigint NOT NULL CHECK(revision > 0)
        );
        CREATE TABLE app.model_connections (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            kind text NOT NULL CHECK(kind IN ('chat','embedding','rerank')),
            name text NOT NULL, active_profile_id uuid NOT NULL,
            current_connection_version_id uuid NOT NULL, enabled boolean NOT NULL,
            tested_at timestamptz NOT NULL, test_result jsonb NOT NULL,
            revision bigint NOT NULL CHECK(revision > 0),
            UNIQUE(organization_id,kind), UNIQUE(organization_id,id)
        );
        CREATE TABLE app.model_connection_versions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL, connection_id uuid NOT NULL,
            version_no integer NOT NULL CHECK(version_no > 0),
            provider_protocol text NOT NULL, base_url text NOT NULL, credential_value text NOT NULL,
            UNIQUE(connection_id,version_no), UNIQUE(organization_id,connection_id,id),
            FOREIGN KEY(organization_id,connection_id)
                REFERENCES app.model_connections(organization_id,id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE app.model_profiles (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL, connection_id uuid NOT NULL,
            provider_protocol text NOT NULL, base_url text NOT NULL, model_name text NOT NULL,
            dimension integer CHECK(dimension > 0), tokenizer_id text,
            capabilities jsonb NOT NULL, parameters jsonb NOT NULL,
            version integer NOT NULL CHECK(version > 0),
            UNIQUE(connection_id,version), UNIQUE(organization_id,connection_id,id),
            CHECK(provider_protocol != 'openai-embeddings' OR dimension IS NOT NULL),
            FOREIGN KEY(organization_id,connection_id)
                REFERENCES app.model_connections(organization_id,id)
                DEFERRABLE INITIALLY DEFERRED
        );
        ALTER TABLE app.model_connections ADD CONSTRAINT fk_model_active_profile
            FOREIGN KEY(organization_id,id,active_profile_id)
            REFERENCES app.model_profiles(organization_id,connection_id,id)
                DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE app.model_connections ADD CONSTRAINT fk_model_current_version
            FOREIGN KEY(organization_id,id,current_connection_version_id)
            REFERENCES app.model_connection_versions(organization_id,connection_id,id)
                DEFERRABLE INITIALLY DEFERRED;
        CREATE FUNCTION app.reject_model_version_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'model versions are immutable'; END $$;
        CREATE TRIGGER model_profile_immutable BEFORE UPDATE ON app.model_profiles
            FOR EACH ROW EXECUTE FUNCTION app.reject_model_version_update();
        CREATE TRIGGER model_connection_version_immutable
            BEFORE UPDATE ON app.model_connection_versions
            FOR EACH ROW EXECUTE FUNCTION app.reject_model_version_update();
        CREATE TABLE app.usage_entries (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            run_id uuid, step_id uuid,
            operation_kind text NOT NULL, operation_id uuid NOT NULL, attempt_no integer NOT NULL,
            provider_request_id text, model_profile_id uuid REFERENCES app.model_profiles(id),
            reserved_tokens integer NOT NULL CHECK(reserved_tokens >= 0),
            input_tokens integer CHECK(input_tokens >= 0),
            output_tokens integer CHECK(output_tokens >= 0),
            token_source text NOT NULL CHECK(token_source IN ('actual','reserved','unavailable')),
            billed_units jsonb NOT NULL, duration_ms integer NOT NULL, status text NOT NULL,
            UNIQUE(operation_kind,operation_id,attempt_no)
        );
        CREATE INDEX ix_usage_org_created ON app.usage_entries(organization_id,created_at);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE app.usage_entries;
        ALTER TABLE app.model_connections DROP CONSTRAINT fk_model_active_profile;
        ALTER TABLE app.model_connections DROP CONSTRAINT fk_model_current_version;
        DROP TABLE app.model_profiles;
        DROP TABLE app.model_connection_versions;
        DROP FUNCTION app.reject_model_version_update();
        DROP TABLE app.model_connections;
        DROP TABLE app.org_settings;
    """)
