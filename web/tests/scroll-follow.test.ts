import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  AUTO_LOAD_HISTORY_TOP_PX,
  AT_BOTTOM_PX,
  createFrameCoalescer,
  HISTORY_ANCHOR_EPSILON_PX,
  HistoryAnchorController,
  HistoryPageActivityController,
  historyPageStatus,
  isAtHistoryEdge,
  isAtLatestEdge,
  OlderHistoryLoadGate,
  measureBottom,
  NEAR_BOTTOM_PX,
  shouldAutoLoadOlderHistory,
  shouldAutoLoadNewerHistory,
  ScrollFollowController,
} from "../src/scroll-follow.ts";
import { ScrollCoordinator } from "../src/scroll-coordinator.ts";
import { updateTurnKeySnapshot } from "../src/virtual-turn-keys.ts";

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
assert.equal(HISTORY_ANCHOR_EPSILON_PX, 0.5);
assert.equal(isAtHistoryEdge({ scrollHeight: 1_200, scrollTop: 0, clientHeight: 600}), true);
assert.equal(isAtHistoryEdge({ scrollHeight: 1_200, scrollTop: 2, clientHeight: 600}), false,
  "wheel/touch preflight must wait for the browser to reach the real top");
assert.equal(isAtLatestEdge({
  scrollHeight: 1_200, scrollTop: 600, clientHeight: 600,
}), true);
assert.equal(isAtLatestEdge({
  scrollHeight: 1_200, scrollTop: 598, clientHeight: 600,
}), false, "cached-newer preflight must wait for the real bottom");

const firstTurnKeySnapshot = updateTurnKeySnapshot(null, [
  { id: "old-1" },
  { id: "old-2" },
], "session-a");
const streamedTurns = [
  { id: "old-1", text: "streamed content" },
  { id: "old-2" },
];
const contentOnlyTurnKeySnapshot = updateTurnKeySnapshot(
  firstTurnKeySnapshot,
  streamedTurns,
  "session-a",
);
assert.equal(contentOnlyTurnKeySnapshot, firstTurnKeySnapshot,
  "streaming content with unchanged turn ids must keep the virtualizer key extractor stable");
const prependedTurnKeySnapshot = updateTurnKeySnapshot(firstTurnKeySnapshot, [
  { id: "new-1" },
  { id: "new-2" },
  { id: "old-1" },
  { id: "old-2" },
], "session-a");
assert.notEqual(prependedTurnKeySnapshot, firstTurnKeySnapshot);
assert.equal(firstTurnKeySnapshot.getItemKey(0), "session-a\u0000old-1",
  "the previous virtualizer options must retain their old key snapshot");
assert.equal(prependedTurnKeySnapshot.getItemKey(0), "session-a\u0000new-1");
assert.equal(prependedTurnKeySnapshot.getItemKey(2), "session-a\u0000old-1");
const switchedTurnKeySnapshot = updateTurnKeySnapshot(
  prependedTurnKeySnapshot,
  [{ id: "new-1" }, { id: "new-2" }, { id: "old-1" }, { id: "old-2" }],
  "session-b",
);
assert.notEqual(switchedTurnKeySnapshot, prependedTurnKeySnapshot);
assert.equal(switchedTurnKeySnapshot.getItemKey(0), "session-b\u0000new-1",
  "the same turn id in another session must never reuse a measured height");

const scrollCoordinator = new ScrollCoordinator();
assert.deepEqual(scrollCoordinator.policy(true), {
  anchorTo: "end",
  followOnAppend: "auto",
  allowResizeAdjustment: true,
});
const processInteraction = scrollCoordinator.beginInteraction(true);
assert.deepEqual(scrollCoordinator.policy(true), {
  anchorTo: "start",
  followOnAppend: false,
  allowResizeAdjustment: false,
});
assert.equal(scrollCoordinator.requestBottom("auto"), null,
  "automatic bottom writes are queued while a press is active");
