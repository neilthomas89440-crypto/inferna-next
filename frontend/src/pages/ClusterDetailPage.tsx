import { Link, useParams } from "react-router-dom";
import { useClusters, useWorkers } from "../api/hooks";
import StateBadge from "../components/StateBadge";
import VramBar from "../components/VramBar";

function GpuChip({
  index,
  name,
  vendor,
  vramMb,
  usedMb,
  utilization,
}: {
  index: number;
  name: string;
  vendor: string;
  vramMb: number;
  usedMb: number;
  utilization: number;
}) {
  return (
    <div className="rounded-md border border-slate-200 bg-slate-50 p-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono font-medium text-slate-700">GPU {index}</span>
        <span className="capitalize text-slate-400">{vendor}</span>
        <span className="text-slate-500">{utilization}%</span>
      </div>
      <div className="mt-1 truncate text-slate-600">{name}</div>
      <div className="mt-1">
        <VramBar usedMb={usedMb} totalMb={vramMb} />
      </div>
    </div>
  );
}

export default function ClusterDetailPage() {
  const { clusterId = "" } = useParams();
  const workers = useWorkers(clusterId);
  const clusters = useClusters();
  const clusterName =
    clusters.data?.find((c) => c.id === clusterId)?.name ?? clusterId;

  if (workers.isLoading) return <p className="text-slate-400">Loading cluster…</p>;

  return (
    <div className="space-y-6">
      <div>
        <Link to="/clusters" className="text-sm text-indigo-600 hover:underline">
          ← Clusters
        </Link>
        <h1 className="mt-1 text-xl font-semibold text-slate-800">{clusterName}</h1>
      </div>

      {workers.data?.length === 0 && (
        <p className="rounded-lg border border-slate-200 bg-white p-6 text-slate-400">
          No workers in this cluster yet. Start a worker with{" "}
          <code className="text-slate-600">INFERNA_CLUSTER_NAME</code> set to this cluster.
        </p>
      )}

      {workers.data?.map((worker) => (
        <div
          key={worker.id}
          className="rounded-lg border border-slate-200 bg-white p-4"
        >
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-base font-semibold text-slate-800">{worker.name}</h2>
            <StateBadge state={worker.state} />
            <span className="text-sm text-slate-500">{worker.hostname}</span>
            <span className="ml-auto text-xs text-slate-400">
              {worker.os ?? "—"} · {worker.cpu_cores ?? "?"} cores ·{" "}
              {worker.memory_mb ? Math.round(worker.memory_mb / 1024) : "?"} GB RAM
            </span>
          </div>

          <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-3">
            {worker.gpus.map((gpu) => (
              <GpuChip
                key={gpu.id}
                index={gpu.index}
                name={gpu.name}
                vendor={gpu.vendor}
                vramMb={gpu.vram_mb}
                usedMb={gpu.used_vram_mb}
                utilization={gpu.utilization_pct}
              />
            ))}
          </div>

          {worker.instances.length > 0 && (
            <div className="mt-4">
              <h3 className="text-sm font-medium text-slate-600">Instances</h3>
              <table className="mt-1 w-full text-left text-sm">
                <tbody>
                  {worker.instances.map((inst) => (
                    <tr key={inst.id} className="border-t border-slate-100">
                      <td className="px-2 py-1.5">{inst.model?.display_name}</td>
                      <td className="px-2 py-1.5 text-slate-500">{inst.engine}</td>
                      <td className="px-2 py-1.5 font-mono text-slate-500">
                        {inst.port ?? "—"}
                      </td>
                      <td className="px-2 py-1.5">
                        <StateBadge state={inst.state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
