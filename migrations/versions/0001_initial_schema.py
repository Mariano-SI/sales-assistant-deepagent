"""Tabelas de produto: conversations e messages.

Escrita à mão (não autogerada) porque é a revisão inicial. O `include_name` de
migrations/env.py garante que as tabelas do LangGraph — criadas por
`saver.setup()` no boot da API, não pelo Alembic — nunca apareçam aqui.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # A sidebar ordena por updated_at desc — sem índice isso vira seq scan.
    op.create_index(
        op.f("ix_conversations_updated_at"), "conversations", ["updated_at"]
    )

    op.create_table(
        "messages",
        # BIGSERIAL: a ordem de inserção já é a ordem cronológica da conversa.
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_conversations_updated_at"), table_name="conversations")
    op.drop_table("conversations")
