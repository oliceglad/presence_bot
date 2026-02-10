"""support_and_coupons

Revision ID: 0007
Revises: 0006
Create Date: 2026-02-11 02:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0007_support_and_coupons'
down_revision = '0006_action_and_reminders'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add points to users
    op.add_column('users', sa.Column('points', sa.Integer(), nullable=True, server_default='0'))

    # 2. Create support_messages table
    op.create_table('support_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('media_type', sa.String(), nullable=True),
        sa.Column('media_file_id', sa.String(), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key')
    )

    # 3. Create coupons table
    op.create_table('coupons',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('cost', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=True, server_default='true'),
        sa.PrimaryKeyConstraint('id')
    )

    # 4. Create user_coupons table
    op.create_table('user_coupons',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('coupon_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=True, server_default='active'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('redeemed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['coupon_id'], ['coupons.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('user_coupons')
    op.drop_table('coupons')
    op.drop_table('support_messages')
    op.drop_column('users', 'points')
