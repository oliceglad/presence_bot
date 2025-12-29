"""initial

Revision ID: 0001_initial
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("consent", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_table(
        "schedule_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day_index", sa.Integer()),
        sa.Column("send_date", sa.Date()),
        sa.Column("type", sa.String()),
        sa.Column("text", sa.Text()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "inbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("tg_message_id", sa.BigInteger()),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("raw", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_table(
        "action_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("days_to_extend", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true")),
    )
    op.create_table(
        "action_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("action_rules.id")),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("old_expires_at", sa.DateTime(), nullable=True),
        sa.Column("new_expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("send_at", sa.DateTime(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("outbox_messages")
    op.drop_table("action_events")
    op.drop_table("action_rules")
    op.drop_table("subscriptions")
    op.drop_table("inbox_messages")
    op.drop_table("schedule_messages")
    op.drop_table("users")
