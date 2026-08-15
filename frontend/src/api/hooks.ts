import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  Cluster,
  Dashboard,
  DeployRequest,
  Instance,
  ModelInfo,
  User,
  Worker,
} from "./types";

const LIVE_INTERVAL = 5000;

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => api<User>("/auth/me"),
    refetchInterval: LIVE_INTERVAL,
  });
}

export function useUsers() {
  return useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
}

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { username: string; password: string; role: string }) =>
      api<User>("/users", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useDeleteUser() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<void>(`/users/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}

export function useChangePassword() {
  return useMutation({
    mutationFn: ({ id, password }: { id: string; password: string }) =>
      api<User>(`/users/${id}/password`, {
        method: "PUT",
        body: JSON.stringify({ password }),
      }),
  });
}

export function useClusters() {
  return useQuery({ queryKey: ["clusters"], queryFn: () => api<Cluster[]>("/clusters") });
}

export function useCreateCluster() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description?: string }) =>
      api<Cluster>("/clusters", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
  });
}

export function useDeleteCluster() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<void>(`/clusters/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
  });
}

export function useWorkers(clusterId?: string) {
  return useQuery({
    queryKey: ["workers", clusterId ?? "all"],
    queryFn: () =>
      api<Worker[]>(clusterId ? `/workers?cluster_id=${clusterId}` : "/workers"),
    refetchInterval: LIVE_INTERVAL,
  });
}

export function useModels() {
  return useQuery({ queryKey: ["models"], queryFn: () => api<ModelInfo[]>("/models") });
}

export function useModel(id: string) {
  return useQuery({
    queryKey: ["models", id],
    queryFn: () => api<ModelInfo>(`/models/${id}`),
    enabled: Boolean(id),
  });
}

export function useInstances() {
  return useQuery({
    queryKey: ["instances"],
    queryFn: () => api<Instance[]>("/model-instances"),
    refetchInterval: LIVE_INTERVAL,
  });
}

export function useDeployInstance() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DeployRequest) =>
      api<Instance>("/model-instances", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      queryClient.invalidateQueries({ queryKey: ["workers"] });
    },
  });
}

export function useStopInstance() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<Instance>(`/model-instances/${id}/stop`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useDeleteInstance() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<void>(`/model-instances/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["instances"] });
      queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useDashboard() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
    refetchInterval: LIVE_INTERVAL,
  });
}
