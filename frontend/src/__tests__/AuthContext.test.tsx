import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AuthProvider, useAuth } from "../context/AuthContext";

const ME = { id: "1", username: "admin", role: "admin", is_active: true, email: null };

function TestHarness() {
  const { user, login, logout } = useAuth();
  return (
    <div>
      <span data-testid="user">{user ? user.username : "anonymous"}</span>
      <button onClick={() => login("admin", "inferna")}>login</button>
      <button onClick={logout}>logout</button>
    </div>
  );
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderWithProviders() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <TestHarness />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

test("login stores token in localStorage and sets the user", async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(jsonResponse({ access_token: "tok123" }))
    .mockResolvedValueOnce(jsonResponse(ME));
  vi.stubGlobal("fetch", fetchMock);

  renderWithProviders();
  fireEvent.click(screen.getByText("login"));

  await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("admin"));
  expect(localStorage.getItem("token")).toBe("tok123");
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining("/auth/login"),
    expect.objectContaining({ method: "POST" }),
  );
});

test("logout clears the token and the user", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ access_token: "tok123" }))
      .mockResolvedValueOnce(jsonResponse(ME)),
  );
  renderWithProviders();
  fireEvent.click(screen.getByText("login"));
  await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("admin"));

  fireEvent.click(screen.getByText("logout"));
  expect(screen.getByTestId("user")).toHaveTextContent("anonymous");
  expect(localStorage.getItem("token")).toBeNull();
});

test("bootstraps the user from a stored token on mount", async () => {
  localStorage.setItem("token", "stored-token");
  const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(ME));
  vi.stubGlobal("fetch", fetchMock);

  renderWithProviders();

  await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("admin"));
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining("/auth/me"),
    expect.objectContaining({ headers: expect.any(Headers) }),
  );
});

test("clears an invalid stored token on bootstrap failure", async () => {
  localStorage.setItem("token", "expired-token");
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(jsonResponse({ detail: "nope" }, 401)));

  renderWithProviders();

  await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("anonymous"));
  expect(localStorage.getItem("token")).toBeNull();
});
