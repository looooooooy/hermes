import { expect, test, type Page } from "@playwright/test";

test.describe("Hermes Web reference layout", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("keeps approval actions and composer fully visible in the first phone viewport", async ({ page }) => {
    const consoleErrors = captureConsoleErrors(page);
    await page.goto("/");

    await expect(page.getByRole("region", { name: "Conversation" })).toBeVisible();
    await expect(page.locator(".process-rail, .process-node")).toHaveCount(0);
    await expect(page.getByRole("tablist")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Approve" })).toBeInViewport();
    await expect(page.getByRole("button", { name: "Deny" })).toBeInViewport();
    await expect(page.getByRole("form", { name: "Message Hermes" })).toBeInViewport();
    await expectNoViewportOverflow(page);

    const approval = await page.getByRole("region", { name: "Input required · Approval" }).boundingBox();
    const composer = await page.getByRole("form", { name: "Message Hermes" }).boundingBox();
    expect(approval).not.toBeNull();
    expect(composer).not.toBeNull();
    expect(approval!.y + approval!.height).toBeLessThanOrEqual(composer!.y);
    expect(composer!.y + composer!.height).toBeLessThanOrEqual(844);
    await page.screenshot({ path: "docs/qa/mobile-conversation.png", fullPage: true });
    expect(consoleErrors).toEqual([]);
  });

  test("matches reference navigation: only Subagents owns the two peer tabs", async ({ page }) => {
    const consoleErrors = captureConsoleErrors(page);
    await page.goto("/");
    await page.getByRole("button", { name: "Open menu" }).click();
    await page.getByRole("button", { name: "Open Subagents" }).click();
    const tabs = page.getByRole("tablist", { name: "Session views" }).getByRole("tab");
    await expect(tabs).toHaveText(["Conversation", "Subagents"]);
    const tabGeometry = await page.getByRole("tablist", { name: "Session views" }).evaluate((tablist) => {
      const bounds = tablist.getBoundingClientRect();
      const style = getComputedStyle(tablist);
      const tabBounds = Array.from(tablist.querySelectorAll<HTMLElement>("[role=tab]"))
        .map((tab) => tab.getBoundingClientRect());
      return {
        availableWidth: bounds.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
        occupiedWidth: tabBounds.reduce((total, tab) => total + tab.width, 0),
        widths: tabBounds.map((tab) => tab.width),
      };
    });
    expect(tabGeometry.occupiedWidth).toBeCloseTo(tabGeometry.availableWidth, 0);
    expect(tabGeometry.widths[0]).toBeCloseTo(tabGeometry.widths[1]!, 0);
    const stableFrames = await waitForStableSubagentsScreenshot(page);
    expect(stableFrames).toBeGreaterThanOrEqual(2);
    await expect(page.getByRole("region", { name: "Subagent orchestration" })).toBeVisible();
    await expectNoViewportOverflow(page);
    await captureAfterCompositorSync(page, "docs/qa/mobile-subagents.png");

    await page.getByRole("button", { name: "Open menu" }).click();
    await page.getByRole("button", { name: "Open Long conversation" }).click();
    await expect(page.getByRole("tablist")).toHaveCount(0);
    await expect(page.getByRole("navigation", { name: "Long conversation navigation" })).toBeVisible();
    await expectNoViewportOverflow(page);
    await captureAfterCompositorSync(page, "docs/qa/mobile-long-conversation.png");
    expect(consoleErrors).toEqual([]);
  });
});

test("has no horizontal overflow at 1440px", async ({ page }) => {
  const consoleErrors = captureConsoleErrors(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Conversation" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Deny" })).toBeVisible();
  await expectNoViewportOverflow(page);
  await page.screenshot({ path: "docs/qa/desktop-conversation.png", fullPage: true });
  expect(consoleErrors).toEqual([]);
});

async function waitForStableSubagentsScreenshot(page: Page): Promise<number> {
  const requiredContent = [
    page.getByText("Hermes Web", { exact: true }),
    page.getByText("Controller", { exact: true }),
    page.getByText("session · mobile-56f3", { exact: true }),
    page.getByText("5G", { exact: true }),
    page.getByRole("region", { name: "Subagent orchestration" }),
  ];
  for (const locator of requiredContent) await expect(locator).toBeVisible();
  await page.evaluate(async () => {
    await document.fonts.ready;
  });

  const layoutSignature = async () => JSON.stringify(
    await Promise.all(requiredContent.map((locator) => locator.boundingBox())),
  );
  let previous = await layoutSignature();
  let stableFrames = 0;
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => resolve());
    }));
    const current = await layoutSignature();
    stableFrames = current === previous ? stableFrames + 1 : 0;
    if (stableFrames >= 2) return stableFrames;
    previous = current;
  }
  throw new Error("Subagents screenshot layout did not stabilize across two animation frames");
}

function captureConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  return errors;
}

async function expectNoViewportOverflow(page: Page) {
  const dimensions = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
}

async function captureAfterCompositorSync(page: Page, path: string) {
  await page.screenshot({ fullPage: false });
  await page.waitForTimeout(300);
  await page.screenshot({ fullPage: false });
  await page.waitForTimeout(100);
  await page.screenshot({ path, fullPage: true });
}
