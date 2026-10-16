import { expect, test, type Page } from "@playwright/test";

// Multi-node deploys need one host per rank. The default e2e stack runs a single worker, so the
// happy path is gated behind INFERNA_E2E_MULTINODE=1 (bring up docker-compose.multinode.yml); the
// error path is what CI can always assert, and it is the one that tells an operator what to fix.
const MULTI_NODE_STACK = process.env.INFERNA_E2E_MULTINODE === "1";
const MODEL = "Qwen/Qwen2.5-0.5B-Instruct";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("admin");
  await page.getByLabel("Password").fill("inferna");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
}

async function openDeployDialog(page: Page) {
  await page.getByRole("link", { name: "Models" }).click();
  await page.getByTestId(`model-card-${MODEL}`).getByRole("button", { name: "Deploy" }).click();
  await expect(page.getByTestId("deploy-dialog")).toBeVisible();
}

test("multi-node deploy asks for the host count and the parallel mode", async ({ page }) => {
  await login(page);
  await openDeployDialog(page);

  // The mode selector only appears once more than one host is involved.
  await expect(page.getByRole("radio", { name: "pp" })).toHaveCount(0);
  await page.getByLabel(/Nodes per replica/).fill("2");
  await expect(page.getByRole("radio", { name: "pp" })).toBeVisible();
  await expect(page.getByRole("radio", { name: "tp" })).toBeVisible();
});

test("a stack without enough hosts explains itself instead of failing silently", async ({ page }) => {
  test.skip(MULTI_NODE_STACK, "the multinode stack is up: the happy path runs below");
  test.setTimeout(60_000);
  await login(page);
  await openDeployDialog(page);

  await page.getByLabel(/Nodes per replica/).fill("2");
  await page.getByTestId("deploy-dialog").getByRole("button", { name: "Deploy", exact: true }).click();

  // The server refuses with a message naming the requirement; the dialog must show it verbatim
  // rather than closing and leaving the user guessing.
  await expect(page.getByTestId("deploy-dialog")).toContainText(/needs 2 hosts|routable address/);
});

test("two ranks appear as one group, with an endpoint only on the head", async ({ page }) => {
  test.skip(!MULTI_NODE_STACK, "needs docker-compose.multinode.yml (three workers)");
  test.setTimeout(180_000);
  await login(page);
  await openDeployDialog(page);

  await page.getByLabel(/Nodes per replica/).fill("2");
  await page.getByRole("radio", { name: "pp" }).check();
  await page.getByTestId("deploy-dialog").getByRole("button", { name: "Deploy", exact: true }).click();
  await expect(page).toHaveURL(/\/instances$/, { timeout: 30000 });

  const rows = page.locator('tr[data-testid^="instance-row-"]');
  await expect(rows).toHaveCount(2);
  await expect(page.getByText("head / 2")).toBeVisible();
  await expect(page.getByText("rank 1 / 2")).toBeVisible();
  await expect(page.getByText("→ ")).toBeVisible(); // the follower shows its rendezvous point
  // Exactly one endpoint control: the head's.
  await expect(page.getByRole("button", { name: "Endpoint" })).toHaveCount(1);

  await expect(rows.first().getByText("running", { exact: true })).toBeVisible({ timeout: 120000 });

  // Cleanup: delete every rank so the serialized specs start from the same state.
  while ((await rows.count()) > 0) {
    await rows.first().getByRole("button", { name: "Delete" }).click();
    await page.locator("div.fixed.inset-0").getByRole("button", { name: /delete/i }).click();
    await expect(rows).toHaveCount((await rows.count()) - 1);
  }
});