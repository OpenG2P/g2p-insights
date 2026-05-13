"""init insight schema

Revision ID: 0001
Revises:
Create Date: 2026-05-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS insight")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(256), nullable=False, index=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("question", sa.Text),
        sa.Column("sql", sa.Text),
        sa.Column("cost_level", sa.String(16)),
        sa.Column("estimated_rows", sa.Integer),
        sa.Column("actual_rows", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="insight",
    )

    op.create_table(
        "chat_session",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(256), nullable=False, index=True),
        sa.Column("original_question", sa.Text),
        sa.Column("messages", sa.JSON),
        sa.Column("status", sa.String(24), nullable=False, server_default="clarifying", index=True),
        sa.Column("last_intent", sa.JSON),
        sa.Column("placeholder_map", sa.JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="insight",
    )

    op.create_table(
        "saved_chart",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("superset_chart_id", sa.Integer, nullable=False, index=True),
        sa.Column("superset_dashboard_id", sa.Integer),
        sa.Column("form_data", sa.JSON, nullable=False),
        sa.Column("authored_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="insight",
    )


def downgrade() -> None:
    op.drop_table("saved_chart", schema="insight")
    op.drop_table("chat_session", schema="insight")
    op.drop_table("audit_log", schema="insight")
    op.execute("DROP SCHEMA IF EXISTS insight CASCADE")
