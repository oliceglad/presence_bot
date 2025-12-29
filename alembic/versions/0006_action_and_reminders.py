"""action selection and reminders

Revision ID: 0006_action_and_reminders
Revises: 0005_send_at_drop
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_action_and_reminders"
down_revision: Union[str, Sequence[str], None] = "0005_send_at_drop"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("snooze_until", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("last_activity_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("last_inactivity_reminder_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("last_expiry_reminder_at", sa.DateTime(), nullable=True))

    op.add_column(
        "inbox_messages",
        sa.Column("action_rule_id", sa.Integer(), sa.ForeignKey("action_rules.id"), nullable=True),
    )
    op.add_column("inbox_messages", sa.Column("action_status", sa.String(), nullable=True))
    op.add_column("inbox_messages", sa.Column("action_reviewed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("inbox_messages", "action_reviewed_at")
    op.drop_column("inbox_messages", "action_status")
    op.drop_column("inbox_messages", "action_rule_id")

    op.drop_column("users", "last_expiry_reminder_at")
    op.drop_column("users", "last_inactivity_reminder_at")
    op.drop_column("users", "last_activity_at")
    op.drop_column("users", "snooze_until")
