// TS interfaces mirroring the server's pydantic schemas (snake_case).

export type Role = "admin" | "user";
export type InstanceState = "scheduled" | "starting" | "running" | "stopped" | "error";
export type Engine = "vllm" | "sglang";
export type Profile = "latency" | "throughput";

export interface User {
  id: string;
  username: string;
  email: string | null;
  role: Role;
  is_active: boolean;
}

export interface Cluster {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
}

export interface GPU {
  id: number;
  index: number;
  vendor: string;
  name: string;
  vram_mb: number;
  used_vram_mb: number;
  utilization_pct: number;
  uuid: string | null;
  driver_version: string | null;
}

export interface ModelInfo {
  id: string;
  name: string;
  display_name: string;
  category: "llm" | "embedding" | "reranker" | "audio" | "multimodal";
  description: string | null;
  params_b: number | null;
  vram_required_mb: number;
  requires_hf_token: boolean;
  license: string | null;
  is_builtin: boolean;
}

export interface Instance {
  id: string;
  model_id: string;
  cluster_id: string;
  worker_id: string | null;
  engine: Engine;
  profile: Profile;
  gpu_indexes: number[];
  state: InstanceState;
  port: number | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
  model: ModelInfo | null;
  worker_name: string | null;
}

export interface Worker {
  id: string;
  cluster_id: string;
  name: string;
  hostname: string;
  state: "connected" | "disconnected";
  version: string | null;
  os: string | null;
  cpu_cores: number | null;
  memory_mb: number | null;
  last_seen_at: string | null;
  gpus: GPU[];
  instances: Instance[];
}

export interface Dashboard {
  clusters: number;
  workers_online: number;
  gpus_total: number;
  vram_used_mb: number;
  vram_total_mb: number;
  instances_running: number;
  instances: Instance[];
}

export interface ManualGpuSelection {
  worker_id: string;
  gpu_indexes: number[];
}

export interface DeployRequest {
  model_id: string;
  cluster_id: string;
  engine: Engine;
  profile: Profile;
  gpu_selection: "auto" | ManualGpuSelection;
}