assert.deepEqual(scrollCoordinator.endInteraction(processInteraction, true), {
  kind: "bottom",
  behavior: "auto",
});
const historyInteraction = scrollCoordinator.beginInteraction(false);
assert.equal(scrollCoordinator.endInteraction(historyInteraction, true), null,
  "a process tap while reading history must not jump back to the bottom");
assert.deepEqual(scrollCoordinator.policy(false), {
  anchorTo: "end",
  followOnAppend: false,
  allowResizeAdjustment: true,
});
assert.deepEqual(scrollCoordinator.requestOffset(120.5), {
  kind: "offset",
  offset: 120.5,
});
const detailInteraction = scrollCoordinator.beginInteraction(false);
assert.equal(scrollCoordinator.requestOffset(125), null,
  "ordinary offset writes remain blocked during detail replacement");
assert.deepEqual(scrollCoordinator.requestInteractionOffset(
  detailInteraction, 125,
), {
  kind: "offset",
  offset: 125,
});
assert.equal(scrollCoordinator.requestInteractionOffset(
  detailInteraction + 1, 125,
), null, "only the active detail transaction may correct its anchor");
assert.equal(scrollCoordinator.requestBottom("smooth"), null);
assert.equal(scrollCoordinator.endInteraction(detailInteraction, false), null,
  "paused detail reading must discard a bottom request queued while locked");
const followedInteraction = scrollCoordinator.beginInteraction(false);
assert.equal(scrollCoordinator.requestBottom("smooth"), null);
assert.deepEqual(scrollCoordinator.endInteraction(followedInteraction, true), {
  kind: "bottom",
  behavior: "smooth",
}, "a still-following viewport keeps the existing deferred-bottom behavior");

const multiPointerCoordinator = new ScrollCoordinator();
const draggedPointer = multiPointerCoordinator.beginInteraction(true);
const heldPointer = multiPointerCoordinator.beginInteraction(true);
assert.equal(multiPointerCoordinator.requestBottom("smooth"), null);
assert.equal(multiPointerCoordinator.endInteraction(draggedPointer, false), null);
assert.equal(multiPointerCoordinator.endInteraction(heldPointer, true), null,
  "one dragged/cancelled pointer prevents a later pointerup from replaying bottom");
assert.deepEqual(multiPointerCoordinator.policy(true), {
  anchorTo: "end",
  followOnAppend: "auto",
  allowResizeAdjustment: true,
}, "all pointer tokens still release after a multi-pointer cancellation");

const historyGate = new OlderHistoryLoadGate();
historyGate.beginGesture();
assert.equal(historyGate.acquire(), true);
assert.equal(historyGate.acquire(), false, "one gesture can start only one prepend");
historyGate.complete();
assert.equal(historyGate.acquire(), false,
  "finishing a page while the same finger is down must not chain another page");
historyGate.endGesture();
historyGate.beginGesture();
assert.equal(historyGate.acquire(), true, "a new pull gesture may load the next page");
historyGate.complete();
historyGate.endGesture();

const pageActivity = new HistoryPageActivityController();
const firstPageActivity = pageActivity.begin({
  direction: "older",
});
assert.equal(pageActivity.complete(firstPageActivity.key + 1), false,
  "a stale response cannot clear the current history activity");
const nextPageActivity = pageActivity.begin({
  direction: "older",
});
assert.notEqual(nextPageActivity.key, firstPageActivity.key,
  "each accepted cursor owns a distinct loading transaction");
const repeatedPageActivity = pageActivity.begin({
  direction: "older",
});
assert.notEqual(repeatedPageActivity.key, nextPageActivity.key,
  "retrying the same cursor still owns a distinct loading transaction");
assert.equal(pageActivity.complete(firstPageActivity.key), false,
  "a late first page cannot hide the next page's activity");
assert.equal(pageActivity.complete(nextPageActivity.key), false,
  "a late same-cursor page cannot hide its retry activity");
assert.equal(pageActivity.complete(repeatedPageActivity.key), true);
assert.equal(pageActivity.complete(repeatedPageActivity.key), false,
  "a completed activity cannot be cleared twice");

