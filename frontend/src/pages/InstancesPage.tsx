import { useState } from "react";
import {
  useDeleteInstance,
  useInstances,
  useRestartInstance,
  useStopInstance,
} from "../api/hooks";
import ConfirmDialog from "../components/ConfirmDialog";
import StateBadge from "../components/StateBadge";

export default function InstancesPage() {
  const instances = useInstances();
  const stopInstance = useStopInstance();
  const restartInstance = useRestartInstance();
  const deleteInstance = useDeleteInstance();
  const [deleting, setDeleting] = useState<string | null>(null);

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
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {instances.data.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-6 text-center text-slate-400">
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
