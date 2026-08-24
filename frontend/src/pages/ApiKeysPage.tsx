import { useEffect, useState, type FormEvent } from "react";
import { useApiKeys, useCreateApiKey, useRevokeApiKey } from "../api/hooks";
import type { ApiKeyWithSecret } from "../api/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { copyText } from "../lib/clipboard";

export default function ApiKeysPage() {
  const keys = useApiKeys();
  const createKey = useCreateApiKey();
  const revokeKey = useRevokeApiKey();

  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  // Page-level `error` renders beneath the modal overlays, so dialog failures
  // need their own state to be visible inside the open dialog.
  const [modalError, setModalError] = useState<string | null>(null);
  const [created, setCreated] = useState<ApiKeyWithSecret | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => () => setCreated(null), []);


  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setModalError(null);
    const trimmed = name.trim();
    if (!trimmed) {
      setModalError("Name required");
      return;
    }
    try {
      const result = await createKey.mutateAsync({ name: trimmed });
      setName("");
      setCreating(false);
      setCreated(result);
      setCopyState("idle");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Create failed";
      setError(message);
      setModalError(message);
    }
  };

  if (keys.isLoading) {
    return <p className="text-slate-400">Loading API keys…</p>;
  }
  if (keys.isError || !keys.data) {
    return (
      <p className="text-red-600">
        Failed to load API keys: {String(keys.error ?? "unknown error")}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-800">API Keys</h1>
        <button
          type="button"
          onClick={() => {
            setModalError(null);
            setCreating(true);
          }}
          className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
        >
          New key
        </button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
      )}

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-xs uppercase text-slate-500">
            <tr>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2">Created</th>
              <th className="px-4 py-2">Last used</th>
              <th className="px-4 py-2">Revoked</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {keys.data?.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400">
                  No API keys yet — create one to call the inference gateway.
                </td>
              </tr>
            )}
            {keys.data?.map((k) => (
              <tr key={k.id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-2 font-medium text-slate-700">{k.name}</td>
                <td className="px-4 py-2 text-slate-600">
                  {new Date(k.created_at).toLocaleString()}
                </td>
                <td className="px-4 py-2 text-slate-600">
                  {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}
                </td>
                <td className="px-4 py-2">
                  {k.revoked_at ? (
                    <span className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
                      revoked
                    </span>
                  ) : (
                    <span className="text-slate-400">—</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right">
                  {!k.revoked_at && (
                    <button
                      type="button"
                      onClick={() => {
                        setModalError(null);
                        setRevoking(k.id);
                      }}
                      className="text-sm text-red-600 hover:underline"
                    >
                      Revoke
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
            <h2 className="text-lg font-semibold text-slate-800">New API key</h2>
            <form onSubmit={submit} className="mt-4 space-y-4">
              <label className="block text-sm">
                <span className="font-medium text-slate-700">Name</span>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  placeholder="e.g. ci, notebook, prod"
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
                />
              </label>
              <div className="flex justify-end gap-3">
                <button
                  type="button"
                  onClick={() => {
                    setCreating(false);
                    setModalError(null);
                  }}
                  className="rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-600 hover:bg-slate-100"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createKey.isPending}
                  className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
                >
                  {createKey.isPending ? "Creating…" : "Create"}
                </button>
              </div>
            </form>
            {modalError && (
              <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
                {modalError}
              </div>
            )}
          </div>
        </div>
      )}
      {created && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4">
          <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
            <h2 className="text-lg font-semibold text-slate-800">Key created</h2>
            <p className="mt-2 text-sm text-amber-700">
              Store this key now — it will not be shown again.
            </p>
            <div className="mt-4 flex items-center gap-2">
              <code className="flex-1 truncate rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-sm text-slate-800">
                {created.key}
              </code>
              <button
                type="button"
                onClick={async () => {
                  const result = await copyText(created.key);
                  setCopyState(result);
                }}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-600 hover:bg-slate-100"
              >
                {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy"}
              </button>
            </div>
            {copyState === "failed" && (
              <p className="mt-2 text-sm text-red-600">
                Copy failed — select and copy the key manually.
              </p>
            )}
            <div className="mt-4 flex justify-end">
              <button
                type="button"
                onClick={() => {
                  setCreated(null);
                  setCopyState("idle");
                  createKey.reset();
                }}
                className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {revoking && (
        <ConfirmDialog
          title="Revoke API key"
          message="Requests using this key will be rejected immediately. This cannot be undone."
          confirmLabel="Revoke"
          busy={revokeKey.isPending}
          onConfirm={() => {
            setModalError(null);
            revokeKey.mutate(revoking, {
              onSuccess: () => {
                setRevoking(null);
                setModalError(null);
              },
              onError: (err) =>
                setModalError(err instanceof Error ? err.message : "Revoke failed"),
            });
          }}
          error={modalError}
          onCancel={() => {
            setRevoking(null);
            setModalError(null);
          }}
        />
      )}
    </div>
  );
}
