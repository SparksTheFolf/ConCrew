"""add telegram notification fields

Revision ID: c1e4c8e27f93
Revises: b7f0d1b2a9c4
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1e4c8e27f93'
down_revision = 'b7f0d1b2a9c4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('con_years', schema=None) as batch_op:
        batch_op.add_column(sa.Column('telegram_bot_token', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('telegram_chat_id', sa.String(length=64), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('telegram_chat_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_telegram_chat_id'), ['telegram_chat_id'], unique=True)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_telegram_chat_id'))
        batch_op.drop_column('telegram_chat_id')

    with op.batch_alter_table('con_years', schema=None) as batch_op:
        batch_op.drop_column('telegram_chat_id')
        batch_op.drop_column('telegram_bot_token')