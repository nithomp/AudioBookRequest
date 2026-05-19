"""add goodreads_queued_book table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'goodreads_queued_book',
        sa.Column('goodreads_book_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('author', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('queued_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('asin', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='pending'),
        sa.PrimaryKeyConstraint('goodreads_book_id'),
    )


def downgrade() -> None:
    op.drop_table('goodreads_queued_book')
