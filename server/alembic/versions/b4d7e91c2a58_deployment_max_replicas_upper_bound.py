"""deployment max_replicas upper bound

Revision ID: b4d7e91c2a58
Revises: e8f2a6c41d93
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4d7e91c2a58'
down_revision: Union[str, Sequence[str], None] = 'e8f2a6c41d93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _replace_check(table: str, name: str, expr: str) -> None:
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        with op.batch_alter_table(table, recreate='always') as batch_op:
            batch_op.drop_constraint(name, type_='check')
            batch_op.create_check_constraint(name, expr)
    else:
        op.drop_constraint(name, table, type_='check')
        op.create_check_constraint(name, table, expr)


def upgrade() -> None:
    _replace_check(
        'deployments',
        'ck_deployments_min_max',
        'min_replicas >= 1 AND max_replicas >= min_replicas AND max_replicas <= 8',
    )


def downgrade() -> None:
    _replace_check(
        'deployments',
        'ck_deployments_min_max',
        'min_replicas >= 1 AND max_replicas >= min_replicas',
    )
