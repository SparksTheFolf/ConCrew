"""add report_to user relationship

Revision ID: e1f2a3b4c5d6
Revises: 7a2d1f0e4c91
Create Date: 2026-07-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1f2a3b4c5d6'
down_revision = '7a2d1f0e4c91'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('report_to_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_report_to_id'), ['report_to_id'], unique=False)
        batch_op.create_foreign_key('fk_users_report_to_id_users', 'users', ['report_to_id'], ['id'])


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint('fk_users_report_to_id_users', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_report_to_id'))
        batch_op.drop_column('report_to_id')
