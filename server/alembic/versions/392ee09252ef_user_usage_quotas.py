"""user usage quotas

Revision ID: 392ee09252ef
Revises: 523ac0598aed
Create Date: 2026-10-14 15:06:16.866559

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '392ee09252ef'
down_revision: Union[str, Sequence[str], None] = '523ac0598aed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('token_limit', sa.Integer(), nullable=True))
    op.add_column('users', sa.Column('request_limit', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        # SQLite cannot drop a column in place; the batch copy-and-move rebuilds the table.
        with op.batch_alter_table('users', recreate='always') as batch_op:
            batch_op.drop_column('request_limit')
            batch_op.drop_column('token_limit')
    else:
        # A batch rebuild on PostgreSQL drops and recreates the primary key, which fails while
        # foreign keys (usage_records, api_keys, …) still reference `users`.
        op.drop_column('users', 'request_limit')
        op.drop_column('users', 'token_limit')
