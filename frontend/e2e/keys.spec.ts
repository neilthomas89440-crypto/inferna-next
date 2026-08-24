import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("create and revoke an API key", async ({ page }) => {
  const keyName = `e2e-key-${Date.now()}`;
  await login(page);
  await page.getByRole("link", { name: "API Keys" }).click();

  await page.getByRole("button", { name: "New key" }).click();
  await page.getByLabel("Name").fill(keyName);
  await page.getByRole("button", { name: "Create", exact: true }).click();

  // Key shown exactly once, with the inf- prefix.
  await expect(page.getByText("Store this key now — it will not be shown again.")).toBeVisible();
  // Modal root is the first fixed overlay; the table below also has <code> cells.
  const keyText = await page.locator("div.fixed.inset-0").locator("code").first().textContent();
  expect(keyText).toMatch(/^inf-[0-9a-f]{32}$/);
  await page.getByRole("button", { name: "Done" }).click();

  // Unique per-run name: stale revoked rows from earlier runs must not match.
  const row = page.locator("tbody tr", { hasText: keyName }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Revoke" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Revoke", exact: true }).click();
  await expect(row.getByText("revoked")).toBeVisible();
});
