"""D13 explicit GitHub grants; no automatic migration of credentials."""

from alembic import op

revision = "0010_github"
down_revision = "0009_compare"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.github_connections (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL UNIQUE REFERENCES app.organizations(id),
            name text NOT NULL, credential_value text NOT NULL,
            enabled boolean NOT NULL, revision bigint NOT NULL CHECK(revision>0),
            tested_at timestamptz, test_result jsonb NOT NULL,
            UNIQUE(organization_id,id)
        );
        CREATE TABLE app.github_repositories (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            connection_id uuid NOT NULL, github_repo_id bigint NOT NULL CHECK(github_repo_id>0),
            owner text NOT NULL, repo text NOT NULL, enabled boolean NOT NULL,
            verified_at timestamptz NOT NULL,
            UNIQUE(organization_id,github_repo_id), UNIQUE(organization_id,id),
            FOREIGN KEY(organization_id,connection_id)
                REFERENCES app.github_connections(organization_id,id)
        );
        CREATE UNIQUE INDEX github_repository_name ON app.github_repositories
            (organization_id,lower(owner),lower(repo));
        CREATE TABLE app.org_tool_settings (
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            tool_key text NOT NULL CHECK(tool_key IN ('github.search_issues',
                'github.search_pull_requests','github.read_issue','github.read_pull_request')),
            enabled boolean NOT NULL, revision bigint NOT NULL CHECK(revision>0),
            PRIMARY KEY(organization_id,tool_key)
        );
    """)


def downgrade() -> None:
    # Never silently destroy saved credentials/grants on a rollback.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS(SELECT 1 FROM app.github_connections)
                OR EXISTS(SELECT 1 FROM app.org_tool_settings) THEN
                RAISE EXCEPTION 'GitHub configuration exists; use a forward repair';
            END IF;
        END $$;
        DROP TABLE app.org_tool_settings;
        DROP TABLE app.github_repositories;
        DROP TABLE app.github_connections;
    """)
