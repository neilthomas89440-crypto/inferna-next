import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ModelInfo } from "../api/types";
import { AuthProvider } from "../context/AuthContext";
import ModelsPage from "../pages/ModelsPage";

const BUILTIN: ModelInfo = {
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

const CUSTOM: ModelInfo = {
  ...BUILTIN,
  id: "m2",
  name: "acme/llm-3b-instruct",
  display_name: "acme/llm-3b-instruct",
  params_b: null,
  vram_required_mb: 8192,
  is_builtin: false,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage(role: "admin" | "user", fetchImpl: typeof fetch) {
  localStorage.setItem("token", "tok");
  vi.stubGlobal("fetch", fetchImpl);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter>
          <ModelsPage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function stub(role: "admin" | "user", models: ModelInfo[], calls?: { url: string; init?: RequestInit }[]) {
  return vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.includes("/auth/me")) {
      return Promise.resolve(
        jsonResponse({ id: "u1", username: "admin", email: null, role, is_active: true }),
      );
    }
    if (init?.method === "POST") {
      calls?.push({ url: path, init });
      return Promise.resolve(jsonResponse(CUSTOM, 201));
    }
    return Promise.resolve(jsonResponse(models));
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

test("admin adds a model and the request carries the entered fields", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  renderPage("admin", stub("admin", [BUILTIN], calls));

  const addButton = await screen.findByRole("button", { name: "Add model" });
  fireEvent.click(addButton);
  fireEvent.change(screen.getByLabelText("Hugging Face repository"), {
    target: { value: "acme/llm-3b-instruct" },
  });
  fireEvent.change(screen.getByLabelText("VRAM required (MB)"), { target: { value: "8192" } });
  fireEvent.click(screen.getByRole("button", { name: "Add to catalog" }));

  await waitFor(() => expect(calls).toHaveLength(1));
  expect(JSON.parse(String(calls[0].init?.body))).toEqual({
    hf_repo_id: "acme/llm-3b-instruct",
    display_name: null,
    category: "llm",
    vram_required_mb: 8192,
    requires_hf_token: false,
    license: null,
    description: null,
  });
});

test("custom models are badged and deletable, builtins are not", async () => {
  renderPage("admin", stub("admin", [BUILTIN, CUSTOM]));

  await waitFor(() =>
    expect(screen.getByTestId("custom-badge-acme/llm-3b-instruct")).toBeInTheDocument(),
  );
  expect(screen.queryByTestId("custom-badge-Qwen/Qwen2.5-0.5B-Instruct")).not.toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1);
});

test("non-admin sees the catalog without write controls", async () => {
  renderPage("user", stub("user", [BUILTIN, CUSTOM]));

  await waitFor(() => expect(screen.getByText("Qwen2.5 0.5B Instruct")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "Add model" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
});