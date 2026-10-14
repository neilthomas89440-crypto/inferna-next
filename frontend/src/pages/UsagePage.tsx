import { useState } from "react";
import { useUsage } from "../api/hooks";
import { useAuth } from "../context/AuthContext";

const RANGES = [1, 7, 30] as const;

function Bar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-20 overflow-hidden rounded-full bg-slate-200">
        <div className="h-full bg-indigo-500" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-slate-500">{pct}%</span>
    </div>
  );
}

export default function UsagePage() {
  const { user } = useAuth();
  const [days, setDays] = useState<number>(7);
  const usage = useUsage({ days });
  const isAdmin = user?.role === "admin";

  if (usage.isLoading) return <p className="text-slate-400">Loading usage…</p>;
  if (usage.isError || !usage.data) {
    return (
      <p className="text-red-600">
        Failed to load usage: {String(usage.error ?? "unknown error")}
      </p>
    );
  }

  const { totals, rows, limits } = usage.data;
  const maxTokens = Math.max(1, ...rows.map((row) => row.tokens_in + row.tokens_out));
  const tokenLimit = limits?.token_limit ?? null;
  const requestLimit = limits?.request_limit ?? null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-800">Usage</h1>
        <div className="flex items-center gap-2">
          {RANGES.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setDays(value)}
              className={`rounded-md px-3 py-1.5 text-sm ${
                days === value
                  ? "bg-indigo-600 font-medium text-white"
                  : "border border-slate-300 text-slate-600 hover:bg-slate-100"
              }`}
            >
              {value === 1 ? "Today" : `${value} days`}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {[
          { id: "requests", label: "Requests", value: totals.requests.toLocaleString() },
          { id: "tokens-in", label: "Tokens in", value: totals.tokens_in.toLocaleString() },
          { id: "tokens-out", label: "Tokens out", value: totals.tokens_out.toLocaleString() },
          {
            id: "avg-latency",
            label: "Avg latency",
            value: `${totals.avg_latency_ms.toLocaleString()} ms`,
          },
        ].map((card) => (
          <div
            key={card.id}
            data-testid={`totals-${card.id}`}
            className="rounded-lg border border-slate-200 bg-white p-4"
          >
            <div className="text-xs uppercase tracking-wide text-slate-400">{card.label}</div>
            <div className="mt-1 text-lg font-semibold text-slate-800">{card.value}</div>
          </div>
        ))}
      </div>

      <div className="rounded-lg border border-slate-200 bg-white p-4">
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Today vs your daily limits
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-8 gap-y-2 text-sm text-slate-600">
          <span data-testid="requests-quota">
            Requests: {limits?.requests_used ?? 0} / {requestLimit ?? "no limit"}
          </span>
          <span data-testid="tokens-quota">
            Tokens: {limits?.tokens_used ?? 0} / {tokenLimit ?? "no limit"}
          </span>
          <span>GPU hours (cluster, all users): {totals.gpu_hours.toFixed(2)}</span>
        </div>
        <p className="mt-2 text-xs text-slate-400">
          Limits are soft, counted per UTC day and include requests buffered since the last flush.
        </p>
      </div>

      {rows.length === 0 ? (
        <div className="rounded-lg border border-slate-200 bg-white p-6 text-slate-500">
          No requests in this window yet.
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-2">Day</th>
                {isAdmin && <th className="px-4 py-2">User</th>}
                <th className="px-4 py-2">Model</th>
                <th className="px-4 py-2 text-right">Requests</th>
                <th className="px-4 py-2 text-right">Tokens in</th>
                <th className="px-4 py-2 text-right">Tokens out</th>
                <th className="px-4 py-2 text-right">Avg latency</th>
                <th className="px-4 py-2">Token share</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={`${row.period_start}-${row.user_id}-${row.api_key_id}-${row.model_id}`}
                  data-testid="usage-row"
                  className="border-t border-slate-100"
                >
                  <td className="px-4 py-2 font-mono text-xs text-slate-500">
                    {row.period_start}
                  </td>
                  {isAdmin && <td className="px-4 py-2 text-slate-600">{row.username}</td>}
                  <td className="px-4 py-2 text-slate-700">{row.model_name}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{row.requests}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{row.tokens_in}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{row.tokens_out}</td>
                  <td className="px-4 py-2 text-right tabular-nums">{row.avg_latency_ms} ms</td>
                  <td className="px-4 py-2">
                    <Bar value={row.tokens_in + row.tokens_out} max={maxTokens} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}