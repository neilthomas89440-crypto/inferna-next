import { copyText } from "../lib/clipboard";

describe("copyText", () => {
  // jsdom does not implement execCommand; provide a stub once so it can be
  // spied per-test (restoreAllMocks then restores this stub, so nothing leaks).
  beforeAll(() => {
    if (!("execCommand" in document)) {
      Object.defineProperty(document, "execCommand", {
        value: () => false,
        configurable: true,
        writable: true,
      });
    }
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // jsdom clipboard delete needs unchecked cast
    const nav = navigator as unknown as Record<string, unknown>;
    try {
      delete nav.clipboard;
    } catch {
      // ignore
    }
  });

  it("returns copied when navigator.clipboard.writeText resolves", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
    const result = await copyText("hello");
    expect(result).toBe("copied");
  });

  it("falls back to execCommand when writeText rejects", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
      configurable: true,
    });
    vi.spyOn(document, "execCommand").mockReturnValue(true);
    const result = await copyText("hello");
    expect(result).toBe("copied");
  });

  it("returns failed when clipboard is missing and execCommand returns false", async () => {
    const nav = navigator as unknown as Record<string, unknown>;
    try {
      delete nav.clipboard;
    } catch {
      // ignore
    }
    vi.spyOn(document, "execCommand").mockReturnValue(false);
    const result = await copyText("hello");
    expect(result).toBe("failed");
  });
});
