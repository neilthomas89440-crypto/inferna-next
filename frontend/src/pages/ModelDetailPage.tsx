import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useModel } from "../api/hooks";
import DeployDialog from "../components/DeployDialog";

export default function ModelDetailPage() {
  const { modelId = "" } = useParams();
  const model = useModel(modelId);
  const [deploying, setDeploying] = useState(false);

  if (model.isLoading) return <p className="text-slate-400">Loading model…</p>;
  if (model.isError || !model.data) {
    return (
      <p className="text-red-600">
        Failed to load model: {String(model.error ?? "unknown error")}
      </p>
    );
  }
  const m = model.data;

  return (
    <div className="space-y-6">
      <Link to="/models" className="text-sm text-indigo-600 hover:underline">
        ← Catalog
      </Link>
      <div className="rounded-lg border border-slate-200 bg-white p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-slate-800">{m.display_name}</h1>
            <div className="mt-1 font-mono text-sm text-slate-400">{m.name}</div>
          </div>
          <button
            type="button"
            onClick={() => setDeploying(true)}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            Deploy
          </button>
        </div>
        <p className="mt-4 text-slate-600">{m.description ?? "No description."}</p>
        <dl className="mt-6 grid grid-cols-2 gap-4 text-sm md:grid-cols-4">
          <div>
            <dt className="text-slate-400">Category</dt>
            <dd className="mt-0.5 capitalize text-slate-700">{m.category}</dd>
          </div>
          <div>
            <dt className="text-slate-400">Parameters</dt>
            <dd className="mt-0.5 text-slate-700">
              {m.params_b ? `${m.params_b} B` : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-slate-400">VRAM required</dt>
            <dd className="mt-0.5 text-slate-700">
              {Math.round(m.vram_required_mb / 1024)} GB
            </dd>
          </div>
          <div>
            <dt className="text-slate-400">License</dt>
            <dd className="mt-0.5 text-slate-700">{m.license ?? "—"}</dd>
          </div>
        </dl>
        {m.requires_hf_token && (
          <p className="mt-4 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-700">
            This model is gated — the worker needs{" "}
            <code className="font-mono">INFERNA_HF_TOKEN</code> set to deploy it.
          </p>
        )}
      </div>

      {deploying && <DeployDialog model={m} onClose={() => setDeploying(false)} />}
    </div>
  );
}
