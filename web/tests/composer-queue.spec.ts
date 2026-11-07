import { expect, test } from "@playwright/test";

const url = "/tests/composer-queue.html";
for (const side of [false, true]) {
  for (const engine of ["claude", "codex", "dsh"]) {
    test(`${engine} ${side ? "side" : "main"} composer queues only this Tab submission`, async ({ page }) => {
      await page.goto(`${url}?engine=${engine}${side ? "&side=1" : ""}`);
      const input = page.locator("textarea");
      const events = page.getByTestId("events");
      await expect(page.locator(".seg, .btw-runbar, .runbar")).toHaveCount(0);
      await input.fill("next task");
      await input.press("Tab");
      await expect(input).toHaveValue("");
      await expect(input).toBeFocused();
      await expect(events).toHaveText(JSON.stringify([{ kind: "queue", prompt: "next task" }]));
      await expect(page.locator(".qchip")).toContainText("next task");
      await input.fill("clarification");
      await input.press("Enter");
      await expect(events).toHaveText(JSON.stringify([
        { kind: "queue", prompt: "next task" }, { kind: "steer", prompt: "clarification" },
      ]));
      await input.fill("button clarification");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await expect(events).toContainText('"kind":"steer","prompt":"button clarification"');
      await input.press("Enter");
      await expect(events).not.toContainText('"kind":"stop"');
      await page.getByRole("button", { name: "停止", exact: true }).click();
      await expect(events).toContainText('"kind":"stop"');
    });
  }

  test(`${side ? "side" : "main"} composer preserves focus keys, IME and unsent drafts`, async ({ page }) => {
    await page.goto(`${url}?engine=claude${side ? "&side=1" : ""}`);
    const input = page.locator("textarea");
    const events = page.getByTestId("events");
    await input.fill("草稿");
    for (const key of ["Shift+Tab", "Control+Tab", "Alt+Tab", "Meta+Tab"]) {
      await input.press(key);
      await expect(events).toHaveText("[]");
    }
    await input.focus();
    await input.dispatchEvent("compositionstart");
    await input.press("Tab");
    await expect(events).toHaveText("[]");
    await input.dispatchEvent("compositionend");
    await input.dispatchEvent("keydown", { key: "Tab", keyCode: 229 });
    await input.dispatchEvent("keydown", { key: "Tab", isComposing: true });
    await input.dispatchEvent("keydown", { key: "Tab", repeat: true });
    await expect(events).toHaveText("[]");
    await expect(input).toHaveValue("草稿");
    await page.getByTestId("state").evaluate(button => (button as HTMLButtonElement).click());
    await input.press("Tab");
    await expect(events).toHaveText("[]");
    await expect(input).not.toBeFocused();
    await input.press("Enter");
    await expect(events).toContainText('"kind":"send"');
  });

  test(`${side ? "side" : "main"} composer keeps a rejected queue draft and queues attachments`, async ({ page }) => {
    const surface = side ? "&side=1" : "";
    await page.goto(`${url}?reject=1${surface}`);
    const input = page.locator("textarea");
    await input.fill("retain me");
    await input.press("Tab");
    await expect(input).toHaveValue("retain me");
    await expect(page.getByTestId("events")).toHaveText("[]");
    await page.goto(`${url}?full=1${surface}`);
    await input.fill("queue is full");
    await input.press("Tab");
    await expect(input).toHaveValue("queue is full");
    await expect(page.getByText(/排队已满/)).toBeVisible();
    await expect(page.getByTestId("events")).toHaveText("[]");
    await page.goto(`${url}?attachment=1${surface}`);
    await input.press("Tab");
    await expect(page.getByTestId("events")).toContainText('"kind":"queue"');
    await expect(page.locator(".qchip")).toHaveCount(1);
  });
}

test("main composer gives command and skill completion priority over Tab queueing", async ({ page }) => {
  await page.goto(`${url}?engine=codex`);
  const input = page.locator("textarea");
  await input.fill("/mod");
  await input.press("Tab");
  await expect(input).toHaveValue("/model ");
  await expect(page.getByTestId("events")).toHaveText("[]");
  await input.press("Tab");
  await expect(page.getByTestId("events")).toHaveText("[]");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await input.fill("$rev");
  await input.press("Tab");
  await expect(input).toHaveValue("$review ");
  await input.press("Tab");
  await expect(page.getByTestId("events")).toHaveText(JSON.stringify([{ kind: "queue", prompt: "$review" }]));
});

for (const engine of ["claude", "codex"] as const) {
  test(`${engine} Work permission selection uses native state and keeps the draft`, async ({ page }) => {
    await page.goto(`${url}?engine=${engine}&work=1`);
    await page.locator("textarea").fill("keep this draft");
    await page.getByText("工作设置", { exact: true }).click();
    await page.getByRole("button", { name: engine === "codex" ? /权限与执行环境/ : /权限模式/ }).click();
    const choice = engine === "codex" ? "Full Access" : "Bypass Permissions";
    await page.getByRole("dialog").getByRole("button", { name: new RegExp(`^${choice}`) }).click();
    if (engine === "codex") await page.keyboard.press("Escape");
    await expect(page.locator(".work-settings-pop")).toContainText(choice);
    await expect(page.locator("textarea")).toHaveValue("keep this draft");
    await page.getByText("工作设置", { exact: true }).click();
    await page.getByRole("button", { name: engine === "codex" ? /权限与执行环境/ : /权限模式/ }).click();
    const reset = engine === "codex" ? "Work 私有工作区" : "Accept Edits";
    await page.getByRole("dialog").getByRole("button", { name: new RegExp(`^${reset}`) }).click();
    if (engine === "codex") await page.keyboard.press("Escape");
    await expect(page.locator(".work-settings-pop")).toContainText(reset);
  });

  test(`${engine} Work permission choice is included in the first submission`, async ({ page }) => {
    await page.goto(`${url}?engine=${engine}&work=1&new=1`);
    await page.locator(".newchat-access").click();
    const choice = engine === "codex" ? "Full Access" : "Bypass Permissions";
    await page.getByRole("dialog").getByRole("button", { name: new RegExp(`^${choice}`) }).click();
    if (engine === "codex") await page.keyboard.press("Escape");
    await page.locator("textarea").fill("first task");
    await page.getByRole("button", { name: "开始", exact: true }).click();
    await expect(page.getByTestId("events")).toContainText(engine === "codex" ? ":danger-full-access" : "bypassPermissions");
    await expect(page.getByTestId("events")).toContainText("first task");
  });
}

test("Work permission selection waits for native confirmation", async ({ page }) => {
  await page.goto(`${url}?engine=claude&work=1&reject-control=1`);
  await page.getByText("工作设置", { exact: true }).click();
  await page.getByRole("button", { name: /权限模式/ }).click();
  await page.getByRole("dialog").getByRole("button", { name: /^Bypass Permissions/ }).click();
  await expect(page.getByTestId("events")).toContainText("bypassPermissions");
  await expect(page.locator(".work-settings-pop")).toContainText("Accept Edits");
});
