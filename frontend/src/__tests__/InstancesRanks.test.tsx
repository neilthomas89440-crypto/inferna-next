import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { Deployment, Instance, ModelInfo } from "../api/types";
import { AuthProvider } from "../context/AuthContext";
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

function instance(rank: number | null, port: number | null, id: string): Instance {
  return {
    id,
    model_id: "m1",
    cluster_id: "c1",
    worker_id: `w${rank}`,
    engine: "vllm",
    profile: "latency",
    gpu_indexes: [0],
    state: "running",
    desired_state: "running",
    generation: 1,
    port,
    error_detail: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:05:00Z",
    model: MODEL,
    worker_name: rank === 0 ? "node-a" : "node-b",
    deployment_id: "d1",
    parallel_group_id: rank === null ? null : "g1",
    node_rank: rank,
    world_size: rank === null ? null : 2,
    parallel_mode: rank === null ? null : "pp",
    rendezvous: rank === null ? null : "10.0.0.1:8110",
  };
}

function deployment(instances: Instance[]): Deployment {
  return {
    id: "d1",
    model_id: "m1",
    cluster_id: "c1",
    engine: "vllm",
    profile: "latency",
    min_replicas: 1,
    max_replicas: 1,
    created_at: "2026-01-01T00:00:00Z",
    model: MODEL,
    instances,
  };
}

function renderPage(deployments: Deployment[]) {
  localStorage.setItem("token", "tok");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: RequestInfo | URL) => {
      const path = String(url);
      if (path.includes("/auth/me")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              id: "u1",
              username: "admin",
              email: null,
              role: "admin",
              is_active: true,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify(deployments), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter>
          <InstancesPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

test("a parallel group shows the head and its ranks", async () => {
  renderPage([deployment([instance(0, 8010, "i0"), instance(1, null, "i1")])]);

  await waitFor(() => expect(screen.getByTestId("rank-badge-i0")).toBeInTheDocument());
  expect(screen.getByTestId("rank-badge-i0")).toHaveTextContent("head / 2");
  expect(screen.getByTestId("rank-badge-i1")).toHaveTextContent("rank 1 / 2");
  // The follower shows where it points, and its own (absent) port.
  expect(screen.getByText("→ 10.0.0.1:8110")).toBeInTheDocument();
});

test("only the head offers an endpoint", async () => {
  renderPage([deployment([instance(0, 8010, "i0"), instance(1, null, "i1")])]);

  const headRow = (await screen.findByTestId("rank-badge-i0")).closest("tr");
  const followerRow = screen.getByTestId("rank-badge-i1").closest("tr");
  expect(headRow).not.toBeNull();
  expect(followerRow).not.toBeNull();

  expect(within(headRow as HTMLElement).getByRole("button", { name: "Endpoint" })).toBeInTheDocument();
  expect(
    within(followerRow as HTMLElement).queryByRole("button", { name: "Endpoint" }),
  ).not.toBeInTheDocument();
});

test("single-node instances keep their old presentation", async () => {
  renderPage([deployment([instance(null, 8010, "solo")])]);

  await waitFor(() => expect(screen.getByRole("button", { name: "Endpoint" })).toBeInTheDocument());
  expect(screen.queryByTestId("rank-badge-solo")).not.toBeInTheDocument();
});
test("the window controls send a range request and label autoscaling", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  localStorage.setItem("token", "tok");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.includes("/auth/me")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ id: "u1", username: "admin", email: null, role: "admin", is_active: true }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      calls.push({ url: path, init });
      const body = init?.method === "PUT" ? { ...deployment([instance(0, 8010, "i0")]), min_replicas: 1, max_replicas: 3 } : [deployment([instance(0, 8010, "i0")])];
      return Promise.resolve(
        new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter>
          <InstancesPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );

  // A pinned deployment (min == max) says so.
  await waitFor(() => expect(screen.getByText("autoscaling off")).toBeInTheDocument());
  await waitFor(() => expect(screen.getByLabelText(/Min replicas/)).toHaveValue(1));

  fireEvent.change(screen.getByLabelText(/Max replicas/), { target: { value: "3" } });
  fireEvent.click(screen.getByLabelText(/Apply range/));

  await waitFor(() => {
    const put = calls.find((call) => call.init?.method === "PUT");
    expect(put).toBeDefined();
    expect(String(put?.url)).toContain("/deployments/d1/range");
    expect(JSON.parse(String(put?.init?.body))).toEqual({ min_replicas: 1, max_replicas: 3 });
  });
});
