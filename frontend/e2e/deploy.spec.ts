import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test("full mock deploy lifecycle", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);
  await page.getByRole("link", { name: "Models" }).click();
  await page.getByTestId("model-card-Qwen/Qwen2.5-0.5B-Instruct").getByRole("button", { name: "Deploy" }).click();
  await expect(page.getByRole("heading", { name: /Deploy .+/ })).toBeVisible();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Deploy", exact: true }).click();
  // Deploy POST can take a while on a cold CI stack — wait for the navigation itself.
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });
  await expect(page.getByRole("heading", { name: "Instances" })).toBeVisible();
  const group = page.locator('tbody[data-testid^="deployment-group-"]', { hasText: "Qwen2.5 0.5B Instruct" });
  const row = group.locator('tr[data-testid^="instance-row-"]');
  await expect(row).toHaveCount(1);
  await expect(row.getByText("running", { exact: true })).toBeVisible({ timeout: 90000 });
  await row.getByRole("button", { name: "Stop" }).click();
  await expect(row.getByText("stopped", { exact: true })).toBeVisible({ timeout: 30000 });
  await row.getByRole("button", { name: "Resume" }).click();
  await expect(row.getByText("running", { exact: true })).toBeVisible({ timeout: 90000 });
  await row.getByRole("button", { name: "Delete" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: /delete/i }).click();
  await expect(row).toHaveCount(0);
});
