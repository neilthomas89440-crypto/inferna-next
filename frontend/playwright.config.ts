import { defineConfig, devices } from "@playwright/test";

// CI-only: runs against `vite preview` on :8080 with the API at :8000.
export default defineConfig({
  testDir: "./e2e",
  // One shared stack: lifecycle specs (deploy, keys) must not race across projects,
  // so workers are serialized even though fullyParallel is set.
  fullyParallel: true,
  workers: 1,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://localhost:8080",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
  webServer: {
    command: "npm run preview",
    url: "http://localhost:8080",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
