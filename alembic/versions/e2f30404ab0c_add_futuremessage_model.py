"""Add FutureMessage model

Revision ID: e2f30404ab0c
Revises: 0007_support_and_coupons
Create Date: 2026-03-15 17:02:41.036473

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2f30404ab0c'
down_revision: Union[str, Sequence[str], None] = '0007_support_and_coupons'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'future_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('media_type', sa.String(), nullable=True),
        sa.Column('media_file_id', sa.String(), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('send_date', sa.Date(), nullable=False),
        sa.Column('sent', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('future_messages')
