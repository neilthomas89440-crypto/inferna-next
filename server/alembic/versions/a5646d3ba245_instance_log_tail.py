"""instance log tail

Revision ID: a5646d3ba245
Revises: b4d7e91c2a58
Create Date: 2026-10-13 14:36:53.204798

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5646d3ba245'
down_revision: Union[str, Sequence[str], None] = 'b4d7e91c2a58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('model_instances', sa.Column('log_tail', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('model_instances', recreate='always') as batch_op:
        batch_op.drop_column('log_tail')
