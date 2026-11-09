"""D07 Agent identities, immutable configurations and tenant-scoped bindings."""

from alembic import op

revision = "0006_agents"
down_revision = "0005_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.agents (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL, revision bigint NOT NULL CHECK(revision > 0),
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            name text NOT NULL, description text NOT NULL,
            task_type text NOT NULL CHECK(task_type IN ('qa','compare','progress')),
            enabled boolean NOT NULL, current_version_id uuid NOT NULL,
            disabled_at timestamptz, UNIQUE(organization_id,id)
        );
        CREATE INDEX ix_agents_page ON app.agents(organization_id,created_at,id);
        CREATE TABLE app.agent_versions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL, agent_id uuid NOT NULL,
            version_no integer NOT NULL CHECK(version_no > 0),
            name text NOT NULL, description text NOT NULL, prompt_text text NOT NULL,
            chat_connection_id uuid, parameters jsonb NOT NULL,
            created_by uuid NOT NULL REFERENCES app.users(id),
            UNIQUE(agent_id,version_no), UNIQUE(organization_id,id),
            UNIQUE(organization_id,agent_id,id),
            FOREIGN KEY(organization_id,agent_id) REFERENCES app.agents(organization_id,id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(organization_id,chat_connection_id)
                REFERENCES app.model_connections(organization_id,id)
        );
        ALTER TABLE app.agents ADD CONSTRAINT fk_agent_current_version
            FOREIGN KEY(organization_id,id,current_version_id)
            REFERENCES app.agent_versions(organization_id,agent_id,id)
                DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE app.agent_version_kbs (
            organization_id uuid NOT NULL, agent_version_id uuid NOT NULL,
            knowledge_base_id uuid NOT NULL, PRIMARY KEY(agent_version_id,knowledge_base_id),
            FOREIGN KEY(organization_id,agent_version_id)
                REFERENCES app.agent_versions(organization_id,id),
            FOREIGN KEY(organization_id,knowledge_base_id)
                REFERENCES app.knowledge_bases(organization_id,id)
        );
        CREATE INDEX ix_agent_kbs_resource
            ON app.agent_version_kbs(organization_id,knowledge_base_id);
        CREATE TABLE app.agent_version_tools (
            organization_id uuid NOT NULL, agent_version_id uuid NOT NULL,
            tool_key text NOT NULL CHECK(tool_key IN ('knowledge.search','github.search_issues',
                'github.search_pull_requests','github.read_issue','github.read_pull_request')),
            PRIMARY KEY(agent_version_id,tool_key),
            FOREIGN KEY(organization_id,agent_version_id)
                REFERENCES app.agent_versions(organization_id,id)
        );
        CREATE FUNCTION app.reject_agent_version_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'agent configurations are immutable'; END $$;
        CREATE TRIGGER agent_version_immutable BEFORE UPDATE OR DELETE ON app.agent_versions
            FOR EACH ROW EXECUTE FUNCTION app.reject_agent_version_change();
        CREATE TRIGGER agent_kbs_immutable BEFORE UPDATE OR DELETE ON app.agent_version_kbs
            FOR EACH ROW EXECUTE FUNCTION app.reject_agent_version_change();
        CREATE TRIGGER agent_tools_immutable BEFORE UPDATE OR DELETE ON app.agent_version_tools
            FOR EACH ROW EXECUTE FUNCTION app.reject_agent_version_change();
        CREATE FUNCTION app.reject_agent_type_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.task_type IS DISTINCT FROM OLD.task_type THEN
                RAISE EXCEPTION 'agent task type is immutable';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER agent_type_immutable BEFORE UPDATE ON app.agents
            FOR EACH ROW EXECUTE FUNCTION app.reject_agent_type_change();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE app.agent_version_tools;
        DROP TABLE app.agent_version_kbs;
        ALTER TABLE app.agents DROP CONSTRAINT fk_agent_current_version;
        DROP TABLE app.agent_versions;
        DROP TABLE app.agents;
        DROP FUNCTION app.reject_agent_version_change();
        DROP FUNCTION app.reject_agent_type_change();
    """)
