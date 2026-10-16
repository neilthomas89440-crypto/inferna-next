import { useState } from "react";
import {
  useDeleteDeployment,
  useDeleteInstance,
  useDeployments,
  useInstanceLogs,
  useRestartInstance,
  useScaleDeployment,
  useSetDeploymentRange,
  useStopInstance,
} from "../api/hooks";
import type { Instance } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import StateBadge from "../components/StateBadge";
import { copyText } from "../lib/clipboard";

export default function InstancesPage() {
  const deployments = useDeployments();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();
  const scaleDeployment = useScaleDeployment();
  const setRange = useSetDeploymentRange();
  const [rangeDraft, setRangeDraft] = useState<Record<string, { min: string; max: string }>>({});
  const deleteDeployment = useDeleteDeployment();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [deletingGroup, setDeletingGroup] = useState<string | null>(null);
  const [endpoint, setEndpoint] = useState<Instance | null>(null);
  const [logsFor, setLogsFor] = useState<Instance | null>(null);
  const [copiedTarget, setCopiedTarget] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const handleCopy = async (label: string, text: string) => {
    const result = await copyText(text);
    if (result === "copied") {
      setCopiedTarget(label);
      setCopyError(null);
    } else {
      setCopiedTarget(null);
      setCopyError(label);
    }
  };

  if (deployments.isLoading) return <p className="text-slate-400">Loading deployments…</p>;
  if (deployments.isError || !deployments.data) {
    return (
      <p className="text-red-600">
        Failed to load deployments: {String(deployments.error ?? "unknown error")}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-800">Instances</h1>
      {actionError && (
        <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{actionError}</div>
      )}
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
            <tr>
              <th className="px-4 py-2">Model</th>
              <th className="px-4 py-2">Engine</th>
              <th className="px-4 py-2">Profile</th>
              <th className="px-4 py-2">Worker</th>
              <th className="px-4 py-2">GPUs</th>
              <th className="px-4 py-2">Port</th>
              <th className="px-4 py-2">State</th>
              <th className="px-4 py-2">Access</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          {!deployments.data.length && (
            <tbody>
              <tr>
                <td colSpan={9} className="px-4 py-6 text-center text-slate-400">
                  No instances yet — deploy a model from the catalog.
                </td>
              </tr>
            </tbody>
          )}
          {deployments.data.map((dep) => {
            const ready = dep.instances.filter(
              (i) => i.desired_state === "running" && i.state === "running",
            ).length;
            return (
              <tbody key={dep.id} data-testid={`deployment-group-${dep.id}`}>
                <tr className="border-b border-slate-100 bg-slate-50">
                  <td className="px-4 py-2 font-medium text-slate-700">
                    {dep.model?.display_name ?? dep.model_id}
                  </td>
                  <td className="px-4 py-2 text-slate-600">{dep.engine}</td>
                  <td className="px-4 py-2 text-slate-600">{dep.profile}</td>
                  <td colSpan={6} className="px-4 py-2">
                    <div className="flex items-center justify-end gap-3">
                      <span
                        data-testid={`replica-badge-${dep.id}`}
                        className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700"
                      >
                        {ready}/{dep.min_replicas} (
                        {dep.min_replicas === dep.max_replicas
                          ? dep.min_replicas
                          : `${dep.min_replicas}–${dep.max_replicas}`}
                        ) running
                      </span>
                      {dep.max_replicas > dep.min_replicas ? (
                        <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700">
                          autoscaling on
                        </span>
                      ) : (
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">
                          autoscaling off
                        </span>
                      )}
                      <label className="flex items-center gap-1 text-xs text-slate-500">
                        min
                        <input
                          aria-label={`Min replicas for ${dep.model?.display_name ?? dep.model_id}`}
                          type="number"
                          min={1}
                          max={8}
                          value={rangeDraft[dep.id]?.min ?? String(dep.min_replicas)}
                          onChange={(event) =>
                            setRangeDraft((draft) => ({
                              ...draft,
                              [dep.id]: {
                                min: event.target.value,
                                max: draft[dep.id]?.max ?? String(dep.max_replicas),
                              },
                            }))
                          }
                          className="w-14 rounded-md border border-slate-300 px-1 py-0.5"
                        />
                      </label>
                      <label className="flex items-center gap-1 text-xs text-slate-500">
                        max
                        <input
                          aria-label={`Max replicas for ${dep.model?.display_name ?? dep.model_id}`}
                          type="number"
                          min={1}
                          max={8}
                          value={rangeDraft[dep.id]?.max ?? String(dep.max_replicas)}
                          onChange={(event) =>
                            setRangeDraft((draft) => ({
                              ...draft,
                              [dep.id]: {
                                min: draft[dep.id]?.min ?? String(dep.min_replicas),
                                max: event.target.value,
                              },
                            }))
                          }
                          className="w-14 rounded-md border border-slate-300 px-1 py-0.5"
                        />
                      </label>
                      <button
                        type="button"
                        aria-label={`Apply range for ${dep.model?.display_name ?? dep.model_id}`}
                        disabled={setRange.isPending}
                        onClick={() => {
                          const draft = rangeDraft[dep.id];
                          const min = Number(draft?.min ?? dep.min_replicas);
                          const max = Number(draft?.max ?? dep.max_replicas);
                          setActionError(null);
                          setRange.mutate(
                            { id: dep.id, min_replicas: min, max_replicas: max },
                            {
                              onError: (err) =>
                                setActionError(
                                  err instanceof Error ? err.message : "Could not set the range",
                                ),
                            },
                          );
                        }}
                        className="rounded-md border border-slate-300 px-2 py-0.5 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                      >
                        Apply
                      </button>
                      <button
                        type="button"
                        aria-label="Scale down"
                        disabled={dep.min_replicas <= 1 || scaleDeployment.isPending}
                        onClick={() =>
                          scaleDeployment.mutate(
                            { id: dep.id, replicas: dep.min_replicas - 1 },
                            {
                              onMutate: () => setActionError(null),
                              onError: (err) =>
                                setActionError(err instanceof Error ? err.message : "Scale failed"),
                            },
                          )
                        }
                        className="h-6 w-6 rounded-md border border-slate-300 text-sm leading-none text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                      >
                        −
                      </button>
                      <button
                        type="button"
                        aria-label="Scale up"
                        disabled={dep.min_replicas >= 8 || scaleDeployment.isPending}
                        onClick={() =>
                          scaleDeployment.mutate(
                            { id: dep.id, replicas: dep.min_replicas + 1 },
                            {
                              onMutate: () => setActionError(null),
                              onError: (err) =>
                                setActionError(err instanceof Error ? err.message : "Scale failed"),
                            },
                          )
                        }
                        className="h-6 w-6 rounded-md border border-slate-300 text-sm leading-none text-slate-600 hover:bg-slate-100 disabled:opacity-40"
                      >
                        +
                      </button>
                      <button
                        type="button"
                        onClick={() => setDeletingGroup(dep.id)}
                        className="text-sm text-red-600 hover:underline"
                      >
                        Delete group
                      </button>
                    </div>
                  </td>
                </tr>
                {dep.instances.map((inst) => (
                  <tr
                    key={inst.id}
                    data-testid={`instance-row-${inst.id}`}
                    className="border-b border-slate-100 last:border-0"
                  >
                    <td className="px-4 py-2 font-medium text-slate-700">
                      {inst.model?.display_name ?? inst.model_id}
                    </td>
                    <td className="px-4 py-2 text-slate-600">{inst.engine}</td>
                    <td className="px-4 py-2 text-slate-600">{inst.profile}</td>
                    <td className="px-4 py-2 text-slate-600">
                      {inst.worker_name ?? "—"}
                      {inst.node_rank !== null && (
                        <span
                          data-testid={`rank-badge-${inst.id}`}
                          className={`ml-2 rounded-full px-2 py-0.5 text-xs font-medium ${
                            inst.node_rank === 0
                              ? "bg-emerald-100 text-emerald-700"
                              : "bg-slate-100 text-slate-600"
                          }`}
                          title={inst.rendezvous ? `rendezvous ${inst.rendezvous}` : undefined}
                        >
                          {inst.node_rank === 0 ? "head" : `rank ${inst.node_rank}`}
                          {inst.world_size ? ` / ${inst.world_size}` : ""}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 font-mono text-slate-600">
                      {inst.gpu_indexes.join(", ") || "—"}
                    </td>
                    <td className="px-4 py-2 font-mono text-slate-600">
                      {inst.port ?? "—"}
                      {inst.parallel_group_id && inst.node_rank !== 0 && inst.rendezvous && (
                        <div className="text-xs text-slate-400" title="rendezvous point">
                          → {inst.rendezvous}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <StateBadge state={inst.state} />
                      {inst.error_detail && (
                        <span
                          className="ml-1 cursor-help text-xs text-red-500"
                          title={inst.error_detail}
                        >
                          ⚠
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {inst.state === "running" && (inst.node_rank ?? 0) === 0 ? (
                        <button
                          type="button"
                          onClick={() => {
                            setCopiedTarget(null);
                            setCopyError(null);
                            setEndpoint(inst);
                          }}
                          className="text-sm text-indigo-600 hover:underline"
                        >
                          Endpoint
                        </button>
                      ) : (
                        // A follower has no API server: only the head exposes an endpoint.
                        <span className="text-slate-400">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        type="button"
                        onClick={() => setLogsFor(inst)}
                        className="mr-3 text-sm text-slate-600 hover:underline"
                      >
                        Logs
                      </button>
                      {inst.desired_state !== "stopped" && inst.state !== "stopped" && (
                        <button
                          type="button"
                          disabled={stopInstance.isPending}
                          onClick={() => stopInstance.mutate(inst.id)}
                          className="mr-3 text-sm text-slate-600 hover:underline disabled:opacity-50"
                        >
                          Stop
                        </button>
                      )}
                      {inst.state === "error" && (
                        <button
                          type="button"
                          disabled={restartInstance.isPending}
                          onClick={() => restartInstance.mutate(inst.id)}
                          className="mr-3 text-sm text-slate-600 hover:underline disabled:opacity-50"
                        >
                          Retry
                        </button>
                      )}
                      {inst.state === "stopped" && (
                        <button
                          type="button"
                          disabled={restartInstance.isPending}
                          onClick={() => restartInstance.mutate(inst.id)}
                          className="mr-3 text-sm text-slate-600 hover:underline disabled:opacity-50"
                        >
                          Resume
                        </button>
                      )}
                      {(inst.state === "running" || inst.state === "starting") && (
                        <button
                          type="button"
                          disabled={restartInstance.isPending}
                          onClick={() => restartInstance.mutate(inst.id)}
                          className="mr-3 text-sm text-slate-600 hover:underline disabled:opacity-50"
                        >
                          Restart
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setDeleting(inst.id)}
                        className="text-sm text-red-600 hover:underline"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            );
          })}
        </table>
      </div>

      {endpoint &&
        (() => {
          const curlSnippet = `curl ${window.location.origin}/v1/chat/completions \\
  -H "Authorization: Bearer $INFERNA_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${endpoint.model?.name ?? ""}","messages":[{"role":"user","content":"Hello"}]}'`;
          const baseUrl = `${window.location.origin}/v1`;
          return (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
              <div className="w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl">
                <div className="flex items-center justify-between">
                  <h2 className="text-lg font-semibold text-slate-800">
                    {endpoint.model?.display_name ?? endpoint.model_id}
                  </h2>
                  <button
                    type="button"
                    onClick={() => {
                      setEndpoint(null);
                      setCopiedTarget(null);
                      setCopyError(null);
                    }}
                    className="text-sm text-slate-500 hover:text-slate-700"
                  >
                    Close
                  </button>
                </div>
                <div className="mt-4 space-y-4">
                  <div>
                    <p className="text-sm font-medium text-slate-700">Base URL</p>
                    <div className="mt-1 flex items-center gap-2">
                      <code className="flex-1 truncate rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-800">
                        {baseUrl}
                      </code>
                      <button
                        type="button"
                        onClick={() => handleCopy("baseUrl", baseUrl)}
                        className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
                      >
                        {copiedTarget === "baseUrl" ? "Copied" : copyError === "baseUrl" ? "Copy failed" : "Copy"}
                      </button>
                    </div>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-slate-700">Model</p>
                    <code className="mt-1 block rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-800">
                      {endpoint.model?.name ?? ""}
                    </code>
                  </div>
                  <div>
                    <p className="text-sm font-medium text-slate-700">cURL snippet</p>
                    <pre className="mt-1 overflow-x-auto rounded-md border border-slate-900 bg-slate-900 px-3 py-2 text-xs text-slate-100">
                      {curlSnippet}
                    </pre>
                    <button
                      type="button"
                      onClick={() => handleCopy("curl", curlSnippet)}
                      className="mt-2 rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
                    >
                      {copiedTarget === "curl" ? "Copied" : copyError === "curl" ? "Copy failed" : "Copy"}
                    </button>
                  </div>
                  <p className="text-xs text-slate-500">
                    Authenticate with an API key from the API Keys page. The gateway hides worker
                    hostnames and ports.
                  </p>
                </div>
              </div>
            </div>
          );
        })()}

      {logsFor && <LogsDialog instance={logsFor} onClose={() => setLogsFor(null)} />}

      {deleting && (
        <ConfirmDialog
          title="Delete instance"
          message="The engine container will be removed. This cannot be undone."
          busy={deleteInstance.isPending}
          onConfirm={() => {
            deleteInstance.mutate(deleting, { onSettled: () => setDeleting(null) });
          }}
          onCancel={() => setDeleting(null)}
        />
      )}

      {deletingGroup && (
        <ConfirmDialog
          title="Delete deployment group"
          message="All replicas in this group will be removed. This cannot be undone."
          confirmLabel="Delete"
          busy={deleteDeployment.isPending}
          onConfirm={() => {
            deleteDeployment.mutate(deletingGroup, {
              onSuccess: () => setDeletingGroup(null),
              onError: (err) =>
                setActionError(err instanceof Error ? err.message : "Delete failed"),
            });
          }}
          onCancel={() => setDeletingGroup(null)}
        />
      )}
    </div>
  );
}

function LogsDialog({ instance, onClose }: { instance: Instance; onClose: () => void }) {
  const logs = useInstanceLogs(instance.id);
  const tail = logs.data?.log_tail?.trim();

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-slate-800">
            Logs — {instance.model?.display_name ?? instance.model_id}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-slate-500 hover:text-slate-700"
          >
            Close
          </button>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <StateBadge state={logs.data?.state ?? instance.state} />
          <span className="text-xs text-slate-500">
            Last snapshot reported by the worker — refreshed every 5 seconds, not a live stream.
          </span>
        </div>
        <pre
          data-testid="instance-logs"
          className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-slate-900 bg-slate-900 px-3 py-2 text-xs text-slate-100"
        >
          {logs.isLoading ? "Loading…" : tail ? tail : "No logs yet"}
        </pre>
        {logs.isError && (
          <p className="mt-2 text-sm text-red-600">
            Failed to load logs: {String(logs.error ?? "unknown error")}
          </p>
        )}
      </div>
    </div>
  );
}
