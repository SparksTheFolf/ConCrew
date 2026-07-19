"""add shift swap requests table

Revision ID: a3d7e5f0c2b4
Revises: f2a6c9d1e8b3
Create Date: 2026-07-18 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3d7e5f0c2b4'
down_revision = 'f2a6c9d1e8b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'shift_swap_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('requester_id', sa.Integer(), nullable=False),
        sa.Column('requester_shift_request_id', sa.Integer(), nullable=False),
        sa.Column('target_shift_request_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['requester_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['requester_shift_request_id'], ['shift_requests.id'], ),
        sa.ForeignKeyConstraint(['target_shift_request_id'], ['shift_requests.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('shift_swap_requests')
