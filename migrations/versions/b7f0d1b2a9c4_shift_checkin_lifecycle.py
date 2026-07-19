"""add shift check-in lifecycle fields

Revision ID: b7f0d1b2a9c4
Revises: 6d8b5a3f0314
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7f0d1b2a9c4'
down_revision = '6d8b5a3f0314'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('shift_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('checked_in_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('checked_out_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('shift_requests', schema=None) as batch_op:
        batch_op.drop_column('checked_out_at')
        batch_op.drop_column('checked_in_at')