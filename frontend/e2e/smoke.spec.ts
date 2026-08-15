import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

test("dashboard stats render", async ({ page }) => {
  await login(page);
  await expect(page.getByText("Clusters")).toBeVisible();
  await expect(page.getByText("Workers online")).toBeVisible();
  await expect(page.getByText("GPUs total")).toBeVisible();
  await expect(page.getByText("Instances running")).toBeVisible();
});

test("models page lists catalog and deploy dialog cancels", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "Models" }).click();
  await expect(page.getByRole("heading", { name: "Model catalog" })).toBeVisible();
  await expect(page.getByText("Qwen2.5 0.5B Instruct")).toBeVisible();
  await expect(page.getByText("Whisper Large v3")).toBeVisible();

  await page.getByRole("button", { name: "Deploy" }).first().click();
  await expect(page.getByRole("heading", { name: /Deploy .+/ })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("heading", { name: /Deploy .+/ })).toHaveCount(0);
});
