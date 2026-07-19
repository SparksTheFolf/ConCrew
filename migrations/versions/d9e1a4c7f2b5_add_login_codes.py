"""add login_codes table

Revision ID: d9e1a4c7f2b5
Revises: c8b2f4a6d9e1
Create Date: 2026-07-19 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd9e1a4c7f2b5'
down_revision = 'c8b2f4a6d9e1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'login_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('consumed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('login_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_login_codes_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('login_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_login_codes_user_id'))
    op.drop_table('login_codes')
