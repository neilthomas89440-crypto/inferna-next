import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useClusters, useCompatibility, useDeployInstance, useWorkers } from "../api/hooks";
import type { Engine, ModelInfo, Profile } from "../api/types";

interface DeployDialogProps {
  model: ModelInfo;
  onClose: () => void;
}

export default function DeployDialog({ model, onClose }: DeployDialogProps) {
  const navigate = useNavigate();
  const clusters = useClusters();
  const deploy = useDeployInstance();

  const [clusterId, setClusterId] = useState("");
  const [engine, setEngine] = useState<Engine | "">("");
  const [profile, setProfile] = useState<Profile>("latency");
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const [workerId, setWorkerId] = useState("");
  const [gpuIndexes, setGpuIndexes] = useState<number[]>([]);
  const [replicas, setReplicas] = useState(1);
  const [error, setError] = useState<string | null>(null);

  const workers = useWorkers(clusterId || undefined);
  const compat = useCompatibility();

  useEffect(() => {
    if (clusters.data && clusters.data.length > 0 && !clusterId) {
      setClusterId(clusters.data[0].id);
    }
  }, [clusters.data, clusterId]);

  useEffect(() => {
    if (workers.data && workers.data.length > 0 && !workerId) {
      setWorkerId(workers.data[0].id);
    }
  }, [workers.data, workerId]);

  useEffect(() => {
    if (model.supported_engines.length > 0 && !engine) {
      setEngine(model.supported_engines[0] as Engine);
    }
  }, [model.supported_engines, engine]);

  const selectedWorker = workers.data?.find((w) => w.id === workerId);

  const toggleGpu = (index: number) => {
    setGpuIndexes((prev) =>
      prev.includes(index) ? prev.filter((i) => i !== index) : [...prev, index],
    );
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!engine) {
      setError("No engine available for this model");
      return;
    }
    if (mode === "manual" && gpuIndexes.length === 0) {
      setError("Select at least one GPU");
      return;
    }
    if (!Number.isInteger(replicas) || replicas < 1 || replicas > 8) {
      setError("Replicas must be an integer between 1 and 8");
      return;
    }
    try {
      await deploy.mutateAsync({
        model_id: model.id,
        cluster_id: clusterId,
        engine: engine as Engine,
        profile,
        replicas,
        gpu_selection:
          mode === "manual" ? { worker_id: workerId, gpu_indexes: gpuIndexes } : "auto",
      });
      onClose();
      navigate("/instances");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Deploy failed");
    }
  };
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
      <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg bg-white p-6 shadow-xl">
        <h2 className="text-lg font-semibold text-slate-800">
          Deploy {model.display_name}
        </h2>
        <p className="mt-1 text-sm text-slate-500">{model.name}</p>

        <form onSubmit={submit} className="mt-4 space-y-4">
          <label className="block text-sm">
            <span className="font-medium text-slate-700">Cluster</span>
            <select
              value={clusterId}
              onChange={(e) => setClusterId(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              required
            >
              {clusters.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            {clusters.data?.length === 0 && (
              <span className="mt-1 block text-xs text-red-600">No clusters exist yet.</span>
            )}
          </label>

          <label className="block text-sm">
            <span className="font-medium text-slate-700">Engine</span>
            <select
              value={engine}
              onChange={(e) => setEngine(e.target.value as Engine)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            >
              {model.supported_engines.map((e) => (
                <option key={e} value={e}>
                  {e === "vllm" ? "vLLM" : "SGLang"}
                </option>
              ))}
            </select>
            {model.supported_engines.length === 0 && (
              <span className="mt-1 block text-xs text-red-600">
                No engine supports this model category in the current image set
              </span>
            )}
          </label>

          <fieldset>
            <legend className="text-sm font-medium text-slate-700">Profile</legend>
            <div className="mt-1 flex gap-4">
              {(["latency", "throughput"] as Profile[]).map((p) => (
                <label key={p} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="profile"
                    checked={profile === p}
                    onChange={() => setProfile(p)}
                    className="accent-indigo-600"
                  />
                  {p === "latency" ? "Low latency" : "High throughput"}
                </label>
              ))}
            </div>
          </fieldset>

          {mode !== "manual" && (
            <label className="block text-sm">
              <span className="font-medium text-slate-700">Replicas</span>
              <input
                type="number"
                min={1}
                max={8}
                value={replicas}
                onChange={(e) => setReplicas(Number(e.target.value))}
                className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              />
            </label>
          )}
          <fieldset>

            <legend className="text-sm font-medium text-slate-700">GPU selection</legend>
            <div className="mt-1 flex gap-4">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="gpu-mode"
                  checked={mode === "auto"}
                  onChange={() => setMode("auto")}
                  className="accent-indigo-600"
                />
                Auto (best fit)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="gpu-mode"
                  checked={mode === "manual"}
                  onChange={() => {
                    setMode("manual");
                    setReplicas(1);
                  }}
                  className="accent-indigo-600"
                />
                Manual
              </label>
            </div>

            {mode === "manual" && (
              <div className="mt-2 space-y-2">
                <select
                  value={workerId}
                  onChange={(e) => setWorkerId(e.target.value)}
                  className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                >
                  {workers.data?.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name} ({w.state})
                    </option>
                  ))}
                </select>
                {selectedWorker?.gpus.map((gpu) => {
                  const freeMb = gpu.vram_mb - gpu.used_vram_mb;
                  const fits = freeMb >= model.vram_required_mb;
                  const vendorOk =
                    !!engine && !!compat.data?.engine_vendors[engine]?.includes(gpu.vendor);
                  const disabled = !fits || !vendorOk;
                  return (
                    <label
                      key={gpu.id}
                      className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${
                        !disabled ? "border-slate-300" : "border-slate-200 opacity-60"
                      }`}
                    >
                      <input
                        type="checkbox"
                        disabled={disabled}
                        checked={gpuIndexes.includes(gpu.index)}
                        onChange={() => toggleGpu(gpu.index)}
                        className="accent-indigo-600"
                      />
                      <span className="font-mono">GPU {gpu.index}</span>
                      <span className="text-slate-500">{gpu.name}</span>
                      <span className="ml-auto text-xs text-slate-400">
                        {Math.round(freeMb / 1024)} GB free
                        {fits ? "" : " · too small"}
                        {!vendorOk ? ` · engine not supported on ${gpu.vendor}` : ""}
                      </span>
                    </label>
                  );
                })}
              </div>
            )}
          </fieldset>

          {error && (
            <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-100"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={
                deploy.isPending || model.supported_engines.length === 0 || !clusterId || !engine
              }
              className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
            >
              {deploy.isPending ? "Deploying…" : "Deploy"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
