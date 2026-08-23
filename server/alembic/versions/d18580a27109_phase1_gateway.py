"""phase1_gateway

Revision ID: d18580a27109
Revises: d88ae166a92a
Create Date: 2026-08-22 07:19:43.873066

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

log = logging.getLogger("alembic")


# revision identifiers, used by Alembic.
revision: str = 'd18580a27109'
down_revision: Union[str, Sequence[str], None] = 'd88ae166a92a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_address_column() -> None:
    """workers.address: nullable column; SQLite recreates the table (batch)."""
    try:
        op.add_column('workers', sa.Column('address', sa.String(length=255), nullable=True))
    except (NotImplementedError, OperationalError) as exc:
        log.warning("workers.address add_column unsupported in-place, batching: %s", exc)
        with op.batch_alter_table('workers') as batch_op:
            batch_op.add_column(sa.Column('address', sa.String(length=255), nullable=True))


def _drop_address_column() -> None:
    """workers.address: SQLite cannot drop columns in place — batch recreate."""
    try:
        op.drop_column('workers', 'address')
    except (NotImplementedError, OperationalError) as exc:
        log.warning("workers.address drop_column unsupported in-place, batching: %s", exc)
        with op.batch_alter_table('workers') as batch_op:
            batch_op.drop_column('address')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('api_keys',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('scopes', sa.JSON(), nullable=False, server_default='[]'),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    op.create_index(op.f('ix_api_keys_user_id'), 'api_keys', ['user_id'], unique=False)
    _add_address_column()

def downgrade() -> None:
    """Downgrade schema."""
    _drop_address_column()
    op.drop_index(op.f('ix_api_keys_user_id'), table_name='api_keys')
    op.drop_table('api_keys')
