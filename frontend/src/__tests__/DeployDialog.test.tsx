import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DeployDialog from "../components/DeployDialog";
import type { ModelInfo, Worker } from "../api/types";

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

const CLUSTER = {
  id: "c1",
  name: "default",
  description: null,
  created_at: "2026-01-01T00:00:00Z",
};

const WORKER: Worker = {
  id: "w1",
  cluster_id: "c1",
  name: "node-1",
  hostname: "node-1",
  state: "connected",
  version: null,
  os: null,
  cpu_cores: null,
  memory_mb: null,
  last_seen_at: null,
  instances: [],
  gpus: [
    {
      id: 0,
      index: 0,
      vendor: "nvidia",
      name: "RTX 4090",
      vram_mb: 8192,
      used_vram_mb: 0,
      utilization_pct: 0,
      uuid: null,
      driver_version: null,
    },
  ],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function apiStub(overrides?: { deployError?: { detail: string } | null; workers?: Worker[] }) {
  return vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    const path = String(url);
    if (path.includes("/compatibility")) {
      return Promise.resolve(jsonResponse({ engine_vendors: { vllm: ["mock", "nvidia"], sglang: ["mock", "nvidia"] } }));
    }
    if (path.includes("/model-instances") && method === "POST") {
      return Promise.resolve(
        overrides?.deployError
          ? jsonResponse(overrides.deployError, 400)
          : jsonResponse({ id: "i1" }, 201),
      );
    }
    if (path.includes("/clusters")) {
      return Promise.resolve(jsonResponse([CLUSTER]));
    }
    if (path.includes("/workers")) {
      return Promise.resolve(jsonResponse(overrides?.workers ?? []));
    }
    return Promise.resolve(jsonResponse([]));
  });
}

function renderDialog(onClose: () => void) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <DeployDialog model={MODEL} onClose={onClose} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

test("renders cluster, engine, profile and GPU fields", async () => {
  vi.stubGlobal("fetch", apiStub());
  renderDialog(() => {});

  await waitFor(() => expect(screen.getByLabelText("Cluster")).toHaveValue("c1"));
  expect(screen.getByLabelText("Engine")).toHaveValue("vllm");
  expect(screen.getByRole("radio", { name: /Low latency/ })).toBeChecked();
  expect(screen.getByRole("radio", { name: /Auto \(best fit\)/ })).toBeChecked();
  expect(screen.getByRole("button", { name: "Deploy" })).toBeInTheDocument();
});

test("submits a deploy request and closes the dialog", async () => {
  const fetchMock = apiStub();
  vi.stubGlobal("fetch", fetchMock);
  const onClose = vi.fn();

  renderDialog(onClose);
  await waitFor(() => expect(screen.getByLabelText("Cluster")).toHaveValue("c1"));

  const form = document.querySelector("form");
  expect(form).not.toBeNull();
  fireEvent.submit(form!);

  await waitFor(() => expect(onClose).toHaveBeenCalled());
  const deployCall = fetchMock.mock.calls.find(([url, init]) =>
    String(url).includes("/model-instances") && (init as RequestInit | undefined)?.method === "POST",
  );
  expect(deployCall).toBeDefined();
  const [url, init] = deployCall as [RequestInfo | URL, RequestInit];
  expect(String(url)).toContain("/model-instances");
  expect(init.method).toBe("POST");
  expect(JSON.parse(init.body as string)).toEqual({
    model_id: "m1",
    cluster_id: "c1",
    engine: "vllm",
    profile: "latency",
    gpu_selection: "auto",
    replicas: 1,
  });
});

test("shows server error detail inline when deploy fails", async () => {
  vi.stubGlobal(
    "fetch",
    apiStub({ deployError: { detail: "no GPU with enough free VRAM in cluster" } }),
  );

  renderDialog(() => {});
  await waitFor(() => expect(screen.getByLabelText("Cluster")).toHaveValue("c1"));

  fireEvent.submit(document.querySelector("form")!);
  await waitFor(() =>
    expect(screen.getByText("no GPU with enough free VRAM in cluster")).toBeInTheDocument(),
  );
});

test.each(["0", "9", "1.5"])("blocks submit when replicas is invalid (%s)", async (replicas) => {
  const fetchMock = apiStub();
  vi.stubGlobal("fetch", fetchMock);
  renderDialog(() => {});
  await waitFor(() => expect(screen.getByLabelText("Cluster")).toHaveValue("c1"));

  fireEvent.change(screen.getByLabelText("Replicas"), { target: { value: replicas } });
  fireEvent.submit(document.querySelector("form")!);

  expect(screen.getByText("Replicas must be an integer between 1 and 8")).toBeInTheDocument();
  const deployCall = fetchMock.mock.calls.find(([url, init]) =>
    String(url).includes("/model-instances") && (init as RequestInit | undefined)?.method === "POST",
  );
  expect(deployCall).toBeUndefined();
});

test("submits non-default replicas count", async () => {
  const fetchMock = apiStub();
  vi.stubGlobal("fetch", fetchMock);
  const onClose = vi.fn();

  renderDialog(onClose);
  await waitFor(() => expect(screen.getByLabelText("Cluster")).toHaveValue("c1"));

  fireEvent.change(screen.getByLabelText("Replicas"), { target: { value: "3" } });
  fireEvent.submit(document.querySelector("form")!);

  await waitFor(() => expect(onClose).toHaveBeenCalled());
  const deployCall = fetchMock.mock.calls.find(([url, init]) =>
    String(url).includes("/model-instances") && (init as RequestInit | undefined)?.method === "POST",
  );
  expect(deployCall).toBeDefined();
  const [, init] = deployCall as [RequestInfo | URL, RequestInit];
  expect(JSON.parse(init.body as string)).toMatchObject({ replicas: 3 });
});

test("hides replicas field and submits default 1 in manual placement", async () => {
  const fetchMock = apiStub({ workers: [WORKER] });
  vi.stubGlobal("fetch", fetchMock);
  const onClose = vi.fn();

  renderDialog(onClose);
  await waitFor(() => expect(screen.getByLabelText("Cluster")).toHaveValue("c1"));

  fireEvent.click(screen.getByRole("radio", { name: /Manual/ }));
  expect(screen.queryByLabelText("Replicas")).toBeNull();
  await waitFor(() => expect(screen.getByText(/GPU 0/)).toBeInTheDocument());
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.submit(document.querySelector("form")!);

  await waitFor(() => expect(onClose).toHaveBeenCalled());
  const deployCall = fetchMock.mock.calls.find(([url, init]) =>
    String(url).includes("/model-instances") && (init as RequestInit | undefined)?.method === "POST",
  );
  expect(deployCall).toBeDefined();
  const [, init] = deployCall as [RequestInfo | URL, RequestInit];
  expect(JSON.parse(init.body as string)).toMatchObject({
    replicas: 1,
    gpu_selection: { worker_id: "w1", gpu_indexes: [0] },
  });
});
