import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useClusters, useCreateCluster, useDeleteCluster, useWorkers } from "../api/hooks";
import { useAuth } from "../context/AuthContext";
import ConfirmDialog from "../components/ConfirmDialog";

export default function ClustersPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const clusters = useClusters();
  const createCluster = useCreateCluster();
  const deleteCluster = useDeleteCluster();
  const workers = useWorkers();

  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const workerCounts: Record<string, number> = {};
  for (const w of workers.data ?? []) {
    workerCounts[w.cluster_id] = (workerCounts[w.cluster_id] ?? 0) + 1;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await createCluster.mutateAsync({ name, description: description || undefined });
      setName("");
      setDescription("");
      setCreating(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed");
    }
  };

  if (clusters.isLoading) return <p className="text-slate-400">Loading clusters…</p>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-800">Clusters</h1>
        {isAdmin && (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            New cluster
          </button>
        )}
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
            <tr>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2">Description</th>
              <th className="px-4 py-2">Workers</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {clusters.data?.length === 0 && (
              <tr>
                <td colSpan={4} className="px-4 py-6 text-center text-slate-400">
                  No clusters yet.
                </td>
              </tr>
            )}
            {clusters.data?.map((cluster) => (
              <tr key={cluster.id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-2 font-medium">
                  <Link
                    to={`/clusters/${cluster.id}`}
                    className="text-indigo-600 hover:underline"
                  >
                    {cluster.name}
                  </Link>
                </td>
                <td className="px-4 py-2 text-slate-600">{cluster.description ?? "—"}</td>
                <td className="px-4 py-2 text-slate-600">{workerCounts[cluster.id] ?? 0}</td>
                <td className="px-4 py-2 text-right">
                  {isAdmin && (
                    <button
                      type="button"
                      onClick={() => setDeleting(cluster.id)}
                      className="text-sm text-red-600 hover:underline"
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {creating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
            <h2 className="text-lg font-semibold text-slate-800">New cluster</h2>
            <form onSubmit={submit} className="mt-4 space-y-4">
              <label className="block text-sm">
                <span className="font-medium text-slate-700">Name</span>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                />
              </label>
              <label className="block text-sm">
                <span className="font-medium text-slate-700">Description</span>
                <input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                />
              </label>
              {error && (
                <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
                  {error}
                </div>
              )}
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setCreating(false)}
                  className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-100"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createCluster.isPending}
                  className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  Create
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete cluster"
          message="This deletes the cluster. It cannot be deleted while it has workers or live instances."
          busy={deleteCluster.isPending}
          onConfirm={() => {
            deleteCluster.mutate(deleting, {
              onSuccess: () => setDeleting(null),
              onError: (err) => {
                setDeleting(null);
                setError(err instanceof Error ? err.message : "Delete failed");
              },
            });
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
