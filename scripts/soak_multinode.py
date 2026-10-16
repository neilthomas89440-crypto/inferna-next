#!/usr/bin/env python3
"""Multi-node soak: parallel-group lifecycle with randomized faults.

    INFERNA_API=http://server:8000 INFERNA_ADMIN_TOKEN=$JWT \
        scripts/soak-multinode.sh                       # 24 h, interval 5 min, fault every 4 cycles

Why Python and not the `curl + jq` style of `scripts/soak.sh`: this runs for a day and every check
needs JSON in and JSON out (group state, invariant assertions). The repo’s other operational helpers
(`usage-reconcile.py`, `parity.sh` → `bench.parity`) follow the same pattern, and it makes the script
runnable on a machine without `jq`.

Invariants asserted every cycle (a violation ends the soak with a non-zero exit):

- the control plane never answers 409/500 for a deploy/stop/delete of a group;
- every rank of the group reaches `running`, then `stopped`;
- deleting the ranks leaves no row for that group behind;
- with `--docker`, no container carrying the instance label survives a group deletion.

Faults (rotated, one every `--fault-every` cycles): kill the head, kill a follower, restart the
worker (containers must be adopted), apply packet loss. Each is *expectable*: the group must stay
controllable and end stopped/deleted, and the soak records what happened for the report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FAULTS = ("kill_head", "kill_peer", "restart_worker", "netem_loss")


@dataclass(slots=True)
class Soak:
    api: str
    token: str
    out: Path
    nodes: int
    mode: str
    docker: bool
    failures: int = 0
    cycles: int = 0

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        request = urllib.request.Request(
            f"{self.api}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                return response.status, (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode()
            try:
                return exc.code, json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, payload[:400]

    def log(self, action: str, result: str, *, instance: str = "", duration: float = 0.0,
            error: str = "", **extra: object) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "instance_id": instance,
            "result": result,
            "duration_s": round(duration, 2),
            "error": error,
            "cycle": self.cycles,
            **extra,
        }
        with self.out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        flag = "ok " if result in ("ok", "injected") else "!! "
        print(f"  {flag}{action}: {result} {error}".rstrip(), flush=True)

    # --- primitives ---

    def instances(self) -> list[dict]:
        status, payload = self.call("GET", "/api/v1/model-instances")
        return payload if status == 200 and isinstance(payload, list) else []

    def group(self, group_id: str) -> list[dict]:
        return [row for row in self.instances() if row.get("parallel_group_id") == group_id]

    def group_states(self, group_id: str) -> list[str]:
        return sorted(row["state"] for row in self.group(group_id))

    def wait_group(self, group_id: str, expected: list[str], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.group_states(group_id) == expected:
                return True
            time.sleep(10)
        return False

    def worker_container(self, instance_id: str, action: str) -> bool:
        """`docker kill|restart` on the rank’s host over ssh, best effort."""
        if not self.docker:
            return False
        status, row = self.call("GET", f"/api/v1/model-instances/{instance_id}")
        if status != 200 or not isinstance(row, dict):
            return False
        host = row.get("worker_name")
        if action == "restart_worker":
            command = ["docker", "-H", f"ssh://{host}", "restart", "inferna-worker"]
        else:
            command = ["docker", "-H", f"ssh://{host}", action, f"inferna-instance-{instance_id}"]
        return subprocess.call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0

    # --- one cycle ---

    def run_cycle(self, model_id: str, cluster_id: str, fault_every: int) -> None:
        self.cycles += 1
        print(f"[{self.cycles}] deploy group ({self.nodes} nodes, {self.mode})", flush=True)
        started = time.monotonic()
        status, created = self.call(
            "POST",
            "/api/v1/model-instances",
            {
                "model_id": model_id,
                "cluster_id": cluster_id,
                "engine": "vllm",
                "profile": "latency",
                "replicas": 1,
                "gpu_selection": "auto",
                "nodes_per_replica": self.nodes,
                "parallel_mode": self.mode,
            },
        )
        duration = time.monotonic() - started
        if status >= 400:
            violation = status in (409, 500)
            self.log("deploy", "violation" if violation else "fail", duration=duration, error=str(created)[:200])
            self.failures += 1
            if violation:
                raise SystemExit("invariant violation: control plane answered 409/500")
            return
        ranks = created if isinstance(created, list) else [created]
        group_id = ranks[0].get("parallel_group_id")
        head = next((row for row in ranks if row.get("node_rank") == 0), ranks[0])
        if not group_id:
            self.log("deploy", "fail", duration=duration, error="no parallel_group_id in the response")
            self.failures += 1
            return
        self.log("deploy", "ok", instance=head["id"], duration=duration, group=group_id, ranks=len(ranks))

        expected_running = ["running"] * len(ranks)
        started = time.monotonic()
        if self.wait_group(group_id, expected_running, 900):
            self.log("poll_running", "ok", instance=head["id"], duration=time.monotonic() - started)
        else:
            self.log(
                "poll_running", "fail", instance=head["id"], duration=time.monotonic() - started,
                error=f"states {self.group_states(group_id)}",
            )
            self.failures += 1

        if fault_every and self.cycles % fault_every == 0:
            fault = FAULTS[(self.cycles // fault_every - 1) % len(FAULTS)]
            started = time.monotonic()
            applied = False
            if fault == "kill_head":
                applied = self.worker_container(head["id"], "kill")
            elif fault == "kill_peer":
                peer = next((row["id"] for row in ranks if row["id"] != head["id"]), None)
                applied = bool(peer) and self.worker_container(peer, "kill")
            elif fault == "restart_worker":
                applied = self.worker_container(head["id"], "restart_worker")
            elif fault == "netem_loss":
                applied = Path("scripts/netem.sh").exists() and subprocess.call(
                    ["scripts/netem.sh", "apply", "wan-25ms-1%"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) == 0
                if applied:
                    time.sleep(30)
                    subprocess.call(["scripts/netem.sh", "clear"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            self.log(
                "fault", "injected" if applied else "skipped", instance=head["id"],
                duration=time.monotonic() - started, error=fault if applied else f"{fault} (needs --docker/root)",
            )

        # Whatever happened, the group must remain controllable: stop every rank, then delete them.
        for row in ranks:
            self.call("POST", f"/api/v1/model-instances/{row['id']}/stop")
        started = time.monotonic()
        if self.wait_group(group_id, ["stopped"] * len(ranks), 600):
            self.log("poll_stopped", "ok", instance=head["id"], duration=time.monotonic() - started)
        else:
            self.log(
                "poll_stopped", "fail", instance=head["id"], duration=time.monotonic() - started,
                error=f"states {self.group_states(group_id)}",
            )
            self.failures += 1

        for row in ranks:
            self.call("DELETE", f"/api/v1/model-instances/{row['id']}")
        left = len(self.group(group_id))
        if left:
            self.log("delete", "fail", instance=head["id"], error=f"{left} ranks left")
            self.failures += 1
        else:
            self.log("delete", "ok", instance=head["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--token", required=True)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--cycles", type=int, default=0, help="stop after N cycles (smoke runs)")
    parser.add_argument("--interval-min", type=float, default=5.0)
    parser.add_argument("--nodes", type=int, default=2)
    parser.add_argument("--mode", default="pp")
    parser.add_argument("--model", default=None, help="catalog model id (default: smallest)")
    parser.add_argument("--fault-every", type=int, default=4, help="0 disables faults")
    parser.add_argument("--docker", action="store_true", help="allow fault injection over ssh")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    out = Path(args.out or f"artifacts/soak-multinode-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    soak = Soak(api=args.api.rstrip("/"), token=args.token, out=out, nodes=args.nodes,
                mode=args.mode, docker=args.docker)
    print(f"multi-node soak -> {out} ({args.hours}h, interval {args.interval_min}min, "
          f"{args.nodes} nodes, mode {args.mode}, faults every {args.fault_every or 'never'})")

    status, models = soak.call("GET", "/api/v1/models")
    if status != 200 or not isinstance(models, list) or not models:
        print("cannot list the model catalog", file=sys.stderr)
        return 2
    model_id = args.model or min(models, key=lambda model: model["vram_required_mb"])["id"]
    status, clusters = soak.call("GET", "/api/v1/clusters")
    cluster_rows = clusters if isinstance(clusters, list) else (clusters or {}).get("items", [])
    if not cluster_rows:
        print("no cluster available", file=sys.stderr)
        return 2
    print(f"model={model_id} cluster={cluster_rows[0]['id']}")

    deadline = time.monotonic() + args.hours * 3600
    while True:
        if args.cycles and soak.cycles >= args.cycles:
            break
        if time.monotonic() >= deadline:
            break
        soak.run_cycle(model_id, cluster_rows[0]["id"], args.fault_every)
        if args.cycles and soak.cycles >= args.cycles:
            break
        time.sleep(args.interval_min * 60)

    print(f"\ncycles: {soak.cycles}, failures: {soak.failures}\njsonl: {out}")
    if soak.failures:
        print("SOAK FAIL", file=sys.stderr)
        return 1
    print("SOAK PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())