// Merely painting at the top (short history, session switch, or layout clamp)
// must not start pagination. A real gesture toward older content does, within
// a small top threshold, and only while another local/server page exists.
assert.equal(shouldAutoLoadOlderHistory({
  scrollHeight: 1_200,
  scrollTop: AUTO_LOAD_HISTORY_TOP_PX,
  clientHeight: 600,
}, true, true), true);
assert.equal(shouldAutoLoadOlderHistory({
  scrollHeight: 1_200,
  scrollTop: AUTO_LOAD_HISTORY_TOP_PX + 1,
  clientHeight: 600,
}, true, true), false);
assert.equal(shouldAutoLoadOlderHistory({
  scrollHeight: 1_200,
  scrollTop: 0,
  clientHeight: 600,
}, false, true), false);
assert.equal(shouldAutoLoadOlderHistory({
  scrollHeight: 1_200,
  scrollTop: 0,
  clientHeight: 600,
}, true, false), false);
assert.equal(shouldAutoLoadNewerHistory({
  scrollHeight: 1_200,
  scrollTop: 528,
  clientHeight: 600,
}, true, true), true);
assert.equal(shouldAutoLoadNewerHistory({
  scrollHeight: 1_200,
  scrollTop: 527,
  clientHeight: 600,
}, true, true), false);
assert.equal(shouldAutoLoadNewerHistory({
  scrollHeight: 1_200,
  scrollTop: 600,
  clientHeight: 600,
}, false, true), false,
  "painting a browse window at the bottom must not cascade through cached pages");

const historyAnchor = new HistoryAnchorController();
const firstAnchorGeneration = historyAnchor.begin({
  sid: "session-a",
  revision: "revision-a",
  before: "cursor-a",
  source: "server",
  anchorTurnId: "turn-10",
  oldestTurnId: "turn-10",
  anchorOffset: 18,
});
assert.equal(historyAnchor.current()?.phase, "pending");
historyAnchor.observeUserScroll(true);
assert.deepEqual(historyAnchor.current()?.anchorTurnId, "turn-10",
  "rubber-banding at the history edge keeps the frozen page boundary");
assert.equal(historyAnchor.rebasePending(firstAnchorGeneration, {
  anchorTurnId: "turn-11",
  oldestTurnId: "turn-10",
  anchorOffset: 24,
}), true);
assert.deepEqual(historyAnchor.current()?.anchorTurnId, "turn-11",
  "verified movement may rebase a response that is staged before mount");
assert.equal(historyAnchor.markApplied(firstAnchorGeneration), true);
assert.equal(historyAnchor.current()?.phase, "applied");
assert.equal(historyAnchor.rebase(firstAnchorGeneration, {
  anchorTurnId: "turn-12",
  oldestTurnId: "turn-1",
  anchorOffset: 31,
}), true);
assert.deepEqual(historyAnchor.current(), {
  sid: "session-a",
  revision: "revision-a",
  before: "cursor-a",
  source: "server",
  generation: firstAnchorGeneration,
  phase: "applied",
  anchorTurnId: "turn-12",
  oldestTurnId: "turn-1",
  anchorOffset: 31,
}, "a user scroll after the page attaches rebases the residual correction");
assert.equal(historyAnchor.rebase(firstAnchorGeneration + 1, {
  anchorTurnId: "turn-13",
  oldestTurnId: "turn-1",
  anchorOffset: 12,
}), false, "a stale gesture cannot rebase a newer transaction");
historyAnchor.cancel();

const newerAnchorGeneration = historyAnchor.begin({
  sid: "session-a",
  revision: "revision-a",
  viewId: "browse-a",
  before: null,
  windowEpoch: 7,
  direction: "newer",
  source: "local",
  anchorTurnId: "turn-18",
  oldestTurnId: "turn-1",
  anchorOffset: 22,
});
const pendingNewerPage = historyAnchor.current()!;
assert.equal(historyPageStatus(pendingNewerPage, {
  sid: "session-a", revision: "revision-a", viewId: "browse-a",
  cursor: "turn-1", hasMore: true, windowEpoch: 7, hasNewer: true,
}), "pending");
assert.equal(historyPageStatus(pendingNewerPage, {
  sid: "session-a", revision: "revision-a", viewId: "browse-a",
  cursor: "turn-9", hasMore: true, windowEpoch: 8, hasNewer: true,
}), "complete",
  "a cached-newer append/head-eviction completes only after its window epoch commits");
