"""Pydantic v2 request/response schemas (snake_case JSON)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ---


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(ORMModel):
    id: uuid.UUID
    username: str
    email: str | None
    role: str
    is_active: bool


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"


class PasswordChange(BaseModel):
    password: str = Field(min_length=6, max_length=128)


# --- clusters / workers ---


class ClusterOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class ClusterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None


class GPUOut(ORMModel):
    id: int
    index: int
    vendor: str
    name: str
    vram_mb: int
    used_vram_mb: int
    utilization_pct: int
    uuid: str | None
    driver_version: str | None


class InstanceOut(ORMModel):
    id: uuid.UUID
    model_id: uuid.UUID
    cluster_id: uuid.UUID
    worker_id: uuid.UUID | None
    engine: str
    profile: str
    gpu_indexes: list[int]
    state: str
    desired_state: str
    generation: int
    port: int | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime
    model: ModelOut | None = None
    worker_name: str | None = None


class WorkerOut(ORMModel):
    id: uuid.UUID
    cluster_id: uuid.UUID
    name: str
    hostname: str
    state: str
    version: str | None
    os: str | None
    cpu_cores: int | None
    memory_mb: int | None
    last_seen_at: datetime | None
    gpus: list[GPUOut] = []
    instances: list[InstanceOut] = []


# --- models ---


class ModelOut(ORMModel):
    id: uuid.UUID
    name: str
    display_name: str
    category: str
    description: str | None
    params_b: float | None
    vram_required_mb: int
    requires_hf_token: bool
    license: str | None
    is_builtin: bool
    supported_engines: list[str] = []


# --- instances ---


class ManualGpuSelection(BaseModel):
    worker_id: uuid.UUID
    gpu_indexes: list[int] = Field(min_length=1)


class DeployRequest(BaseModel):
    model_id: uuid.UUID
    cluster_id: uuid.UUID
    engine: Literal["vllm", "sglang"]
    profile: Literal["latency", "throughput"]
    gpu_selection: ManualGpuSelection | Literal["auto"] = "auto"


# --- dashboard ---


class DashboardOut(BaseModel):
    clusters: int
    workers_online: int
    gpus_total: int
    vram_used_mb: int
    vram_total_mb: int
    instances_running: int
    instances: list[InstanceOut]


InstanceOut.model_rebuild()
WorkerOut.model_rebuild()
