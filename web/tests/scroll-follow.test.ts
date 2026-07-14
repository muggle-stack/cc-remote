import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  AT_BOTTOM_PX,
  anchoredScrollTop,
  createFrameCoalescer,
  measureBottom,
  NEAR_BOTTOM_PX,
  ScrollFollowController,
} from "../src/scroll-follow.ts";

assert.deepEqual(measureBottom({
  scrollHeight: 1_000,
  scrollTop: 320,
  clientHeight: 600,
}), {
  distance: NEAR_BOTTOM_PX,
  atBottom: false,
  nearBottom: true,
});
assert.equal(measureBottom({
  scrollHeight: 1_000,
  scrollTop: 398,
  clientHeight: 600,
}).atBottom, true);
assert.equal(AT_BOTTOM_PX, 2);
assert.equal(anchoredScrollTop(170, 1_000, 1_700), 870);
assert.equal(anchoredScrollTop(0, 1_000, 800), 0);

const controller = new ScrollFollowController();
assert.deepEqual(controller.reset({
  scrollHeight: 1_000,
  scrollTop: 400,
  clientHeight: 600,
}), { followOutput: true, nearBottom: true });

// An upward wheel/touch intent pauses even while still geometrically near the
// bottom. A layout update must not silently turn following back on.
assert.deepEqual(controller.pause({
  scrollHeight: 1_000,
  scrollTop: 350,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });
assert.deepEqual(controller.observeLayout({
  scrollHeight: 1_200,
  scrollTop: 350,
  clientHeight: 600,
}), { followOutput: false, nearBottom: false });

// Scrolling close is not enough; reaching the real bottom is the deliberate
// gesture that resumes live output following.
assert.deepEqual(controller.observeScroll({
  scrollHeight: 1_200,
  scrollTop: 580,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });
assert.deepEqual(controller.observeScroll({
  scrollHeight: 1_200,
  scrollTop: 598,
  clientHeight: 600,
}), { followOutput: true, nearBottom: true });

// Moving toward history pauses immediately even if the new position remains
// inside the 80px near-bottom range.
assert.deepEqual(controller.observeScroll({
  scrollHeight: 1_200,
  scrollTop: 570,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });

// Hiding an in-flow bottom control can shrink scrollHeight and make the
// browser clamp scrollTop by the same amount. The viewport is still at the
// real bottom, so that geometry-only movement must not be treated as an
// upward user scroll and restart the show/hide feedback loop.
const layoutClampController = new ScrollFollowController();
layoutClampController.pause({
  scrollHeight: 1_040,
  scrollTop: 400,
  clientHeight: 600,
});
assert.deepEqual(layoutClampController.observeScroll({
  scrollHeight: 1_040,
  scrollTop: 440,
  clientHeight: 600,
}), { followOutput: true, nearBottom: true });
assert.deepEqual(layoutClampController.observeScroll({
  scrollHeight: 1_000,
  scrollTop: 400,
  clientHeight: 600,
}), { followOutput: true, nearBottom: true });

// A process card can collapse while the user is reading history. Reaching the
// bottom because the layout shrank is not a deliberate downward gesture, no
// matter whether ResizeObserver or the browser's scroll event arrives first.
const shrinkLayoutFirst = new ScrollFollowController();
shrinkLayoutFirst.pause({
  scrollHeight: 1_200,
  scrollTop: 500,
  clientHeight: 600,
});
assert.deepEqual(shrinkLayoutFirst.observeLayout({
  scrollHeight: 1_000,
  scrollTop: 400,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });
assert.deepEqual(shrinkLayoutFirst.observeScroll({
  scrollHeight: 1_000,
  scrollTop: 400,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });

const shrinkScrollFirst = new ScrollFollowController();
shrinkScrollFirst.pause({
  scrollHeight: 1_200,
  scrollTop: 500,
  clientHeight: 600,
});
assert.deepEqual(shrinkScrollFirst.observeScroll({
  scrollHeight: 1_000,
  scrollTop: 400,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });
assert.deepEqual(shrinkScrollFirst.observeLayout({
  scrollHeight: 1_000,
  scrollTop: 400,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });

// History anchoring writes scrollTop programmatically and must preserve the
// paused intent. A session reset intentionally restores following.
assert.deepEqual(controller.recordProgrammaticScroll({
  scrollHeight: 1_500,
  scrollTop: 870,
  clientHeight: 600,
}), { followOutput: false, nearBottom: true });
assert.deepEqual(controller.reset({
  scrollHeight: 2_000,
  scrollTop: 1_400,
  clientHeight: 600,
}), { followOutput: true, nearBottom: true });

const queuedFrames = new Map<number, () => void>();
const cancelledFrames: number[] = [];
let nextFrameId = 1;
const coalescer = createFrameCoalescer(
  (callback) => {
    const id = nextFrameId++;
    queuedFrames.set(id, callback);
    return id;
  },
  (id) => { cancelledFrames.push(id); },
);
const frameRuns: string[] = [];
coalescer.schedule(() => frameRuns.push("stale"));
coalescer.schedule(() => frameRuns.push("latest"));
assert.equal(queuedFrames.size, 1);
queuedFrames.get(1)?.();
assert.deepEqual(frameRuns, ["latest"]);

coalescer.schedule(() => frameRuns.push("cancelled"));
coalescer.cancel();
assert.deepEqual(cancelledFrames, [2]);
queuedFrames.get(2)?.();
assert.deepEqual(frameRuns, ["latest"]);

// Guard the visual regression without requiring a browser test dependency:
// the header decoration must remain inside the header and must not blur the
// reconnect banner or first thread row beneath it.
const css = readFileSync(
  new URL("../../../../src/index.css", import.meta.url),
  "utf8",
);
const headerRule = css.match(/\.c-head\{[^}]+\}/)?.[0] ?? "";
assert.match(headerRule, /overflow:hidden/);
const headerDecoration = css.match(/\.c-head::after\{[^}]+\}/)?.[0] ?? "";
assert.match(headerDecoration, /bottom:0/);
assert.doesNotMatch(headerDecoration, /top:100%/);
assert.doesNotMatch(headerDecoration, /backdrop-filter/);
const bannerRule = css.match(/\.banner\{[^}]+\}/)?.[0] ?? "";
assert.match(bannerRule, /position:relative/);
assert.match(bannerRule, /color:var\(--text\)/);
assert.match(bannerRule, /safe-area-inset-left/);

// The bottom control must overlay the scroll viewport instead of participating
// in its content flow. Conditional visibility may then never change
// scrollHeight and feed another synthetic scroll back into the controller.
const chatViewSource = readFileSync(
  new URL("../../../../src/components/ChatView.tsx", import.meta.url),
  "utf8",
);
assert.match(chatViewSource, /className="thread-shell"/);
const threadShellRule = css.match(/\.thread-shell\{[^}]+\}/)?.[0] ?? "";
assert.match(threadShellRule, /position:relative/);
const scrollBottomRule = css.match(/\.scroll-bottom-wrap\{[^}]+\}/)?.[0] ?? "";
assert.match(scrollBottomRule, /position:absolute/);
assert.doesNotMatch(scrollBottomRule, /position:sticky/);

console.log("scroll follow tests passed");
