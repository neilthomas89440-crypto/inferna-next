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

# NOTE: This migration previously dropped the unique index `ix_api_keys_key_hash`.
# That index is NOT redundant: the ORM model (ApiKey.key_hash) declares
# `unique=True, index=True`, which produces exactly one unique index and no
# separate UniqueConstraint. Dropping it left the DB missing an index the ORM
# expects, forcing Alembic check to re-add it. The d18580 migration has been
# corrected to create only the unique index (no UniqueConstraint), so this
# migration is now a no-op to avoid removing the needed index.


def upgrade() -> None:
    """No-op: unique index on api_keys.key_hash is required (see docstring)."""
    pass


def downgrade() -> None:
    """No-op: see upgrade docstring."""
    pass
