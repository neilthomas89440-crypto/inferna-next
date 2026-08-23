"""drop redundant unique index on api_keys.key_hash

Revision ID: 7a3b9c1d5e2f
Revises: d18580a27109
Create Date: 2026-08-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7a3b9c1d5e2f'
down_revision: Union[str, Sequence[str], None] = 'd18580a27109'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f('ix_api_keys_key_hash'), table_name='api_keys')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_index(op.f('ix_api_keys_key_hash'), 'api_keys', ['key_hash'], unique=True)
