import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { Deployment, Instance, ModelInfo } from "../api/types";
import InstancesPage from "../pages/InstancesPage";

const MODEL: ModelInfo = {
  id: "m1",
  name: "Qwen/Qwen2.5-0.5B-Instruct",
  display_name: "Qwen2.5 0.5B Instruct",
  category: "llm",
  description: null,
  params_b: 0.5,
  vram_required_mb: 2048,
  requires_hf_token: false,
  license: "apache-2.0",
  is_builtin: true,
  supported_engines: ["vllm", "sglang"],
};

const INSTANCE: Instance = {
  id: "i1",
  model_id: "m1",
  cluster_id: "c1",
  worker_id: "w1",
  engine: "vllm",
  profile: "latency",
  gpu_indexes: [0],
  state: "error",
  desired_state: "running",
  generation: 2,
  port: 8010,
  error_detail: "health probe timed out",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:05:00Z",
  model: MODEL,
  worker_name: "node-1",
  deployment_id: "d1",
};

const DEPLOYMENT: Deployment = {
  id: "d1",
  model_id: "m1",
  cluster_id: "c1",
  engine: "vllm",
  profile: "latency",
  min_replicas: 1,
  max_replicas: 1,
  created_at: "2026-01-01T00:00:00Z",
  model: MODEL,
  instances: [INSTANCE],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiStub(logTail: string | null) {
  return vi.fn((url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes("/deployments")) return Promise.resolve(jsonResponse([DEPLOYMENT]));
    if (path.includes("/logs")) {
      return Promise.resolve(
        jsonResponse({
          instance_id: "i1",
          state: "error",
          log_tail: logTail,
          updated_at: "2026-01-01T00:05:00Z",
        }),
      );
    }
    return Promise.resolve(jsonResponse([]));
  });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <InstancesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("logs dialog shows the snapshot reported by the worker", async () => {
  vi.stubGlobal("fetch", apiStub("vllm: CUDA out of memory"));
  renderPage();

  const button = await screen.findByRole("button", { name: "Logs" });
  fireEvent.click(button);

  await waitFor(() =>
    expect(screen.getByTestId("instance-logs")).toHaveTextContent("vllm: CUDA out of memory"),
  );
  expect(screen.getByText(/Last snapshot reported by the worker/)).toBeInTheDocument();
});

test("logs dialog falls back to an empty state", async () => {
  vi.stubGlobal("fetch", apiStub(null));
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Logs" }));

  await waitFor(() => expect(screen.getByTestId("instance-logs")).toHaveTextContent("No logs yet"));
});

test("logs dialog closes without touching the table", async () => {
  vi.stubGlobal("fetch", apiStub("some log line"));
  renderPage();

  fireEvent.click(await screen.findByRole("button", { name: "Logs" }));
  await waitFor(() => expect(screen.getByTestId("instance-logs")).toBeInTheDocument());

  fireEvent.click(screen.getByRole("button", { name: "Close" }));

  await waitFor(() => expect(screen.queryByTestId("instance-logs")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Logs" })).toBeInTheDocument();
});