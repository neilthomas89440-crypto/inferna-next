import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useCreateModel, useDeleteModel, useModels } from "../api/hooks";
import type { ModelInfo } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import DeployDialog from "../components/DeployDialog";
import { useAuth } from "../context/AuthContext";

const CATEGORY_COLORS: Record<string, string> = {
  llm: "bg-indigo-100 text-indigo-700",
  embedding: "bg-teal-100 text-teal-700",
  reranker: "bg-purple-100 text-purple-700",
  audio: "bg-sky-100 text-sky-700",
  multimodal: "bg-pink-100 text-pink-700",
};

const CATEGORIES = ["llm", "embedding", "reranker", "audio", "multimodal"] as const;

interface ModelForm {
  hf_repo_id: string;
  display_name: string;
  category: (typeof CATEGORIES)[number];
  vram_required_mb: number;
  requires_hf_token: boolean;
  license: string;
  description: string;
}

const EMPTY_FORM: ModelForm = {
  hf_repo_id: "",
  display_name: "",
  category: "llm",
  vram_required_mb: 8192,
  requires_hf_token: false,
  license: "",
  description: "",
};

export default function ModelsPage() {
  const { user } = useAuth();
  const models = useModels();
  const createModel = useCreateModel();
  const deleteModel = useDeleteModel();
  const isAdmin = user?.role === "admin";

  const [deploying, setDeploying] = useState<ModelInfo | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<ModelForm>(EMPTY_FORM);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<ModelInfo | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  if (models.isLoading) return <p className="text-slate-400">Loading catalog…</p>;
  if (models.isError || !models.data) {
    return (
      <p className="text-red-600">
        Failed to load catalog: {String(models.error ?? "unknown error")}
      </p>
    );
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await createModel.mutateAsync({
        hf_repo_id: form.hf_repo_id.trim(),
        display_name: form.display_name.trim() || null,
        category: form.category,
        vram_required_mb: Number(form.vram_required_mb),
        requires_hf_token: form.requires_hf_token,
        license: form.license.trim() || null,
        description: form.description.trim() || null,
      });
      setForm(EMPTY_FORM);
      setCreating(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the model");
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-800">Model catalog</h1>
        {isAdmin && (
          <button
            type="button"
            onClick={() => {
              setError(null);
              setCreating(true);
            }}
            className="rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            Add model
          </button>
        )}
      </div>
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
              <div className="flex shrink-0 flex-col items-end gap-1">
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium capitalize ${CATEGORY_COLORS[model.category] ?? "bg-slate-100 text-slate-600"}`}
                >
                  {model.category}
                </span>
                {!model.is_builtin && (
                  <span
                    data-testid={`custom-badge-${model.name}`}
                    className="rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700"
                  >
                    custom
                  </span>
                )}
              </div>
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
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={() => setDeploying(model)}
                className="flex-1 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700"
              >
                Deploy
              </button>
              {isAdmin && !model.is_builtin && (
                <button
                  type="button"
                  onClick={() => {
                    setDeleteError(null);
                    setDeleting(model);
                  }}
                  className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
                >
                  Delete
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      {creating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <form
            onSubmit={submit}
            className="w-full max-w-lg space-y-4 rounded-lg bg-white p-6 shadow-xl"
          >
            <h2 className="text-lg font-semibold text-slate-800">Add a model</h2>
            <p className="text-sm text-slate-500">
              Any Hugging Face repository that the selected engine supports. Engines are derived
              from the category; VRAM is your responsibility (no Hub lookup).
            </p>
            {error && (
              <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
            )}
            <div className="space-y-3">
              <div>
                <label htmlFor="hf_repo_id" className="text-sm text-slate-600">
                  Hugging Face repository
                </label>
                <input
                  id="hf_repo_id"
                  required
                  minLength={3}
                  value={form.hf_repo_id}
                  onChange={(event) => setForm({ ...form, hf_repo_id: event.target.value })}
                  placeholder="Qwen/Qwen2.5-3B-Instruct"
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label htmlFor="display_name" className="text-sm text-slate-600">
                  Display name (optional)
                </label>
                <input
                  id="display_name"
                  value={form.display_name}
                  onChange={(event) => setForm({ ...form, display_name: event.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="category" className="text-sm text-slate-600">
                    Category
                  </label>
                  <select
                    id="category"
                    value={form.category}
                    onChange={(event) =>
                      setForm({ ...form, category: event.target.value as ModelForm["category"] })
                    }
                    className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                  >
                    {CATEGORIES.map((category) => (
                      <option key={category} value={category}>
                        {category}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label htmlFor="vram_required_mb" className="text-sm text-slate-600">
                    VRAM required (MB)
                  </label>
                  <input
                    id="vram_required_mb"
                    type="number"
                    min={256}
                    max={1000000}
                    required
                    value={form.vram_required_mb}
                    onChange={(event) =>
                      setForm({ ...form, vram_required_mb: Number(event.target.value) })
                    }
                    className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                  />
                </div>
              </div>
              <div>
                <label htmlFor="license" className="text-sm text-slate-600">
                  License (optional)
                </label>
                <input
                  id="license"
                  value={form.license}
                  onChange={(event) => setForm({ ...form, license: event.target.value })}
                  placeholder="apache-2.0"
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label htmlFor="description" className="text-sm text-slate-600">
                  Description (optional)
                </label>
                <textarea
                  id="description"
                  rows={2}
                  value={form.description}
                  onChange={(event) => setForm({ ...form, description: event.target.value })}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
                />
              </div>
              <label className="flex items-center gap-2 text-sm text-slate-600">
                <input
                  type="checkbox"
                  checked={form.requires_hf_token}
                  onChange={(event) =>
                    setForm({ ...form, requires_hf_token: event.target.checked })
                  }
                />
                Requires a Hugging Face token (gated repository)
              </label>
            </div>
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
                disabled={createModel.isPending}
                className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
              >
                {createModel.isPending ? "Adding…" : "Add to catalog"}
              </button>
            </div>
          </form>
        </div>
      )}

      {deploying && <DeployDialog model={deploying} onClose={() => setDeploying(null)} />}

      {deleting && (
        <ConfirmDialog
          title="Delete model"
          message={`Remove ${deleting.name} from the catalog? Deployments must be deleted first.`}
          busy={deleteModel.isPending}
          error={deleteError}
          onConfirm={() => {
            deleteModel.mutate(deleting.id, {
              onError: (err) => setDeleteError(err instanceof Error ? err.message : "Delete failed"),
              onSettled: () => setDeleting(null),
            });
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}