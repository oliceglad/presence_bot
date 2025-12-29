"""outbox logging

Revision ID: 0004_outbox_logging
Revises: 0003_create_outbox_messages
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_outbox_logging"
down_revision: Union[str, Sequence[str], None] = "0003_create_outbox_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("outbox_messages", sa.Column("attempts", sa.Integer(), server_default="0"))
    op.add_column("outbox_messages", sa.Column("last_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("outbox_messages", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_table(
        "outbox_send_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("outbox_id", sa.Integer(), sa.ForeignKey("outbox_messages.id")),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("outbox_send_logs")
    op.drop_column("outbox_messages", "last_error")
    op.drop_column("outbox_messages", "last_attempt_at")
    op.drop_column("outbox_messages", "attempts")
