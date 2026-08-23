import { copyText } from "../lib/clipboard";

describe("copyText", () => {
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
    document.execCommand = vi.fn(() => true);
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
    document.execCommand = vi.fn(() => false);
    const result = await copyText("hello");
    expect(result).toBe("failed");
  });
});
