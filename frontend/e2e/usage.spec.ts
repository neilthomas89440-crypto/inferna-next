import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test("gateway traffic shows up in the usage page", async ({ page }) => {
  test.setTimeout(120_000);
  const keyName = `e2e-usage-${Date.now()}`;
  await login(page);

  // Create a key exactly like keys.spec.ts does, then send one request through /v1.
  await page.getByRole("link", { name: "API Keys" }).click();
  await page.getByRole("button", { name: "New key" }).click();
  await page.getByLabel("Name").fill(keyName);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  const rawKey =
    (await page.locator("div.fixed.inset-0").locator("code").first().textContent()) ?? "";
  await page.getByRole("button", { name: "Done" }).click();

  // The mock stack has no engine upstream, so the call may fail (502) — either way the
  // request must be accounted for.
  const status = await page.evaluate(async (key) => {
    const response = await fetch("/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key.trim()}` },
      body: JSON.stringify({
        model: "any-model",
        messages: [{ role: "user", content: "hi" }],
        stream: true,
      }),
    });
    return response.status;
  }, rawKey);
  expect([200, 400, 404, 502]).toContain(status);

  await page.getByRole("link", { name: "Usage" }).click();
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  // The burst may not be flushed for up to ~30s; the quota line covers today's counters.
  await expect
    .poll(async () => (await page.getByTestId("requests-quota").innerText()), {
      timeout: 60_000,
    })
    .not.toBe("Requests: 0 / no limit");

  // Clean up so the serialized specs start from the same state.
  await page.getByRole("link", { name: "API Keys" }).click();
  const row = page.locator("tbody tr", { hasText: keyName }).first();
  await row.getByRole("button", { name: "Revoke" }).click();
  await page
    .locator("div.fixed.inset-0")
    .getByRole("button", { name: "Revoke", exact: true })
    .click();
  await expect(row.getByText("revoked")).toBeVisible();
});