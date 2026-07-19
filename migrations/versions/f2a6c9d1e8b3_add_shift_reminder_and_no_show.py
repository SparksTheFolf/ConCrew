"""add shift request reminder_sent_at column

Revision ID: f2a6c9d1e8b3
Revises: e1f2a3b4c5d6
Create Date: 2026-07-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2a6c9d1e8b3'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('shift_requests', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reminder_sent_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('shift_requests', schema=None) as batch_op:
        batch_op.drop_column('reminder_sent_at')
