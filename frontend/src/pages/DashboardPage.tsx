import { Link } from "react-router-dom";
import { useDashboard } from "../api/hooks";
import StateBadge from "../components/StateBadge";
import VramBar from "../components/VramBar";

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-sm text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-800">{value}</div>
    </div>
  );
}

export default function DashboardPage() {
  const dashboard = useDashboard();
  if (dashboard.isLoading) return <p className="text-slate-400">Loading dashboard…</p>;
  if (dashboard.isError || !dashboard.data) {
    return (
      <p className="text-red-600">
        Failed to load dashboard: {String(dashboard.error ?? "unknown error")}
      </p>
    );
  }
  const d = dashboard.data;
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-800">Dashboard</h1>
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <StatCard label="Clusters" value={d.clusters} />
        <StatCard label="Workers online" value={d.workers_online} />
        <StatCard label="GPUs total" value={d.gpus_total} />
        <StatCard label="Instances running" value={d.instances_running} />
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <div className="text-sm text-slate-500">VRAM used</div>
          <div className="mt-2">
            <VramBar usedMb={d.vram_used_mb} totalMb={d.vram_total_mb} />
          </div>
        </div>
      </div>

      <div>
        <h2 className="mb-2 text-lg font-semibold text-slate-800">Recent instances</h2>
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-2">Model</th>
                <th className="px-4 py-2">Engine</th>
                <th className="px-4 py-2">Worker</th>
                <th className="px-4 py-2">Port</th>
                <th className="px-4 py-2">State</th>
              </tr>
            </thead>
            <tbody>
              {d.instances.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-6 text-center text-slate-400">
                    No instances yet — deploy a model from the catalog.
                  </td>
                </tr>
              )}
              {d.instances.map((inst) => (
                <tr key={inst.id} className="border-b border-slate-100 last:border-0">
                  <td className="px-4 py-2">
                    <Link
                      to={`/models/${inst.model_id}`}
                      className="text-indigo-600 hover:underline"
                    >
                      {inst.model?.display_name ?? inst.model_id}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-slate-600">{inst.engine}</td>
                  <td className="px-4 py-2 text-slate-600">{inst.worker_name ?? "—"}</td>
                  <td className="px-4 py-2 font-mono text-slate-600">
                    {inst.port ?? "—"}
                  </td>
                  <td className="px-4 py-2">
                    <StateBadge state={inst.state} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
