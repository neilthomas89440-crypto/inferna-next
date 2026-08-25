import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const MODEL = "Qwen2.5 0.5B Instruct";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

/** Wipe every deployment group via the API so specs stay isolated across projects. */
async function clearDeployments(request: APIRequestContext) {
  const authResponse = await request.post("/api/v1/auth/login", {
    data: { username: "admin", password: "inferna" },
  });
  const { access_token } = (await authResponse.json()) as { access_token: string };
  const headers = { Authorization: `Bearer ${access_token}` };
  const list = await request.get("/api/v1/deployments", { headers });
  for (const deployment of (await list.json()) as Array<{ id: string }>) {
    await request.delete(`/api/v1/deployments/${deployment.id}`, { headers });
  }
}

test.beforeEach(async ({ request }) => {
  await clearDeployments(request);
});

test.afterEach(async ({ request }) => {
  await clearDeployments(request);
});

test("deploy with 2 replicas creates a running group", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);

  await page.getByRole("link", { name: "Models" }).click();
  await page.getByTestId("model-card-Qwen/Qwen2.5-0.5B-Instruct").getByRole("button", { name: "Deploy" }).click();
  const dialog = page.getByTestId("deploy-dialog");
  await expect(page.getByRole("heading", { name: /Deploy .+/ })).toBeVisible();

  // The Replicas field is only offered in auto placement mode.
  const replicas = page.getByLabel("Replicas");
  await expect(replicas).toBeVisible();
  await dialog.getByRole("radio", { name: "Manual" }).check();
  await expect(replicas).toHaveCount(0);
  await dialog.getByRole("radio", { name: "Auto (best fit)" }).check();
  await expect(replicas).toBeVisible();
  await replicas.fill("2");

  await dialog.getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });
  await expect(page.getByRole("heading", { name: "Instances" })).toBeVisible();

  const group = page.locator('tbody[data-testid^="deployment-group-"]', { hasText: MODEL });
  const rows = group.locator('tr[data-testid^="instance-row-"]');
  await expect(rows).toHaveCount(2, { timeout: 30000 });

  const badge = group.locator("tr").first().getByText(/^\d+\/\d+ running$/);
  await expect(badge).toHaveText("2/2 running", { timeout: 90000 });
});