assert.equal(historyPageStatus(pendingNewerPage, {
  sid: "session-a", revision: "revision-a", viewId: "browse-b",
  cursor: "turn-9", hasMore: true, windowEpoch: 8, hasNewer: true,
}), "stale",
  "a delayed cached page from a retired browse view cannot move its replacement");
assert.equal(historyAnchor.markApplied(newerAnchorGeneration), true);
historyAnchor.cancel();

const abandonedAnchorGeneration = historyAnchor.begin({
  sid: "session-a",
  revision: "revision-a",
  before: "cursor-a",
  source: "server",
  anchorTurnId: "turn-10",
  oldestTurnId: "turn-10",
  anchorOffset: 18,
});
historyAnchor.observeUserScroll(false);
assert.equal(historyAnchor.current(), null,
  "leaving the history edge while a page is pending prevents a delayed jump");
assert.equal(historyAnchor.markApplied(abandonedAnchorGeneration), false);

const staleAnchorGeneration = historyAnchor.begin({
  sid: "session-a",
  revision: "revision-a",
  before: "cursor-a",
  source: "server",
  anchorTurnId: "turn-10",
  oldestTurnId: "turn-10",
  anchorOffset: 18,
});
const currentAnchorGeneration = historyAnchor.begin({
  sid: "session-a",
  revision: "revision-a",
  before: "cursor-b",
  source: "server",
  anchorTurnId: "turn-5",
  oldestTurnId: "turn-5",
  anchorOffset: 24,
});
assert.equal(historyAnchor.markApplied(staleAnchorGeneration), false,
  "a stale layout effect cannot complete a newer history request");
assert.equal(historyAnchor.current()?.generation, currentAnchorGeneration);
const pendingPage = historyAnchor.current()!;
assert.equal(historyPageStatus(pendingPage, {
  sid: "session-a", revision: "revision-a",
  cursor: "cursor-b", hasMore: true,
}), "pending");
assert.equal(historyPageStatus(pendingPage, {
  sid: "session-a", revision: "revision-a",
  cursor: "cursor-c", hasMore: true,
}), "complete",
  "cursor movement completes a page even when runtime turn length stays capped");
assert.equal(historyPageStatus(pendingPage, {
  sid: "session-a", revision: "revision-a",
  cursor: "cursor-b", hasMore: false,
}), "complete");
assert.equal(historyPageStatus({
  ...pendingPage, windowEpoch: 7,
}, {
  sid: "session-a", revision: "revision-a",
  cursor: "cursor-b", hasMore: true, windowEpoch: 8,
}), "complete",
"a terminal page failure advances the window epoch so the same cursor can retry");
assert.equal(historyPageStatus(pendingPage, {
  sid: "session-a", revision: "revision-b",
  cursor: "cursor-c", hasMore: true,
}), "stale",
  "a destructive history revision cannot reuse the old viewport anchor");
assert.equal(historyPageStatus(pendingPage, {
  sid: "session-b", revision: "revision-a",
  cursor: "cursor-c", hasMore: true,
}), "stale",
  "a delayed page from another session cannot move this viewport");
assert.equal(historyAnchor.markRendering(currentAnchorGeneration), true);
assert.equal(historyAnchor.current()?.phase, "rendering",
  "an accepted page may expand the render window only once before anchoring");
assert.equal(historyAnchor.markRendering(currentAnchorGeneration), false,
  "a synchronous layout re-render cannot reveal a second batch");
historyAnchor.cancel();

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

const browseController = new ScrollFollowController();
browseController.pause({
  scrollHeight: 1_200,
  scrollTop: 500,
  clientHeight: 600,
});
assert.deepEqual(browseController.observeScroll({
  scrollHeight: 1_200,
  scrollTop: 600,
  clientHeight: 600,
}, false), { followOutput: false, nearBottom: true },
"reaching a browse window's bottom must not silently resume live following");

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
const appSource = readFileSync(
  new URL("../../../../src/App.tsx", import.meta.url),
  "utf8",
);
assert.match(chatViewSource, /"thread-shell work-thread-shell"/);
assert.match(chatViewSource, /onScroll[\s\S]*maybeAutoLoadOlder/,
  "scrollbar and keyboard movement at the top must auto-load older history");
