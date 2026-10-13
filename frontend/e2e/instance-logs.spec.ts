import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test("instance logs show the worker's snapshot through the lifecycle", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);
  await page.getByRole("link", { name: "Models" }).click();
  await page
    .getByTestId("model-card-Qwen/Qwen2.5-0.5B-Instruct")
    .getByRole("button", { name: "Deploy" })
    .click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });

  const row = page.locator('tr[data-testid^="instance-row-"]');
  await expect(row).toHaveCount(1);
  // The mock worker reports "starting" first; the button works for any state.
  await row.getByRole("button", { name: "Logs" }).click();

  await expect(page.getByTestId("instance-logs")).toBeVisible();
  await expect(page.getByTestId("instance-logs")).toHaveText(/mock:/, { timeout: 30000 });
  await expect(row.getByText("running", { exact: true })).toBeVisible({ timeout: 90000 });
  await expect(page.getByTestId("instance-logs")).toHaveText(/mock: engine ready on port 80\d\d/);

  await page.getByRole("button", { name: "Close" }).click();
  await expect(page.getByTestId("instance-logs")).not.toBeVisible();

  // Leave no state behind for the other serialized specs.
  await row.getByRole("button", { name: "Delete" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: /delete/i }).click();
  await expect(row).toHaveCount(0);
});