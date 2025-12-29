"""inbox media fields

Revision ID: 0002_inbox_media_fields
Revises: 0001_initial
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_inbox_media_fields"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inbox_messages", sa.Column("media_type", sa.String(), nullable=True))
    op.add_column("inbox_messages", sa.Column("media_file_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("inbox_messages", "media_file_id")
    op.drop_column("inbox_messages", "media_type")
