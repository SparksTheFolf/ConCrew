"""add telegram handle registration fields

Revision ID: d4a3d2a0b7a1
Revises: c1e4c8e27f93
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd4a3d2a0b7a1'
down_revision = 'c1e4c8e27f93'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('telegram_handle', sa.String(length=50), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_telegram_handle'), ['telegram_handle'], unique=True)

    op.create_table(
        'telegram_registrations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('telegram_handle', sa.String(length=50), nullable=False),
        sa.Column('telegram_chat_id', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('telegram_chat_id'),
        sa.UniqueConstraint('telegram_handle')
    )


def downgrade():
    op.drop_table('telegram_registrations')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_telegram_handle'))
        batch_op.drop_column('telegram_handle')