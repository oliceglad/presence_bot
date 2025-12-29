"""schedule send_at and drop outbox tables

Revision ID: 0005_send_at_drop
Revises: 0004_outbox_logging
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_send_at_drop"
down_revision: Union[str, Sequence[str], None] = "0004_outbox_logging"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("schedule_messages", sa.Column("send_at", sa.DateTime(), nullable=True))
    op.add_column("schedule_messages", sa.Column("attempts", sa.Integer(), server_default="0"))
    op.add_column("schedule_messages", sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("schedule_messages", sa.Column("last_error", sa.Text(), nullable=True))

    op.execute("DROP TABLE IF EXISTS outbox_send_logs")
    op.execute("DROP TABLE IF EXISTS outbox_messages")


def downgrade() -> None:
    op.drop_column("schedule_messages", "last_error")
    op.drop_column("schedule_messages", "last_attempt_at")
    op.drop_column("schedule_messages", "attempts")
    op.drop_column("schedule_messages", "send_at")
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()),
        sa.Column("send_at", sa.DateTime(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_table(
        "outbox_send_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("outbox_id", sa.Integer()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )
