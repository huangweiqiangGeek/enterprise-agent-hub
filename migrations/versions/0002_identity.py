"""D02 identity, organization ownership, idempotency and audit."""

from alembic import op

revision = "0002_identity"
down_revision = "0001_infrastructure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.users (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL, revision bigint NOT NULL CHECK (revision > 0),
            normalized_email text NOT NULL UNIQUE,
            display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 100),
            password_hash text NOT NULL, password_changed_at timestamptz NOT NULL
        );
        CREATE TABLE app.auth_sessions (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            user_id uuid REFERENCES app.users(id), token_hash text NOT NULL UNIQUE,
            csrf_hash text NOT NULL, expires_at timestamptz NOT NULL,
            last_seen_at timestamptz NOT NULL, revoked_at timestamptz
        );
        CREATE INDEX ix_auth_sessions_user_expires ON app.auth_sessions(user_id, expires_at);
        CREATE TABLE app.organizations (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
            owner_user_id uuid NOT NULL,
            access_revision bigint NOT NULL CHECK (access_revision > 0),
            settings_revision bigint NOT NULL CHECK (settings_revision > 0)
        );
        CREATE TABLE app.org_memberships (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL, revision bigint NOT NULL CHECK (revision > 0),
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            user_id uuid NOT NULL REFERENCES app.users(id),
            base_role text NOT NULL CONSTRAINT ck_membership_role
                CHECK (base_role IN ('admin','member')),
            joined_at timestamptz NOT NULL,
            CONSTRAINT uq_org_membership UNIQUE (organization_id, user_id)
        );
        CREATE INDEX ix_memberships_user ON app.org_memberships(user_id);
        CREATE INDEX ix_memberships_page
            ON app.org_memberships(organization_id, created_at, id);
        ALTER TABLE app.organizations ADD CONSTRAINT fk_organizations_owner_membership
            FOREIGN KEY (id, owner_user_id)
            REFERENCES app.org_memberships(organization_id, user_id)
            DEFERRABLE INITIALLY DEFERRED;
        CREATE TABLE app.idempotency_keys (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid REFERENCES app.organizations(id),
            actor_id uuid NOT NULL REFERENCES app.users(id), operation text NOT NULL,
            resource_id uuid, key text NOT NULL, request_digest text NOT NULL,
            response_status integer NOT NULL, response_data jsonb NOT NULL, target_id uuid NOT NULL,
            CONSTRAINT uq_idempotency_scope UNIQUE NULLS NOT DISTINCT
                (organization_id, actor_id, operation, resource_id, key)
        );
        CREATE TABLE app.audit_entries (
            id uuid PRIMARY KEY, created_at timestamptz NOT NULL,
            organization_id uuid NOT NULL REFERENCES app.organizations(id),
            actor_id uuid NOT NULL REFERENCES app.users(id), action text NOT NULL,
            target_type text NOT NULL, target_id uuid NOT NULL,
            before_summary jsonb NOT NULL, after_summary jsonb NOT NULL,
            request_id text NOT NULL, occurred_at timestamptz NOT NULL, outcome text NOT NULL
        );
        CREATE INDEX ix_audit_org_created ON app.audit_entries(organization_id, created_at);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE app.audit_entries;
        DROP TABLE app.idempotency_keys;
        ALTER TABLE app.organizations DROP CONSTRAINT fk_organizations_owner_membership;
        DROP TABLE app.org_memberships;
        DROP TABLE app.organizations;
        DROP TABLE app.auth_sessions;
        DROP TABLE app.users;
    """)
