"""create outbox messages

Revision ID: 0003_create_outbox_messages
Revises: 0002_inbox_media_fields
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_create_outbox_messages"
down_revision: Union[str, Sequence[str], None] = "0002_inbox_media_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            send_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
            text TEXT NOT NULL,
            sent_at TIMESTAMP WITHOUT TIME ZONE
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbox_messages")
