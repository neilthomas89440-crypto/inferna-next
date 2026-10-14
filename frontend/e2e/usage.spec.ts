import { expect, test, type Page } from "@playwright/test";

const MODEL = "Qwen/Qwen2.5-0.5B-Instruct";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test("gateway traffic shows up in the usage page", async ({ page }) => {
  test.setTimeout(180_000);
  const keyName = `e2e-usage-${Date.now()}`;
  await login(page);

  // A model must be deployed for the gateway to resolve it: the mock worker has no engine
  // HTTP server, so the proxied call ends in 502 — which still has to be accounted for.
  await page.getByRole("link", { name: "Models" }).click();
  await page
    .getByTestId(`model-card-${MODEL}`)
    .getByRole("button", { name: "Deploy" })
    .click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });

  const row = page.locator('tr[data-testid^="instance-row-"]');
  await expect(row).toHaveCount(1);
  await expect(row.getByText("running", { exact: true })).toBeVisible({ timeout: 90000 });

  // Issue a key and send one request through the gateway proxy.
  await page.getByRole("link", { name: "API Keys" }).click();
  await page.getByRole("button", { name: "New key" }).click();
  await page.getByLabel("Name").fill(keyName);
  await page.getByRole("button", { name: "Create", exact: true }).click();
  const rawKey =
    (await page.locator("div.fixed.inset-0").locator("code").first().textContent()) ?? "";
  expect(rawKey.length).toBeGreaterThan(10);
  await page.getByRole("button", { name: "Done" }).click();

  const status = await page.evaluate(
    async ([key, model]) => {
      const response = await fetch("/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${key.trim()}` },
        body: JSON.stringify({
          model,
          messages: [{ role: "user", content: "hi" }],
          stream: true,
        }),
      });
      return response.status;
    },
    [rawKey, MODEL] as const,
  );
  expect([200, 502]).toContain(status);

  // The quota line counts records *and* the not-yet-flushed buffer, so the request shows up
  // well before the 30 s flush interval elapses.
  await page.getByRole("link", { name: "Usage" }).click();
  await expect(page.getByRole("heading", { name: "Usage" })).toBeVisible();
  await expect
    .poll(async () => (await page.getByTestId("requests-quota").innerText()).trim(), {
      timeout: 60_000,
    })
    .not.toBe("Requests: 0 / no limit");

  // Cleanup so the serialized specs start from the same state.
  await page.getByRole("link", { name: "API Keys" }).click();
  const keyRow = page.locator("tbody tr", { hasText: keyName }).first();
  await keyRow.getByRole("button", { name: "Revoke" }).click();
  await page
    .locator("div.fixed.inset-0")
    .getByRole("button", { name: "Revoke", exact: true })
    .click();
  await expect(keyRow.getByText("revoked")).toBeVisible();

  await page.getByRole("link", { name: "Instances" }).click();
  await page.locator('tr[data-testid^="instance-row-"]').getByRole("button", { name: "Delete" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: /delete/i }).click();
  await expect(page.locator('tr[data-testid^="instance-row-"]')).toHaveCount(0);
});