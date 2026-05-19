"""goodreads per-user config

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-19 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Per-user config table
    op.create_table(
        'goodreads_user_config',
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('rss_url', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=''),
        sa.Column('auto_download', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('last_polled', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.ForeignKeyConstraint(['username'], ['user.username'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('username'),
    )

    # Recreate goodreads_queued_book with composite PK (book_id + username)
    # Drop old table (data is discarded — it was all from testing)
    op.drop_table('goodreads_queued_book')
    op.create_table(
        'goodreads_queued_book',
        sa.Column('goodreads_book_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('username', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('author', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('queued_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('asin', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='pending'),
        sa.ForeignKeyConstraint(['username'], ['user.username'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('goodreads_book_id', 'username'),
    )

    # Remove stale global goodreads keys from the Config table
    op.execute("DELETE FROM config WHERE key IN ('goodreads_rss_url', 'goodreads_auto_download', 'goodreads_last_polled')")


def downgrade() -> None:
    op.drop_table('goodreads_user_config')
    op.drop_table('goodreads_queued_book')
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
