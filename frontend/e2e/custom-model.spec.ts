import { expect, test, type Page } from "@playwright/test";

const REPO = "acme/llm-3b-instruct";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test("custom model: add, badge, deploy, delete", async ({ page }) => {
  test.setTimeout(150_000);
  await login(page);
  await page.getByRole("link", { name: "Models" }).click();

  const card = page.getByTestId(`model-card-${REPO}`);
  await expect(card).toHaveCount(0);

  await page.getByRole("button", { name: "Add model" }).click();
  await page.getByLabel("Hugging Face repository").fill(REPO);
  await page.getByLabel("VRAM required (MB)").fill("8192");
  await page.getByRole("button", { name: "Add to catalog" }).click();

  await expect(page.getByTestId(`custom-badge-${REPO}`)).toBeVisible();
  await expect(card.getByText("acme/llm-3b-instruct")).toBeVisible();

  // Deploy it like any builtin model.
  await card.getByRole("button", { name: "Deploy" }).click();
  await expect(page.getByRole("heading", { name: /Deploy .+/ })).toBeVisible();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });

  const group = page.locator('tbody[data-testid^="deployment-group-"]', { hasText: REPO });
  const row = group.locator('tr[data-testid^="instance-row-"]');
  await expect(row).toHaveCount(1);
  await expect(row.getByText("running", { exact: true })).toBeVisible({ timeout: 90000 });

  // Cleanup: the instance first, then the model (deletion is refused while deployed).
  await row.getByRole("button", { name: "Delete" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: /delete/i }).click();
  await expect(row).toHaveCount(0);

  await page.getByRole("link", { name: "Models" }).click();
  await card.getByRole("button", { name: "Delete" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: /delete/i }).click();
  await expect(card).toHaveCount(0);
});