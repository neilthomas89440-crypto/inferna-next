"""phase2 replicas

Revision ID: e8f2a6c41d93
Revises: 7a3b9c1d5e2f
Create Date: 2026-08-24 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

# revision identifiers, used by Alembic.
revision: str = 'e8f2a6c41d93'
down_revision: Union[str, Sequence[str], None] = '7a3b9c1d5e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_check(table: str, name: str) -> None:
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        try:
            with op.batch_alter_table(table, recreate='always') as batch_op:
                batch_op.drop_constraint(name, type_='check')
        except Exception:
            pass
    else:
        try:
            op.drop_constraint(name, table, type_='check')
        except Exception:
            pass


def _add_deployment_id_column() -> None:
    """model_instances.deployment_id: nullable until backfill; SQLite recreates (batch)."""
    try:
        op.add_column('model_instances', sa.Column('deployment_id', sa.Uuid(), nullable=True))
    except (NotImplementedError, OperationalError) as exc:
        with op.batch_alter_table('model_instances', recreate='always') as batch_op:
            batch_op.add_column(sa.Column('deployment_id', sa.Uuid(), nullable=True))


def _drop_deployment_id_column() -> None:
    """model_instances.deployment_id: SQLite cannot drop columns in place — batch recreate."""
    try:
        op.drop_column('model_instances', 'deployment_id')
    except (NotImplementedError, OperationalError) as exc:
        with op.batch_alter_table('model_instances', recreate='always') as batch_op:
            batch_op.drop_column('deployment_id')


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    # 1. deployments table (last_scaled_at has no default: first scale must not be blocked)
    op.create_table('deployments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('model_id', sa.Uuid(), nullable=False),
    sa.Column('cluster_id', sa.Uuid(), nullable=False),
    sa.Column('engine', sa.String(length=16), nullable=False),
    sa.Column('profile', sa.String(length=16), nullable=False),
    sa.Column('min_replicas', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('max_replicas', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_scaled_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['model_id'], ['models.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['cluster_id'], ['clusters.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint("engine IN ('vllm','sglang')", name='ck_deployments_engine'),
    sa.CheckConstraint("profile IN ('latency','throughput')", name='ck_deployments_profile'),
    sa.CheckConstraint('min_replicas >= 1 AND max_replicas >= min_replicas', name='ck_deployments_min_max')
    )
    op.create_index(op.f('ix_deployments_model_id'), 'deployments', ['model_id'], unique=False)
    op.create_index(op.f('ix_deployments_cluster_id'), 'deployments', ['cluster_id'], unique=False)

    # 2. model_instances.deployment_id — nullable until backfill completes
    _add_deployment_id_column()

    # 3. Backfill: every existing instance becomes a single-replica group (min=max=1).
    # Runs with the API stopped (CMD runs `alembic upgrade head && uvicorn ...`).
    rows = list(conn.execute(sa.text(
        "SELECT id, model_id, cluster_id, engine, profile, created_at FROM model_instances"
    )).mappings())
    for row in rows:
        dep_id = str(uuid.uuid4())
        conn.execute(
            sa.text(
                "INSERT INTO deployments "
                "(id, model_id, cluster_id, engine, profile, min_replicas, max_replicas, created_at) "
                "VALUES (:dep_id, :model_id, :cluster_id, :engine, :profile, 1, 1, :created_at)"
            ),
            {
                "dep_id": dep_id,
                "model_id": row["model_id"],
                "cluster_id": row["cluster_id"],
                "engine": row["engine"],
                "profile": row["profile"],
                "created_at": row["created_at"],
            },
        )
        conn.execute(
            sa.text("UPDATE model_instances SET deployment_id = :dep_id WHERE id = :row_id"),
            {"dep_id": dep_id, "row_id": row["id"]},
        )
    orphan_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM model_instances WHERE deployment_id IS NULL")
    ).scalar_one()
    if orphan_count:
        raise RuntimeError(
            f"deployment backfill left {orphan_count} model_instances row(s) without a group"
        )

    # 4+5. NOT NULL + FK -> deployments.id ON DELETE CASCADE
    if dialect == 'sqlite':
        with op.batch_alter_table('model_instances', recreate='always') as batch_op:
            batch_op.alter_column('deployment_id', existing_type=sa.Uuid(), nullable=False)
            batch_op.create_foreign_key(
                'fk_model_instances_deployment_id',
                'deployments',
                ['deployment_id'],
                ['id'],
                ondelete='CASCADE',
            )
    else:
        op.alter_column('model_instances', 'deployment_id', existing_type=sa.Uuid(), nullable=False)
        op.create_foreign_key(
            'fk_model_instances_deployment_id',
            'model_instances',
            'deployments',
            ['deployment_id'],
            ['id'],
            ondelete='CASCADE',
        )

    # 6.
    op.create_index(
        op.f('ix_model_instances_deployment_id'), 'model_instances', ['deployment_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_model_instances_deployment_id'), table_name='model_instances')
    try:
        op.drop_constraint(
            'fk_model_instances_deployment_id', 'model_instances', type_='foreignkey'
        )
    except Exception:
        pass
    _drop_deployment_id_column()
    _drop_check('deployments', 'ck_deployments_min_max')
    _drop_check('deployments', 'ck_deployments_profile')
    _drop_check('deployments', 'ck_deployments_engine')
    op.drop_index(op.f('ix_deployments_cluster_id'), table_name='deployments')
    op.drop_index(op.f('ix_deployments_model_id'), table_name='deployments')
    op.drop_table('deployments')
