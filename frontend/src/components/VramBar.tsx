interface VramBarProps {
  usedMb: number;
  totalMb: number;
}

export default function VramBar({ usedMb, totalMb }: VramBarProps) {
  const pct = totalMb > 0 ? Math.min(100, Math.round((usedMb / totalMb) * 100)) : 0;
  const color = pct >= 90 ? "bg-red-500" : pct >= 60 ? "bg-amber-500" : "bg-emerald-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-24 overflow-hidden rounded-full bg-slate-200">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-slate-500">
        {Math.round(usedMb / 1024)} / {Math.round(totalMb / 1024)} GB
      </span>
    </div>
  );
}
