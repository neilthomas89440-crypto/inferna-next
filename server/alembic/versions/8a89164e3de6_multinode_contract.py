"""multinode contract

Revision ID: 8a89164e3de6
Revises: 392ee09252ef
Create Date: 2026-10-14 23:15:48.633218

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a89164e3de6'
down_revision: Union[str, Sequence[str], None] = '392ee09252ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_parallel_check() -> None:
    """`parallel_mode` check constraint (SQLite needs a table rebuild for constraints)."""
    conn = op.get_bind()
    name = "ck_deployments_parallel_mode"
    expr = "parallel_mode IS NULL OR parallel_mode IN ('single', 'dp', 'pp', 'tp', 'pd')"
    if conn.dialect.name == "sqlite":
        with op.batch_alter_table("deployments", recreate="always") as batch_op:
            batch_op.create_check_constraint(name, expr)
    else:
        op.create_check_constraint(name, "deployments", expr)


def _drop_parallel_check() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        with op.batch_alter_table("deployments", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_deployments_parallel_mode", type_="check")
    else:
        op.drop_constraint("ck_deployments_parallel_mode", "deployments", type_="check")


def upgrade() -> None:
    """Upgrade schema."""

    # model_instances: parallel-group identity. Rows with a group id form ranks; without one
    # they keep the single-node semantics byte for byte.
    op.add_column(
        "model_instances", sa.Column("parallel_group_id", sa.Uuid(), nullable=True)
    )
    op.add_column("model_instances", sa.Column("node_rank", sa.Integer(), nullable=True))
    op.add_column("model_instances", sa.Column("world_size", sa.Integer(), nullable=True))
    op.add_column("model_instances", sa.Column("parallel_mode", sa.String(16), nullable=True))
    op.add_column("model_instances", sa.Column("rendezvous", sa.String(255), nullable=True))
    op.create_index(
        "ix_model_instances_parallel_group_id",
        "model_instances",
        ["parallel_group_id"],
    )
    # One rank per group: insert races in allocate_parallel_replicas lose this way.
    op.create_index(
        "uq_model_instances_group_rank",
        "model_instances",
        ["parallel_group_id", "node_rank"],
        unique=True,
        postgresql_where=sa.text("parallel_group_id IS NOT NULL"),
        sqlite_where=sa.text("parallel_group_id IS NOT NULL"),
    )

    # deployments: desired-state for a whole group.
    op.add_column(
        "deployments",
        sa.Column(
            "nodes_per_replica", sa.Integer(), server_default="1", nullable=False
        ),
    )
    op.add_column("deployments", sa.Column("parallel_mode", sa.String(16), nullable=True))
    _create_parallel_check()


def downgrade() -> None:
    """Downgrade schema."""
    _drop_parallel_check()
    op.drop_column("deployments", "parallel_mode")
    op.drop_column("deployments", "nodes_per_replica")
    op.drop_index("uq_model_instances_group_rank", table_name="model_instances")
    op.drop_index("ix_model_instances_parallel_group_id", table_name="model_instances")
    with op.batch_alter_table("model_instances", recreate="always") as batch_op:
        batch_op.drop_column("rendezvous")
        batch_op.drop_column("parallel_mode")
        batch_op.drop_column("world_size")
        batch_op.drop_column("node_rank")
        batch_op.drop_column("parallel_group_id")