assert.match(chatViewSource, /onWheel[\s\S]*maybeAutoLoadOlder/,
  "an upward wheel gesture at the top must auto-load older history");
assert.match(chatViewSource, /onTouchMove[\s\S]*maybeAutoLoadOlder/,
  "a continued pull gesture at the top must auto-load older history");
assert.match(chatViewSource, /useVirtualizer/);
assert.match(chatViewSource, /measureElement/);
assert.match(chatViewSource, /updateTurnKeySnapshot/);
assert.doesNotMatch(chatViewSource, /turnsRef\.current/,
  "old and new virtualizer options must not share a mutable turn-key source");
assert.match(chatViewSource, /shouldAdjustScrollPositionOnItemSizeChange/,
  "virtual-core resize correction must be explicitly coordinated");
assert.doesNotMatch(chatViewSource, /HISTORY_ANCHOR_SETTLE_MS/,
  "history anchoring must not expire on a guessed layout timer");
assert.doesNotMatch(css, /content-visibility:auto/,
  "freshly prepended rows must not use speculative intrinsic heights");
assert.doesNotMatch(chatViewSource, /requestedOlderRef.*length/,
  "page completion must use revision/cursor identity, never runtime length");
assert.match(appSource, /historyRevision=\{rt\.historyRevision\}/);
assert.match(appSource, /historyViewRevision=\{historyView\.viewRevision\}/,
  "recovery commit must keep the old scroll scope across its atomic replacement");
assert.match(appSource, /historyCursor=\{historyView\.oldestId\}/);
assert.match(chatViewSource,
  /const enteringBrowse = browseMode[\s\S]*request\.revision === historyRevision[\s\S]*anchor\.revision === historyRevision/,
  "runtime-to-browse must preserve an active first-page anchor within one revision");
assert.match(
  chatViewSource,
  /controller\.observeScroll\(\s*metrics,\s*!browseMode && !textSelectionDragging,\s*\)/,
  "browse windows and active text drags must never re-enable live following",
);
assert.match(chatViewSource, /onClick=\{returnToLatest\}/);
assert.match(chatViewSource, /onLoadNewer/);
assert.match(chatViewSource, /turnNodeRefs/,
  "history placement must use the retained keyed turn node instead of scanning the DOM");
const threadShellRule = css.match(/\.thread-shell\{[^}]+\}/)?.[0] ?? "";
assert.match(threadShellRule, /position:relative/);
const threadFrameRule = css.match(/\.thread-frame\{[^}]+\}/)?.[0] ?? "";
assert.match(threadFrameRule, /position:relative/,
  "the scroll-to-bottom overlay must stay scoped to the thread");
assert.match(threadFrameRule, /min-height:0/,
  "the scroll viewport must be able to shrink above the composer");
const threadRule = css.match(/\.thread\{[^}]+\}/)?.[0] ?? "";
assert.match(threadRule, /overflow-anchor:none/,
  "the keyed layout transaction must be the only history scroll owner");
assert.match(threadRule, /padding:0/,
  "the keyed offset coordinate system requires a padding-free scroll container");
assert.match(chatViewSource, /el\.scrollTop = target/,
  "WebKit history prepends must restore their keyed row before paint");
const threadInRule = css.match(/\.thread-in\{[^}]+\}/)?.[0] ?? "";
assert.match(threadInRule, /padding:0 16px/,
  "the visual thread gutter must live inside the in-flow content");
const scrollBottomRule = css.match(/\.scroll-bottom-wrap\{[^}]+\}/)?.[0] ?? "";
assert.match(scrollBottomRule, /position:absolute/);
assert.doesNotMatch(scrollBottomRule, /position:sticky/);

console.log("scroll follow tests passed");
