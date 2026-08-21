import { useState } from "react";
import { Link } from "react-router-dom";
import { useModels } from "../api/hooks";
import type { ModelInfo } from "../api/types";
import DeployDialog from "../components/DeployDialog";

const CATEGORY_COLORS: Record<string, string> = {
  llm: "bg-indigo-100 text-indigo-700",
  embedding: "bg-teal-100 text-teal-700",
  reranker: "bg-purple-100 text-purple-700",
  audio: "bg-sky-100 text-sky-700",
  multimodal: "bg-pink-100 text-pink-700",
};

export default function ModelsPage() {
  const models = useModels();
  const [deploying, setDeploying] = useState<ModelInfo | null>(null);

  if (models.isLoading) return <p className="text-slate-400">Loading catalog…</p>;
  if (models.isError || !models.data) {
    return (
      <p className="text-red-600">
        Failed to load catalog: {String(models.error ?? "unknown error")}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-800">Model catalog</h1>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
        {models.data.map((model) => (
          <div
            key={model.id}
            data-testid={`model-card-${model.name}`}
            className="flex flex-col rounded-lg border border-slate-200 bg-white p-4"
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <Link
                  to={`/models/${model.id}`}
                  className="font-medium text-slate-800 hover:text-indigo-600"
                >
                  {model.display_name}
                </Link>
                <div className="mt-0.5 truncate font-mono text-xs text-slate-400">
                  {model.name}
                </div>
              </div>
              <span
                className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium capitalize ${CATEGORY_COLORS[model.category] ?? "bg-slate-100 text-slate-600"}`}
              >
                {model.category}
              </span>
            </div>
            <p className="mt-2 line-clamp-2 flex-1 text-sm text-slate-500">
              {model.description ?? ""}
            </p>
            <div className="mt-3 flex items-center justify-between text-xs text-slate-400">
              <span>{model.params_b ? `${model.params_b} B params` : "—"}</span>
              <span>{Math.round(model.vram_required_mb / 1024)} GB VRAM</span>
              <span className="rounded bg-slate-100 px-1.5 py-0.5">
                {model.license ?? "?"}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setDeploying(model)}
              className="mt-3 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700"
            >
              Deploy
            </button>
          </div>
        ))}
      </div>

      {deploying && <DeployDialog model={deploying} onClose={() => setDeploying(null)} />}
    </div>
  );
}
