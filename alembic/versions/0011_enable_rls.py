"""enable RLS on public tables and close the Supabase Data API

The bot talks to Postgres directly as the table owner (``postgres``), which
bypasses RLS, so enabling it without policies changes nothing for the bot and
denies every row to the ``anon`` / ``authenticated`` roles behind the public
REST API. Grants are revoked too, and default privileges stop future tables
from being exposed again.

Revision ID: 0011_enable_rls
Revises: 0010_user_address_form
Create Date: 2026-09-28
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0011_enable_rls"
down_revision: Union[str, None] = "0010_user_address_form"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPGRADE = """
DO $$
DECLARE
    t record;
    api_roles text := (
        SELECT string_agg(quote_ident(rolname), ', ')
        FROM pg_roles WHERE rolname IN ('anon', 'authenticated')
    );
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
        IF api_roles IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %s', t.tablename, api_roles);
        END IF;
    END LOOP;
    IF api_roles IS NOT NULL THEN
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %s', api_roles);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %s', api_roles
        );
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %s', api_roles
        );
    END IF;
END $$;
"""

_DOWNGRADE = """
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY', t.tablename);
    END LOOP;
END $$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_UPGRADE)


def downgrade() -> None:
    # Grants stay revoked: re-opening the Data API is never a safe default.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_DOWNGRADE)
