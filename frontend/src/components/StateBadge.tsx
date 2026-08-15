import type { InstanceState } from "../api/types";

const STYLES: Record<InstanceState, string> = {
  scheduled: "bg-slate-100 text-slate-700",
  starting: "bg-blue-100 text-blue-700",
  running: "bg-emerald-100 text-emerald-700",
  stopped: "bg-amber-100 text-amber-700",
  error: "bg-red-100 text-red-700",
};

export default function StateBadge({ state }: { state: InstanceState }) {
  return (
    <span
      className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${STYLES[state]}`}
    >
      {state}
    </span>
  );
}
