import { expect, test } from "@playwright/test";

const url = "/tests/composer-queue.html";

for (const fresh of [false, true]) {
  test(`voice streams to the ${fresh ? "new" : "existing"} chat draft before the desktop click or mobile release`, async ({ page }, testInfo) => {
    const mobile = testInfo.project.use.hasTouch === true;
    let chunks = 0;
    await page.routeWebSocket("**/v1/realtime?intent=transcription", (ws) => {
      ws.onMessage((data) => {
        const event = JSON.parse(String(data));
        if (event.type === "session.update") ws.send(JSON.stringify({ type: "session.updated" }));
        if (event.type === "input_audio_buffer.append" && ++chunks === 1) {
          const audio = Buffer.from(event.audio, "base64");
          expect(audio.byteLength).toBe(4800);
          expect(audio.some(sample => sample !== 0)).toBe(true);
          ws.send(JSON.stringify({
            type: "conversation.item.input_audio_transcription.delta", transcript: "实时中间结果",
          }));
        }
        if (event.type === "input_audio_buffer.commit") ws.send(JSON.stringify({
          type: "conversation.item.input_audio_transcription.completed", transcript: "最终转写草稿",
        }));
      });
    });
    await page.goto(`${url}?voice=1${fresh ? "&new=1" : ""}`);
    await page.evaluate((nativeHaptics) => {
      const stats: { opened: number; track?: MediaStreamTrack; haptics: (number | string)[] } =
        { opened: 0, haptics: [] };
      Object.assign(window, { voiceTest: stats });
      Object.defineProperty(navigator, "vibrate", { value: (duration: number) => {
        stats.haptics.push(duration);
        return true;
      } });
      if (nativeHaptics) {
        window.ccRemoteHarmonyRecordingHaptic = () => { stats.haptics.push("harmony"); };
      }
      let syntheticMic: MediaStream | null = null;
      document.addEventListener("click", () => {
        const context = new AudioContext();
        const microphone = context.createMediaStreamDestination();
        const tone = context.createOscillator();
        tone.connect(microphone);
        tone.start();
        void context.resume();
        syntheticMic = microphone.stream;
      }, { once: true, capture: true });
      Object.defineProperty(Object.getPrototypeOf(navigator.mediaDevices), "getUserMedia", { value: async () => {
        stats.opened++;
        if (!syntheticMic) throw new Error("missing synthetic microphone");
        stats.track = syntheticMic.getAudioTracks()[0];
        return syntheticMic;
      } });
    }, mobile && fresh);
    const mic = page.locator(".voice-input");
    const input = page.locator("textarea");
    const stats = () => page.evaluate(() => {
      const stats = (window as Window & {
        voiceTest: { opened: number; track?: MediaStreamTrack; haptics: (number | string)[] };
      }).voiceTest;
      return {
        opened: stats.opened, stopped: Number(stats.track?.readyState === "ended"), haptics: stats.haptics,
      };
    });
    if (mobile) {
      await expect(mic).toHaveAccessibleName("按住说话");
      await mic.click();
      expect(await stats()).toEqual({ opened: 0, stopped: 0, haptics: [] });
      await expect(page.locator(".voice-input-status")).toHaveCount(0);
      const box = (await mic.boundingBox())!;
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
    } else {
      await expect(mic).toHaveAccessibleName("开始语音转写");
      await mic.click();
    }
    await expect.poll(async () => (await stats()).opened).toBe(1);
    await expect(input).toHaveValue("实时中间结果");
    await expect(mic).toHaveClass(/voice-input-held/);
    await expect(page.locator(".voice-input-status")).toHaveText("正在录音");
    await expect(page.locator(".voice-input-status")).toBeVisible();
    await expect(page.getByTestId("events")).toHaveText("[]");
    if (mobile) {
      await page.mouse.up();
    } else {
      await expect(mic).toHaveAccessibleName("停止语音转写");
      await mic.click();
    }
    await expect(input).toHaveValue("最终转写草稿");
    await expect(page.locator(".voice-input-status")).toHaveCount(0);
    await expect(mic).toHaveCount(0);
    await expect(page.getByRole("button", { name: fresh ? "开始" : "发送", exact: true })).toBeEnabled();
    await expect(page.getByTestId("events")).toHaveText("[]");
    expect(await stats()).toEqual({ opened: 1, stopped: 1, haptics: mobile ? [fresh ? "harmony" : 25] : [] });
  });
}

