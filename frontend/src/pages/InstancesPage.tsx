import { useState } from "react";
import {
  useDeleteInstance,
  useInstances,
  useRestartInstance,
  useStopInstance,
} from "../api/hooks";
import type { Instance } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import StateBadge from "../components/StateBadge";

function copyText(text: string) {
  void navigator.clipboard.writeText(text);
}

export default function InstancesPage() {
  const instances = useInstances();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [endpoint, setEndpoint] = useState<Instance | null>(null);

  if (instances.isLoading) return <p className="text-slate-400">Loading instances…</p>;
  if (instances.isError || !instances.data) {
    return (
      <p className="text-red-600">
        Failed to load instances: {String(instances.error ?? "unknown error")}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-800">Instances</h1>
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
          <tbody>
            {instances.data.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-6 text-center text-slate-400">
                  No instances yet — deploy a model from the catalog.
                </td>
              </tr>
            )}
            {instances.data.map((inst) => (
              <tr key={inst.id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-2 font-medium text-slate-700">
                  {inst.model?.display_name ?? inst.model_id}
                </td>
                <td className="px-4 py-2 text-slate-600">{inst.engine}</td>
                <td className="px-4 py-2 text-slate-600">{inst.profile}</td>
                <td className="px-4 py-2 text-slate-600">{inst.worker_name ?? "—"}</td>
                <td className="px-4 py-2 font-mono text-slate-600">
                  {inst.gpu_indexes.join(", ") || "—"}
                </td>
                <td className="px-4 py-2 font-mono text-slate-600">{inst.port ?? "—"}</td>
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
                  {inst.state === "running" ? (
                    <button
                      type="button"
                      onClick={() => setEndpoint(inst)}
                      className="text-sm text-indigo-600 hover:underline"
                    >
                      Endpoint
                    </button>
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right">
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
        </table>
      </div>

      {endpoint && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-800">
                {endpoint.model?.display_name ?? endpoint.model_id}
              </h2>
              <button
                type="button"
                onClick={() => setEndpoint(null)}
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
                    {window.location.origin}/v1
                  </code>
                  <button
                    type="button"
                    onClick={() => copyText(`${window.location.origin}/v1`)}
                    className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
                  >
                    Copy
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
                <pre className="mt-1 overflow-x-auto rounded-md border border-slate-300 bg-slate-900 px-3 py-2 text-xs text-slate-100">
{`curl ${window.location.origin}/v1/chat/completions \\
  -H "Authorization: Bearer $INFERNA_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"${endpoint.model?.name ?? ""}","messages":[{"role":"user","content":"Hello"}]}'`}
                </pre>
                <button
                  type="button"
                  onClick={() =>
                    copyText(
                      `curl ${window.location.origin}/v1/chat/completions -H "Authorization: Bearer $INFERNA_KEY" -H "Content-Type: application/json" -d '{"model":"${endpoint.model?.name ?? ""}","messages":[{"role":"user","content":"Hello"}]}'`,
                    )
                  }
                  className="mt-2 rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
                >
                  Copy
                </button>
              </div>
              <p className="text-xs text-slate-500">
                Authenticate with an API key from the API Keys page. The gateway hides worker
                hostnames and ports.
              </p>
            </div>
          </div>
        </div>
      )}

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
    </div>
  );
}
