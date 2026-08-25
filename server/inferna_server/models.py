"""SQLAlchemy 2.0 ORM models (uuid PKs, tz-aware UTC datetimes)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inferna_server.db import Base

# Instance states that consume GPU/VRAM and are considered "live".
LIVE_STATES = ("scheduled", "starting", "running")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','user')", name="ck_users_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="user")  # "admin" | "user"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    workers: Mapped[list[Worker]] = relationship(back_populates="cluster")
    deployments: Mapped[list[Deployment]] = relationship(back_populates="cluster")


class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (
        UniqueConstraint("cluster_id", "hostname", name="uq_workers_cluster_hostname"),
        UniqueConstraint("cluster_id", "name", name="uq_workers_cluster_name"),
        CheckConstraint("state IN ('connected','disconnected')", name="ck_workers_state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16), default="connected")
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex of worker token
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cluster: Mapped[Cluster] = relationship(back_populates="workers")
    gpus: Mapped[list[WorkerGPU]] = relationship(
        back_populates="worker", cascade="all, delete-orphan", order_by="WorkerGPU.index"
    )
    instances: Mapped[list[ModelInstance]] = relationship(back_populates="worker")




class WorkerGPU(Base):
    """Per-Sync snapshot of one GPU. Upserted by (worker_id, index); stale rows deleted."""

    __tablename__ = "worker_gpus"
    __table_args__ = (
        UniqueConstraint("worker_id", "index", name="uq_worker_gpus_worker_index"),
        CheckConstraint("vendor IN ('nvidia','amd','mock')", name="ck_worker_gpus_vendor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workers.id", ondelete="CASCADE"), index=True
    )
    index: Mapped[int] = mapped_column(Integer)
    vendor: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    vram_mb: Mapped[int] = mapped_column(Integer)
    used_vram_mb: Mapped[int] = mapped_column(Integer, default=0)
    utilization_pct: Mapped[int] = mapped_column(Integer, default=0)
    uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    driver_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    worker: Mapped[Worker] = relationship(back_populates="gpus")

class Model(Base):
    __tablename__ = "models"
    __table_args__ = (
        CheckConstraint(
            "category IN ('llm','embedding','reranker','audio','multimodal')",
            name="ck_models_category",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True)  # huggingface id
    display_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32))  # llm|embedding|reranker|audio|multimodal
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_b: Mapped[float | None] = mapped_column(nullable=True)
    vram_required_mb: Mapped[int] = mapped_column(Integer)
    requires_hf_token: Mapped[bool] = mapped_column(Boolean, default=False)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    supported_engines: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deployments: Mapped[list[Deployment]] = relationship(back_populates="model")


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship()


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint("engine IN ('vllm','sglang')", name="ck_deployments_engine"),
        CheckConstraint("profile IN ('latency','throughput')", name="ck_deployments_profile"),
        CheckConstraint(
            "min_replicas >= 1 AND max_replicas >= min_replicas AND max_replicas <= 8",
            name="ck_deployments_min_max",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), index=True
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(16))
    profile: Mapped[str] = mapped_column(String(16))
    min_replicas: Mapped[int] = mapped_column(default=1)
    max_replicas: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Written by apply_scale under the same commit as the replica change; NULL until the
    # first scale so it never implies a cooldown. Read by autoscaler cooldown in Release B.
    last_scaled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    model: Mapped[Model] = relationship(back_populates="deployments")
    cluster: Mapped[Cluster] = relationship(back_populates="deployments")
    instances: Mapped[list[ModelInstance]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        order_by="ModelInstance.created_at",
    )


class ModelInstance(Base, TimestampMixin):
    __tablename__ = "model_instances"
    __allow_unmapped__ = True
    __table_args__ = (
        CheckConstraint(
            "state IN ('scheduled','starting','running','stopped','error')",
            name="ck_model_instances_state",
        ),
        CheckConstraint(
            "desired_state IN ('running','stopped')",
            name="ck_model_instances_desired_state",
        ),
        CheckConstraint("engine IN ('vllm','sglang')", name="ck_model_instances_engine"),
        CheckConstraint(
            "profile IN ('latency','throughput')", name="ck_model_instances_profile"
        ),
        Index(
            "uq_model_instances_worker_port_active",
            "worker_id",
            "port",
            unique=True,
            postgresql_where=text(
                "desired_state = 'running' OR state IN ('scheduled','starting','running')"
            ),
            sqlite_where=text(
                "desired_state = 'running' OR state IN ('scheduled','starting','running')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="RESTRICT"), index=True
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"), index=True
    )
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(16))  # vllm | sglang
    profile: Mapped[str] = mapped_column(String(16))  # latency | throughput
    gpu_indexes: Mapped[list[int]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="scheduled")
    desired_state: Mapped[str] = mapped_column(
        String(16), default="running", server_default="running"
    )
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Computed (non-persistent) field, filled by API serialization.
    worker_name: str | None = None

    model: Mapped[Model] = relationship()
    cluster: Mapped[Cluster] = relationship()
    worker: Mapped[Worker | None] = relationship(back_populates="instances")
    deployment: Mapped[Deployment] = relationship(back_populates="instances")