test("voice desktop stop cancels a pending microphone request", async ({ page }, testInfo) => {
  test.skip(testInfo.project.use.hasTouch === true, "Desktop click behavior");
  let connections = 0;
  await page.routeWebSocket("**/v1/realtime?intent=transcription", () => { connections++; });
  await page.goto(`${url}?voice=1`);
  await page.evaluate(() => {
    const pending = { grant: () => {}, track: null as MediaStreamTrack | null };
    Object.assign(window, { pendingVoiceTest: pending });
    Object.defineProperty(Object.getPrototypeOf(navigator.mediaDevices), "getUserMedia", {
      value: () => new Promise<MediaStream>((resolve) => {
        const context = new AudioContext();
        const stream = context.createMediaStreamDestination().stream;
        pending.track = stream.getAudioTracks()[0];
        pending.grant = () => {
          resolve(stream);
          void context.close();
        };
      }),
    });
  });
  const mic = page.locator(".voice-input");
  await mic.press("Enter");
  await expect(mic).toHaveAccessibleName("停止语音转写");
  await expect(page.locator(".voice-input-status")).toHaveText("正在打开麦克风");
  await mic.press("Enter");
  await expect(mic).toHaveAccessibleName("开始语音转写");
  const state = await page.evaluate(async () => {
    const pending = (window as Window & {
      pendingVoiceTest: { grant: () => void; track: MediaStreamTrack };
    }).pendingVoiceTest;
    pending.grant();
    await Promise.resolve();
    return pending.track.readyState;
  });
  expect(state).toBe("ended");
  expect(connections).toBe(0);
});

test("voice narration only requests the selected completed reply", async ({ page }) => {
  const requests: string[] = [];
  await page.route("**/v1/audio/speech", async (route) => {
    requests.push(route.request().postDataJSON().input);
    await route.fulfill({ status: 200, contentType: "audio/pcm", body: Buffer.alloc(24000) });
  });
  await page.goto(`${url}?reply=1`);
  const read = page.getByRole("button", { name: "朗读回复" });
  await expect(read).toBeVisible();
  expect(requests).toEqual([]);
  await read.click();
  const stop = page.getByRole("button", { name: "停止朗读" });
  await expect(stop).toBeVisible();
  await expect.poll(() => requests).toEqual(["你好，测试朗读。"]);
  await stop.click();
  await expect(read).toBeVisible();
  await read.click();
  await expect.poll(() => requests.length).toBe(2);
  await expect(read).toBeVisible();
});
test("new-chat controls retain private draft and attachments across login remount", async ({ page }) => {
  await page.goto(`${url}?engine=claude&new=1&attachment=1`);
  const input = page.locator("textarea");
  await input.fill("更新期间保留的草稿");
  await page.getByTestId("auth-remount").click();
  await expect(input).toHaveCount(0);
  await page.getByTestId("auth-remount").click();
  await expect(input).toHaveValue("更新期间保留的草稿");
  await expect(page.locator(".attach-file")).toContainText("notes.txt");
  await input.fill("/clear");
  await input.press("Enter");
  await page.getByTestId("auth-remount").click();
  await page.getByTestId("auth-remount").click();
  await expect(input).toHaveValue("");
  await expect(page.locator(".attach-file")).toHaveCount(0);
  await expect(page.getByTestId("events")).toHaveText("[]");
});
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
