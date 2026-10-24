"""Chat sessions, messages, cards, protocol edit proposals, and the LLM spend ledger.

Backs the chat-first redesign (docs/redesign-chat-plan.md, section B2):

  - chat_sessions            one conversation (kind: protocol | budget |
                             protocol_edit). Holds the server-owned draft `spec`
                             + `spec_version`, and the per-session turn lock.
  - chat_messages            the append-only history; `api_content` is the exact
                             Anthropic content blocks, replayed verbatim.
                             UNIQUE (session_id, seq).
  - chat_cards               structured UI cards; mutable (superseded / done)
                             without touching the model's history.
  - protocol_edit_proposals  a pending change to a finished protocol, applied
                             only on an explicit user click.
  - llm_usage                append-only spend ledger, one row per model call.

Every status-like column is a native_enum=False Enum, i.e. VARCHAR + a CHECK
constraint, with NO Postgres CREATE TYPE (this project's Alembic/async bridge
mis-handles native enum creation - see 004/009). Unlike the older tables, which
rely on SQLAlchemy's create_constraint=False default and so carry no CHECK,
these pass create_constraint=True so the database itself rejects an
out-of-range value. The CHECK is named after the enum (e.g. chat_session_kind).

Every table carries a plain `org_id` string (the org slug - never an FK to
organizations, same as protocols/budgets/study_reports) so a later row-level-
security migration can key on it.

FK delete rules: chat_sessions.created_by RESTRICT; chat_sessions.protocol_id /
budget_id SET NULL (a chat outlives the thing it produced); chat_messages /
chat_cards CASCADE from their session; chat_cards.message_id SET NULL;
protocol_edit_proposals.protocol_id CASCADE, .session_id SET NULL,
.created_by RESTRICT. llm_usage.user_id is deliberately NOT an FK so a ledger
insert can never fail for a relational reason.

Revision ID: 011
Revises: 010
Create Date: 2026-10-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(name: str, length: int, *values: str) -> sa.Enum:
    """A VARCHAR(length) + CHECK enum. Mirrors the model-side Enum(...) exactly
    (same name -> same CHECK constraint name), so autogenerate sees no drift."""
    return sa.Enum(
        *values, name=name, native_enum=False, length=length, create_constraint=True
    )


def _jsonb_default_empty_object() -> sa.TextClause:
    return sa.text("'{}'::jsonb")


def upgrade() -> None:
    # ---- chat_sessions ------------------------------------------------------
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "kind",
            _enum("chat_session_kind", 20, "protocol", "budget", "protocol_edit"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("chat_session_status", 20, "active", "completed", "archived"),
            nullable=False, server_default="active",
        ),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("spec", postgresql.JSONB(), nullable=False,
                  server_default=_jsonb_default_empty_object()),
        sa.Column("spec_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "protocol_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protocols.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "budget_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("budgets.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("turn_lock_id", sa.String(64), nullable=True),
        sa.Column("turn_lock_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_sessions_org_id", "chat_sessions", ["org_id"])
    op.create_index("ix_chat_sessions_protocol_id", "chat_sessions", ["protocol_id"])
    op.create_index("ix_chat_sessions_budget_id", "chat_sessions", ["budget_id"])
    op.create_index(
        "ix_chat_sessions_created_by_last_activity", "chat_sessions",
        ["created_by", "last_activity_at"],
    )

    # ---- chat_messages ------------------------------------------------------
    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            _enum("chat_message_role", 20,
                  "user", "assistant", "tool_results", "ui_action", "system_note"),
            nullable=False,
        ),
        sa.Column("api_content", postgresql.JSONB(), nullable=True),
        sa.Column("display", postgresql.JSONB(), nullable=False,
                  server_default=_jsonb_default_empty_object()),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("usage", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("stop_reason", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("session_id", "seq", name="uq_chat_messages_session_seq"),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])

    # ---- chat_cards ---------------------------------------------------------
    op.create_table(
        "chat_cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column(
            "message_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("type", sa.String(48), nullable=False),
        sa.Column(
            "status",
            _enum("chat_card_status", 16, "active", "superseded", "done", "error"),
            nullable=False, server_default="active",
        ),
        sa.Column("data", postgresql.JSONB(), nullable=False,
                  server_default=_jsonb_default_empty_object()),
        sa.Column("spec_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chat_cards_session_id", "chat_cards", ["session_id"])
    op.create_index("ix_chat_cards_message_id", "chat_cards", ["message_id"])

    # ---- protocol_edit_proposals -------------------------------------------
    op.create_table(
        "protocol_edit_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "protocol_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("protocols.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("org_id", sa.String(128), nullable=False),
        sa.Column(
            "session_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "kind",
            _enum("edit_proposal_kind", 16, "prose", "engine"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("edit_proposal_status", 20,
                  "proposed", "generating", "ready", "applied",
                  "discarded", "failed", "stale"),
            nullable=False, server_default="proposed",
        ),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False,
                  server_default=_jsonb_default_empty_object()),
        sa.Column("proposed_config", postgresql.JSONB(), nullable=True),
        sa.Column("proposed_sections", postgresql.JSONB(), nullable=True),
        sa.Column("change_detail", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("applied_version", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_protocol_edit_proposals_protocol_id", "protocol_edit_proposals", ["protocol_id"]
    )
    op.create_index(
        "ix_protocol_edit_proposals_session_id", "protocol_edit_proposals", ["session_id"]
    )

    # ---- llm_usage ----------------------------------------------------------
    op.create_table(
        "llm_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", sa.String(128), nullable=False),
        # Deliberately NOT a foreign key - see the module docstring.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "feature",
            _enum("llm_feature", 32,
                  "chat", "protocol_generation", "section_regen",
                  "csr_narrative", "pk_extraction"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_llm_usage_org_id_created_at", "llm_usage", ["org_id", "created_at"])
    op.create_index("ix_llm_usage_user_id_created_at", "llm_usage", ["user_id", "created_at"])


def downgrade() -> None:
    # Reverse of creation order: tables that reference others go first.
    op.drop_index("ix_llm_usage_user_id_created_at", table_name="llm_usage")
    op.drop_index("ix_llm_usage_org_id_created_at", table_name="llm_usage")
    op.drop_table("llm_usage")

    op.drop_index("ix_protocol_edit_proposals_session_id", table_name="protocol_edit_proposals")
    op.drop_index("ix_protocol_edit_proposals_protocol_id", table_name="protocol_edit_proposals")
    op.drop_table("protocol_edit_proposals")

    op.drop_index("ix_chat_cards_message_id", table_name="chat_cards")
    op.drop_index("ix_chat_cards_session_id", table_name="chat_cards")
    op.drop_table("chat_cards")

    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_sessions_created_by_last_activity", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_budget_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_protocol_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_org_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
