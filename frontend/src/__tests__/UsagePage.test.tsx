import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../context/AuthContext";
import UsagePage from "../pages/UsagePage";

const USAGE = {
  totals: { requests: 12, tokens_in: 120, tokens_out: 240, avg_latency_ms: 900, gpu_hours: 1.5 },
  rows: [
    {
      period_start: "2026-10-14",
      model_id: "m1",
      model_name: "Qwen/Qwen2.5-0.5B-Instruct",
      user_id: "u1",
      username: "admin",
      api_key_id: "k1",
      requests: 12,
      tokens_in: 120,
      tokens_out: 240,
      avg_latency_ms: 900,
      gpu_hours: 1.5,
    },
  ],
  limits: { token_limit: 1000, request_limit: null, requests_used: 12, tokens_used: 360 },
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage(role: "admin" | "user", usage: unknown = USAGE) {
  localStorage.setItem("token", "tok");
  vi.stubGlobal(
    "fetch",
    vi.fn((url: RequestInfo | URL) => {
      const path = String(url);
      if (path.includes("/auth/me")) {
        return Promise.resolve(
          jsonResponse({ id: "u1", username: "admin", email: null, role, is_active: true }),
        );
      }
      return Promise.resolve(jsonResponse(usage));
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter>
          <UsagePage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

test("renders totals, rows and the remaining quota for the signed-in user", async () => {
  renderPage("admin");

  await waitFor(() => expect(screen.getByTestId("usage-row")).toBeInTheDocument());
  expect(screen.getByTestId("totals-requests")).toHaveTextContent("12");
  expect(screen.getByTestId("totals-tokens-in")).toHaveTextContent("120");
  expect(screen.getByTestId("totals-tokens-out")).toHaveTextContent("240");
  expect(screen.getByTestId("totals-avg-latency")).toHaveTextContent("900 ms");
  expect(screen.getByTestId("requests-quota")).toHaveTextContent("Requests: 12 / no limit");
  expect(screen.getByTestId("tokens-quota")).toHaveTextContent("Tokens: 360 / 1000");
  expect(screen.getByText("Qwen/Qwen2.5-0.5B-Instruct")).toBeInTheDocument();
  // Admins get the per-user column.
  expect(screen.getByText("admin")).toBeInTheDocument();
});

test("shows an empty state when the window has no traffic", async () => {
  renderPage("user", { ...USAGE, rows: [], limits: null });

  await waitFor(() =>
    expect(screen.getByText("No requests in this window yet.")).toBeInTheDocument(),
  );
  expect(screen.queryByTestId("usage-row")).not.toBeInTheDocument();
});

test("switching the range refetches with the new window", async () => {
  const fetchMock = vi.fn((url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes("/auth/me")) {
      return Promise.resolve(
        jsonResponse({ id: "u1", username: "admin", email: null, role: "admin", is_active: true }),
      );
    }
    return Promise.resolve(jsonResponse(USAGE));
  });
  localStorage.setItem("token", "tok");
  vi.stubGlobal("fetch", fetchMock);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter>
          <UsagePage />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.getByTestId("usage-row")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: "30 days" }));

  await waitFor(() =>
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("days=30"))).toBe(true),
  );
});