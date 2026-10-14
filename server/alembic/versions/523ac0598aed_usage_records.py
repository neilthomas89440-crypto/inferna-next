"""usage records

Revision ID: 523ac0598aed
Revises: a5646d3ba245
Create Date: 2026-10-14 15:06:16.475948

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '523ac0598aed'
down_revision: Union[str, Sequence[str], None] = 'a5646d3ba245'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'usage_records',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('api_key_id', sa.Uuid(), nullable=False),
        sa.Column('model_id', sa.Uuid(), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('requests', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('latency_ms_sum', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['api_key_id'], ['api_keys.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_id'], ['models.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'user_id', 'api_key_id', 'model_id', 'period_start', name='uq_usage_records_period'
        ),
    )
    op.create_index('ix_usage_records_user_id', 'usage_records', ['user_id'])
    op.create_index('ix_usage_records_api_key_id', 'usage_records', ['api_key_id'])
    op.create_index('ix_usage_records_model_id', 'usage_records', ['model_id'])
    op.create_index('ix_usage_records_period_start', 'usage_records', ['period_start'])

    op.create_table(
        'gpu_usage_records',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('model_id', sa.Uuid(), nullable=False),
        sa.Column('cluster_id', sa.Uuid(), nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('gpu_seconds', sa.Float(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['model_id'], ['models.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['cluster_id'], ['clusters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('model_id', 'cluster_id', 'period_start', name='uq_gpu_usage_period'),
    )
    op.create_index('ix_gpu_usage_records_model_id', 'gpu_usage_records', ['model_id'])
    op.create_index('ix_gpu_usage_records_cluster_id', 'gpu_usage_records', ['cluster_id'])
    op.create_index('ix_gpu_usage_records_period_start', 'gpu_usage_records', ['period_start'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_gpu_usage_records_period_start', table_name='gpu_usage_records')
    op.drop_index('ix_gpu_usage_records_cluster_id', table_name='gpu_usage_records')
    op.drop_index('ix_gpu_usage_records_model_id', table_name='gpu_usage_records')
    op.drop_table('gpu_usage_records')
    op.drop_index('ix_usage_records_period_start', table_name='usage_records')
    op.drop_index('ix_usage_records_model_id', table_name='usage_records')
    op.drop_index('ix_usage_records_api_key_id', table_name='usage_records')
    op.drop_index('ix_usage_records_user_id', table_name='usage_records')
    op.drop_table('usage_records')
