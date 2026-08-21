"""phase0 production readiness

Revision ID: d88ae166a92a
Revises: 3b491ceedb36
Create Date: 2026-08-21 10:19:57.515230

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd88ae166a92a'
down_revision: Union[str, Sequence[str], None] = '3b491ceedb36'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_check(table: str, name: str, expr: str) -> None:
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        with op.batch_alter_table(table, recreate='always') as batch_op:
            batch_op.create_check_constraint(name, expr)
    else:
        op.create_check_constraint(name, table, expr)


def _add_unique(table: str, name: str, cols: list[str]) -> None:
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        with op.batch_alter_table(table, recreate='always') as batch_op:
            batch_op.create_unique_constraint(name, cols)
    else:
        op.create_unique_constraint(name, table, cols)


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


def _drop_unique(table: str, name: str) -> None:
    conn = op.get_bind()
    if conn.dialect.name == 'sqlite':
        try:
            with op.batch_alter_table(table, recreate='always') as batch_op:
                batch_op.drop_constraint(name, type_='unique')
        except Exception:
            pass
    else:
        try:
            op.drop_constraint(name, table, type_='unique')
        except Exception:
            pass


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    # 1. model_instances: desired_state, generation, backfill, checks (index deferred until after worker dedup)
    op.add_column('model_instances', sa.Column('desired_state', sa.String(length=16), nullable=False, server_default='running'))
    op.add_column('model_instances', sa.Column('generation', sa.Integer(), nullable=False, server_default='1'))
    op.execute(sa.text("UPDATE model_instances SET desired_state='stopped' WHERE state='stopped'"))
    _add_check('model_instances', 'ck_model_instances_state', "state IN ('scheduled','starting','running','stopped','error')")
    _add_check('model_instances', 'ck_model_instances_desired_state', "desired_state IN ('running','stopped')")
    _add_check('model_instances', 'ck_model_instances_engine', "engine IN ('vllm','sglang')")
    _add_check('model_instances', 'ck_model_instances_profile', "profile IN ('latency','throughput')")

    # 2. worker_gpus: unique + vendor check
    _add_unique('worker_gpus', 'uq_worker_gpus_worker_index', ['worker_id', 'index'])
    _add_check('worker_gpus', 'ck_worker_gpus_vendor', "vendor IN ('nvidia','amd','mock')")

    # 3. workers: state check, dedup (collision-safe), orphan check, unique constraints
    _add_check('workers', 'ck_workers_state', "state IN ('connected','disconnected')")

    # Deduplicate (cluster_id, hostname) — collision-safe for active worker-port index
    dup_host_rows = list(conn.execute(sa.text("SELECT cluster_id, hostname FROM workers GROUP BY cluster_id, hostname HAVING COUNT(*) > 1")).mappings())
    for grp in dup_host_rows:
        cid = grp['cluster_id']
        hostname = grp['hostname']
        rows = list(conn.execute(
            sa.text("SELECT id, last_seen_at, created_at FROM workers WHERE cluster_id = :cid AND hostname = :hn"),
            {"cid": cid, "hn": hostname},
        ).mappings())
        def sort_key(r):
            ls = r['last_seen_at']
            ca = r['created_at']
            ls_s = str(ls) if ls is not None else ""
            ca_s = str(ca) if ca is not None else ""
            primary = ls_s if ls_s else ca_s
            return (primary, ca_s, str(r['id']))
        rows.sort(key=sort_key, reverse=True)
        kept = rows[0]['id']
        for dup in rows[1:]:
            dup_id = dup['id']
            # Collision-safe reassignment: if kept worker already has an active instance on the same port,
            # the unconditional UPDATE would violate the upcoming partial unique index.
            # Null out the colliding dup instance's port (and mark error) so it no longer counts as active.
            # Active = desired_state='running' OR state IN ('scheduled','starting','running')
            dup_active = list(conn.execute(
                sa.text("SELECT id, port FROM model_instances WHERE worker_id = :dup AND (desired_state = 'running' OR state IN ('scheduled','starting','running')) AND port IS NOT NULL"),
                {"dup": dup_id},
            ).mappings())
            for inst in dup_active:
                colliding = conn.execute(
                    sa.text("SELECT 1 FROM model_instances WHERE worker_id = :kept AND port = :port AND (desired_state = 'running' OR state IN ('scheduled','starting','running'))"),
                    {"kept": kept, "port": inst['port']},
                ).fetchone()
                if colliding:
                    conn.execute(
                        sa.text("UPDATE model_instances SET port = NULL, state = 'error', error_detail = 'worker dedup port collision', desired_state = 'stopped' WHERE id = :iid"),
                        {"iid": inst['id']},
                    )
            conn.execute(sa.text("UPDATE model_instances SET worker_id = :kept WHERE worker_id = :dup"), {"kept": kept, "dup": dup_id})
            conn.execute(sa.text("DELETE FROM workers WHERE id = :dup"), {"dup": dup_id})

    # Deduplicate (cluster_id, name)
    dup_name_rows = list(conn.execute(sa.text("SELECT cluster_id, name FROM workers GROUP BY cluster_id, name HAVING COUNT(*) > 1")).mappings())
    for grp in dup_name_rows:
        cid = grp['cluster_id']
        name = grp['name']
        rows = list(conn.execute(
            sa.text("SELECT id, last_seen_at, created_at FROM workers WHERE cluster_id = :cid AND name = :nm"),
            {"cid": cid, "nm": name},
        ).mappings())
        def sort_key2(r):
            ls = r['last_seen_at']
            ca = r['created_at']
            ls_s = str(ls) if ls is not None else ""
            ca_s = str(ca) if ca is not None else ""
            primary = ls_s if ls_s else ca_s
            return (primary, ca_s, str(r['id']))
        rows.sort(key=sort_key2, reverse=True)
        kept = rows[0]['id']
        for dup in rows[1:]:
            dup_id = dup['id']
            dup_active = list(conn.execute(
                sa.text("SELECT id, port FROM model_instances WHERE worker_id = :dup AND (desired_state = 'running' OR state IN ('scheduled','starting','running')) AND port IS NOT NULL"),
                {"dup": dup_id},
            ).mappings())
            for inst in dup_active:
                colliding = conn.execute(
                    sa.text("SELECT 1 FROM model_instances WHERE worker_id = :kept AND port = :port AND (desired_state = 'running' OR state IN ('scheduled','starting','running'))"),
                    {"kept": kept, "port": inst['port']},
                ).fetchone()
                if colliding:
                    conn.execute(
                        sa.text("UPDATE model_instances SET port = NULL, state = 'error', error_detail = 'worker dedup port collision', desired_state = 'stopped' WHERE id = :iid"),
                        {"iid": inst['id']},
                    )
            conn.execute(sa.text("UPDATE model_instances SET worker_id = :kept WHERE worker_id = :dup"), {"kept": kept, "dup": dup_id})
            conn.execute(sa.text("DELETE FROM workers WHERE id = :dup"), {"dup": dup_id})

    # Protective orphan check
    orphan_count = conn.execute(sa.text("SELECT count(*) FROM model_instances WHERE worker_id IS NOT NULL AND worker_id NOT IN (SELECT id FROM workers)")).scalar()
    if orphan_count and int(orphan_count) != 0:
        raise RuntimeError(f"orphaned model_instances.worker_id references after dedup: {orphan_count}")

    # Now create the active port unique index (deferred to avoid collision during reassignment)
    where_expr = sa.text("desired_state = 'running' OR state IN ('scheduled','starting','running')")
    op.create_index(
        'uq_model_instances_worker_port_active',
        'model_instances',
        ['worker_id', 'port'],
        unique=True,
        postgresql_where=where_expr,
        sqlite_where=where_expr,
    )

    _add_unique('workers', 'uq_workers_cluster_hostname', ['cluster_id', 'hostname'])
    _add_unique('workers', 'uq_workers_cluster_name', ['cluster_id', 'name'])


    # 4. models: supported_engines + category check
    op.add_column('models', sa.Column('supported_engines', sa.JSON(), nullable=False, server_default='[]'))
    _add_check('models', 'ck_models_category', "category IN ('llm','embedding','reranker','audio','multimodal')")

    # 5. users: role check
    _add_check('users', 'ck_users_role', "role IN ('admin','user')")

    # 6. FK ondelete for model_instances
    if dialect == 'sqlite':
        with op.batch_alter_table('model_instances', recreate='always') as batch_op:
            batch_op.create_foreign_key('fk_model_instances_model_id', 'models', ['model_id'], ['id'], ondelete='RESTRICT')
            batch_op.create_foreign_key('fk_model_instances_cluster_id', 'clusters', ['cluster_id'], ['id'], ondelete='CASCADE')
            batch_op.create_foreign_key('fk_model_instances_worker_id', 'workers', ['worker_id'], ['id'], ondelete='SET NULL')
    else:
        inspector = sa.inspect(conn)
        fks = inspector.get_foreign_keys('model_instances')
        name_by_cols = {tuple(fk['constrained_columns']): fk['name'] for fk in fks if fk['name']}
        for cols in [('model_id',), ('cluster_id',), ('worker_id',)]:
            fk_name = name_by_cols.get(cols)
            if fk_name:
                op.drop_constraint(fk_name, 'model_instances', type_='foreignkey')
            else:
                try:
                    op.drop_constraint(f"model_instances_{cols[0]}_fkey", 'model_instances', type_='foreignkey')
                except Exception:
                    pass
        op.create_foreign_key('fk_model_instances_model_id', 'model_instances', 'models', ['model_id'], ['id'], ondelete='RESTRICT')
        op.create_foreign_key('fk_model_instances_cluster_id', 'model_instances', 'clusters', ['cluster_id'], ['id'], ondelete='CASCADE')
        op.create_foreign_key('fk_model_instances_worker_id', 'model_instances', 'workers', ['worker_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    # FK revert first
    if dialect == 'sqlite':
        with op.batch_alter_table('model_instances', recreate='always') as batch_op:
            try:
                batch_op.drop_constraint('fk_model_instances_model_id', type_='foreignkey')
            except Exception:
                pass
            try:
                batch_op.drop_constraint('fk_model_instances_cluster_id', type_='foreignkey')
            except Exception:
                pass
            try:
                batch_op.drop_constraint('fk_model_instances_worker_id', type_='foreignkey')
            except Exception:
                pass
            batch_op.create_foreign_key('fk_model_instances_model_id', 'models', ['model_id'], ['id'])
            batch_op.create_foreign_key('fk_model_instances_cluster_id', 'clusters', ['cluster_id'], ['id'])
            batch_op.create_foreign_key('fk_model_instances_worker_id', 'workers', ['worker_id'], ['id'])
    else:
        for fk_name in ['fk_model_instances_model_id', 'fk_model_instances_cluster_id', 'fk_model_instances_worker_id']:
            try:
                op.drop_constraint(fk_name, 'model_instances', type_='foreignkey')
            except Exception:
                pass
        op.create_foreign_key(None, 'model_instances', 'models', ['model_id'], ['id'])
        op.create_foreign_key(None, 'model_instances', 'clusters', ['cluster_id'], ['id'])
        op.create_foreign_key(None, 'model_instances', 'workers', ['worker_id'], ['id'])

    _drop_check('users', 'ck_users_role')
    _drop_check('models', 'ck_models_category')
    # models column
    try:
        op.drop_column('models', 'supported_engines')
    except Exception:
        with op.batch_alter_table('models') as batch_op:
            batch_op.drop_column('supported_engines')

    _drop_unique('workers', 'uq_workers_cluster_name')
    _drop_unique('workers', 'uq_workers_cluster_hostname')
    _drop_check('workers', 'ck_workers_state')

    _drop_check('worker_gpus', 'ck_worker_gpus_vendor')
    _drop_unique('worker_gpus', 'uq_worker_gpus_worker_index')

    _drop_check('model_instances', 'ck_model_instances_profile')
    _drop_check('model_instances', 'ck_model_instances_engine')
    _drop_check('model_instances', 'ck_model_instances_desired_state')
    _drop_check('model_instances', 'ck_model_instances_state')

    try:
        op.drop_index('uq_model_instances_worker_port_active', table_name='model_instances')
    except Exception:
        pass
    # columns (need batch for sqlite if simple drop fails)
    try:
        op.drop_column('model_instances', 'generation')
    except Exception:
        with op.batch_alter_table('model_instances') as batch_op:
            batch_op.drop_column('generation')
    try:
        op.drop_column('model_instances', 'desired_state')
    except Exception:
        with op.batch_alter_table('model_instances') as batch_op:
            batch_op.drop_column('desired_state')
