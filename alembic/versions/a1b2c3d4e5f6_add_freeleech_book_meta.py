"""add freeleech_book_meta table

Revision ID: a1b2c3d4e5f6
Revises: cd14e5f0977c
Create Date: 2026-03-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'cd14e5f0977c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'freeleech_book_meta',
        sa.Column('lookup_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('cover_url', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('genres_json', sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default='[]'),
        sa.Column('fetched_at', sa.Float(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('lookup_key'),
    )


def downgrade() -> None:
    op.drop_table('freeleech_book_meta')
