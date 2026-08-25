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

test("scale deployment group up and down, then delete it", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);

  // Seed a single-replica deployment first.
  await page.getByRole("link", { name: "Models" }).click();
  await page.getByTestId("model-card-Qwen/Qwen2.5-0.5B-Instruct").getByRole("button", { name: "Deploy" }).click();
  const dialog = page.getByTestId("deploy-dialog");
  await expect(page.getByRole("heading", { name: /Deploy .+/ })).toBeVisible();
  await dialog.getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });

  const group = page.locator('tbody[data-testid^="deployment-group-"]', { hasText: MODEL });
  const badge = group.locator("tr").first().getByText(/^\d+\/\d+ running$/);
  const rows = group.locator('tr[data-testid^="instance-row-"]');
  await expect(rows).toHaveCount(1);
  await expect(badge).toHaveText("1/1 running", { timeout: 90000 });

  // Scale up to 3 replicas: the button steps min_replicas by +1 per click.
  await group.getByRole("button", { name: "Scale up" }).click();
  await expect(badge).toHaveText("2/2 running", { timeout: 90000 });
  await group.getByRole("button", { name: "Scale up" }).click();
  await expect(badge).toHaveText("3/3 running", { timeout: 90000 });

  // Scale down to 2: one replica is stopped (row stays listed), badge shows ready/min.
  await group.getByRole("button", { name: "Scale down" }).click();
  await expect(badge).toHaveText("2/2 running", { timeout: 90000 });
  await expect(rows.filter({ hasText: "stopped" })).toHaveCount(1, { timeout: 30000 });

  // Delete the group: confirm dialog clears every row and shows the empty state.
  await group.getByRole("button", { name: "Delete group" }).click();
  await page.locator("div.fixed.inset-0").getByRole("button", { name: "Delete", exact: true }).click();
  await expect(group).toHaveCount(0);
  await expect(
    page.getByText("No instances yet — deploy a model from the catalog."),
  ).toBeVisible();
});
