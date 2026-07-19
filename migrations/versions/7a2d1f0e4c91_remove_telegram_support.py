"""remove telegram support

Revision ID: 7a2d1f0e4c91
Revises: d4a3d2a0b7a1
Create Date: 2026-07-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7a2d1f0e4c91'
down_revision = 'd4a3d2a0b7a1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_telegram_handle'))
        batch_op.drop_column('telegram_handle')
        batch_op.drop_index(batch_op.f('ix_users_telegram_chat_id'))
        batch_op.drop_column('telegram_chat_id')

    with op.batch_alter_table('con_years', schema=None) as batch_op:
        batch_op.drop_column('telegram_bot_token')

    op.drop_table('telegram_registrations')


def downgrade():
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

    with op.batch_alter_table('con_years', schema=None) as batch_op:
        batch_op.add_column(sa.Column('telegram_bot_token', sa.String(length=255), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('telegram_chat_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_telegram_chat_id'), ['telegram_chat_id'], unique=True)
        batch_op.add_column(sa.Column('telegram_handle', sa.String(length=50), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_telegram_handle'), ['telegram_handle'], unique=True)