"""D08 durable QA runs, tenant constraints and unique terminal outcomes."""

from alembic import op

revision = "0007_runs"
down_revision = "0006_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE app.document_chunks ADD CONSTRAINT uq_chunk_source
        UNIQUE(organization_id,document_id,attempt_id,id);
    CREATE TABLE app.conversations (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
        organization_id uuid NOT NULL REFERENCES app.organizations(id),
        owner_user_id uuid NOT NULL REFERENCES app.users(id), agent_id uuid NOT NULL,
        task_type text NOT NULL CHECK(task_type='qa'), title text NOT NULL,
        updated_at timestamptz NOT NULL, UNIQUE(organization_id,id),
        FOREIGN KEY(organization_id,agent_id) REFERENCES app.agents(organization_id,id)
    );
    CREATE INDEX ix_conversations_owner
        ON app.conversations(organization_id,owner_user_id,updated_at,id);
    CREATE TABLE app.runs (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
        organization_id uuid NOT NULL, conversation_id uuid NOT NULL,
        initiator_id uuid NOT NULL REFERENCES app.users(id), agent_version_id uuid NOT NULL,
        status text NOT NULL
            CHECK(status IN ('queued','running','succeeded','failed','cancelled','timed_out')),
        cancel_requested boolean NOT NULL, input_snapshot jsonb NOT NULL,
        config_snapshot jsonb NOT NULL, budget_snapshot jsonb NOT NULL, coverage jsonb NOT NULL,
        started_at timestamptz, deadline_at timestamptz, finished_at timestamptz,
        last_seq bigint NOT NULL DEFAULT 0, lease_generation bigint NOT NULL DEFAULT 0,
        lease_until timestamptz, lease_owner text, retry_of_run_id uuid,
        error_code text, graph_version text NOT NULL,
        UNIQUE(organization_id,id), UNIQUE(organization_id,conversation_id,id),
        FOREIGN KEY(organization_id,conversation_id)
            REFERENCES app.conversations(organization_id,id),
        FOREIGN KEY(organization_id,agent_version_id)
            REFERENCES app.agent_versions(organization_id,id),
        FOREIGN KEY(organization_id,conversation_id,retry_of_run_id)
            REFERENCES app.runs(organization_id,conversation_id,id)
    );
    CREATE UNIQUE INDEX uq_conversation_active_run ON app.runs(conversation_id)
        WHERE status IN ('queued','running');
    CREATE INDEX ix_run_queue ON app.runs(organization_id,status,created_at);
    CREATE INDEX ix_run_lease ON app.runs(status,lease_until);
    CREATE TABLE app.messages (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL, organization_id uuid NOT NULL,
        conversation_id uuid NOT NULL, run_id uuid NOT NULL,
        role text NOT NULL CHECK(role IN ('user','assistant')),
        content text NOT NULL, result_json jsonb, is_partial boolean NOT NULL,
        UNIQUE(run_id,role), UNIQUE(organization_id,run_id,id),
        FOREIGN KEY(organization_id,conversation_id,run_id)
            REFERENCES app.runs(organization_id,conversation_id,id)
    );
    CREATE TABLE app.run_steps (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL, organization_id uuid NOT NULL,
        run_id uuid NOT NULL, sequence bigint NOT NULL, logical_step_key text NOT NULL,
        attempt_no integer NOT NULL,
        kind text NOT NULL CHECK(kind IN ('model','tool','validation')),
        name text NOT NULL, input_digest text NOT NULL,
        input_summary jsonb NOT NULL, output_summary jsonb NOT NULL,
        status text NOT NULL, error_code text, started_at timestamptz NOT NULL,
        finished_at timestamptz, lease_generation bigint NOT NULL,
        UNIQUE(run_id,sequence), UNIQUE(run_id,logical_step_key,attempt_no),
        UNIQUE(organization_id,run_id,id),
        FOREIGN KEY(organization_id,run_id) REFERENCES app.runs(organization_id,id)
    );
    CREATE TABLE app.run_sources (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL, organization_id uuid NOT NULL,
        run_id uuid NOT NULL, identity_key text NOT NULL, document_id uuid NOT NULL,
        attempt_id uuid NOT NULL, chunk_id uuid NOT NULL, title text NOT NULL,
        excerpt text, content_hash text NOT NULL, locator jsonb NOT NULL,
        observed_at timestamptz NOT NULL,
        UNIQUE(run_id,identity_key), UNIQUE(organization_id,run_id,id),
        FOREIGN KEY(organization_id,run_id) REFERENCES app.runs(organization_id,id),
        FOREIGN KEY(organization_id,document_id) REFERENCES app.documents(organization_id,id),
        FOREIGN KEY(organization_id,document_id,attempt_id)
            REFERENCES app.document_attempts(organization_id,document_id,id),
        FOREIGN KEY(organization_id,document_id,attempt_id,chunk_id)
            REFERENCES app.document_chunks(organization_id,document_id,attempt_id,id)
    );
    CREATE TABLE app.run_citations (
        id uuid PRIMARY KEY, created_at timestamptz NOT NULL, organization_id uuid NOT NULL,
        run_id uuid NOT NULL, message_id uuid NOT NULL, source_id uuid NOT NULL,
        label integer NOT NULL, claim_path text NOT NULL, UNIQUE(run_id,label),
        FOREIGN KEY(organization_id,run_id,message_id)
            REFERENCES app.messages(organization_id,run_id,id),
        FOREIGN KEY(organization_id,run_id,source_id)
            REFERENCES app.run_sources(organization_id,run_id,id)
    );
    CREATE TABLE app.run_events (
        organization_id uuid NOT NULL, run_id uuid NOT NULL, seq bigint NOT NULL,
        type text NOT NULL, occurred_at timestamptz NOT NULL, data jsonb NOT NULL,
        PRIMARY KEY(run_id,seq),
        FOREIGN KEY(organization_id,run_id) REFERENCES app.runs(organization_id,id)
    );
    CREATE UNIQUE INDEX uq_run_terminal_event ON app.run_events(run_id)
        WHERE type IN ('run.completed','run.failed','run.cancelled','run.timed_out');
    ALTER TABLE app.usage_entries ADD CONSTRAINT fk_usage_run
        FOREIGN KEY(organization_id,run_id) REFERENCES app.runs(organization_id,id);
    ALTER TABLE app.usage_entries ADD CONSTRAINT fk_usage_step
        FOREIGN KEY(organization_id,run_id,step_id)
            REFERENCES app.run_steps(organization_id,run_id,id);
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE app.usage_entries DROP CONSTRAINT fk_usage_step;
    ALTER TABLE app.usage_entries DROP CONSTRAINT fk_usage_run;
    DROP TABLE app.run_events, app.run_citations, app.run_sources, app.run_steps,
        app.messages, app.runs, app.conversations;
    ALTER TABLE app.document_chunks DROP CONSTRAINT uq_chunk_source;
    """)
