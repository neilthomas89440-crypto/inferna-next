"""Pydantic v2 request/response schemas (snake_case JSON)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    token_limit: int | None = None
    request_limit: int | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"


class PasswordChange(BaseModel):
    password: str = Field(min_length=6, max_length=128)


# --- api keys ---


class ApiKeyOut(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    scopes: list[str] = []
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class ApiKeySecretOut(ApiKeyOut):
    key: str  # plaintext, returned exactly once on creation


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
    deployment_id: uuid.UUID
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
    parallel_group_id: uuid.UUID | None = None
    node_rank: int | None = None
    world_size: int | None = None
    parallel_mode: str | None = None
    rendezvous: str | None = None
    created_at: datetime
    updated_at: datetime
    model: ModelOut | None = None
    worker_name: str | None = None


class InstanceLogsOut(ORMModel):
    instance_id: uuid.UUID
    state: str
    log_tail: str | None
    updated_at: datetime
    # `live=true` waits for the worker's next Sync before answering (the worker polls; the server
    # never dials a GPU host). `stale` says the wait expired and this is the previous snapshot.
    stale: bool = False
    lines: int = 0


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


class ModelCreate(BaseModel):
    """A user-added Hugging Face repository. Engines are derived server-side from the category."""

    hf_repo_id: str = Field(min_length=3, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    category: Literal["llm", "embedding", "reranker", "audio", "multimodal"]
    vram_required_mb: int = Field(ge=256, le=1_000_000)
    requires_hf_token: bool = False
    license: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=500)


# --- instances ---


class ManualGpuSelection(BaseModel):
    worker_id: uuid.UUID
    gpu_indexes: list[int] = Field(min_length=1)


class DeployRequest(BaseModel):
    model_id: uuid.UUID
    cluster_id: uuid.UUID
    engine: Literal["vllm", "sglang"]
    profile: Literal["latency", "throughput"]
    replicas: int = Field(default=1, ge=1, le=8)
    gpu_selection: ManualGpuSelection | Literal["auto"] = "auto"
    # Multi-node: one replica is a group of `nodes_per_replica` hosts (one rank each).
    # `nodes_per_replica == 1` + "single" is the pre-existing behaviour, unchanged.
    nodes_per_replica: int = Field(default=1, ge=1, le=8)
    parallel_mode: Literal["single", "dp", "pp", "tp", "pd"] = "single"


class DeploymentRange(BaseModel):
    """Autoscaling window for one deployment: the count moves inside it, the window does not."""

    min_replicas: int = Field(ge=1, le=8)
    max_replicas: int = Field(ge=1, le=8)

    @model_validator(mode="after")
    def _check_order(self) -> DeploymentRange:
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas must be >= min_replicas")
        return self


class ScaleRequest(BaseModel):
    replicas: int = Field(ge=1, le=8)


class DeploymentOut(ORMModel):
    id: uuid.UUID
    model_id: uuid.UUID
    cluster_id: uuid.UUID
    engine: str
    profile: str
    min_replicas: int
    max_replicas: int
    nodes_per_replica: int = 1
    parallel_mode: str | None = None
    created_at: datetime
    model: ModelOut | None = None
    instances: list[InstanceOut] = []


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
DeploymentOut.model_rebuild()


# --- usage ---


class UserLimitsUpdate(BaseModel):
    token_limit: int | None = Field(default=None, ge=0)
    request_limit: int | None = Field(default=None, ge=0)


class UsageRow(BaseModel):
    period_start: date
    model_id: uuid.UUID
    model_name: str
    user_id: uuid.UUID
    username: str
    api_key_id: uuid.UUID
    requests: int
    tokens_in: int
    tokens_out: int
    avg_latency_ms: int
    # GPU-seconds are attributed to (model, cluster, day) — `Deployment` has no owner,
    # so this value is identical for every user of the same model.
    gpu_hours: float


class UsageTotals(BaseModel):
    requests: int
    tokens_in: int
    tokens_out: int
    avg_latency_ms: int
    gpu_hours: float


class UsageLimits(BaseModel):
    token_limit: int | None
    request_limit: int | None
    requests_used: int
    tokens_used: int


class UsageOut(BaseModel):
    totals: UsageTotals
    rows: list[UsageRow]
    limits: UsageLimits | None = None
