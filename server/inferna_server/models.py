"""SQLAlchemy 2.0 ORM models (uuid PKs, tz-aware UTC datetimes)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
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


class Worker(Base):
    __tablename__ = "workers"

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelInstance(Base, TimestampMixin):
    __tablename__ = "model_instances"
    __allow_unmapped__ = True

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"), index=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clusters.id"), index=True)
    worker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workers.id"), nullable=True, index=True
    )
    engine: Mapped[str] = mapped_column(String(16))  # vllm | sglang
    profile: Mapped[str] = mapped_column(String(16))  # latency | throughput
    gpu_indexes: Mapped[list[int]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="scheduled")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Computed (non-persistent) field, filled by API serialization.
    worker_name: str | None = None

    model: Mapped[Model] = relationship()
    cluster: Mapped[Cluster] = relationship()
    worker: Mapped[Worker | None] = relationship(back_populates="instances")
