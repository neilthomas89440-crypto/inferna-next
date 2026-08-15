import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// jsdom does not implement navigation; stub it so 401 redirects are harmless.
Object.defineProperty(window, "location", {
  value: { ...window.location, assign: vi.fn() },
  writable: true,
});

