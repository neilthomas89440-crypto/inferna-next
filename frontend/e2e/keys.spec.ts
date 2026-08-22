import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("create and revoke an API key", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "API Keys" }).click();

  await page.getByRole("button", { name: "New key" }).click();
  await page.getByLabel("Name").fill("e2e-key");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  // Key shown exactly once, with the inf- prefix.
  await expect(page.getByText("Store this key now — it will not be shown again.")).toBeVisible();
  const keyText = await page.locator("code").first().textContent();
  expect(keyText).toMatch(/^inf-[0-9a-f]{32}$/);
  await page.getByRole("button", { name: "Done" }).click();

  // Newest key is the first row (created_at desc); it has a Revoke action.
  const row = page.locator("tbody tr", { hasText: "e2e-key" }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Revoke" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Revoke", exact: true }).click();
  await expect(row.getByText("revoked")).toBeVisible();
});
