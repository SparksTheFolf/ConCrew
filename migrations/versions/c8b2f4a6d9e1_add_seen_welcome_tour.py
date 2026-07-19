"""add users.seen_welcome_tour

Revision ID: c8b2f4a6d9e1
Revises: a3d7e5f0c2b4
Create Date: 2026-07-18 00:00:02.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8b2f4a6d9e1'
down_revision = 'a3d7e5f0c2b4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seen_welcome_tour', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('seen_welcome_tour')
