import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import {
  BTW_PANEL_SCOPES_KEY, btwPanelScopeKey, readBtwPanelScopes,
  rekeyBtwPanelScope, setBtwPanelScope,
} from "../src/btw-panel-state.ts";

import {
  clearLegacyAuthMarkers,
  probeSession,
  shouldReconnectAfterSessionProbe,
} from "../src/session-auth.ts";
import {
  canEnqueueQuery,
  collectUnconfirmedQueries,
  MAX_QUEUED_QUERY_BYTES,
  queuedQueryWireBytes,
  reduceTargetedRuntime,
} from "../src/runtime-drain.ts";
import {
  mergeAuthoritativeTurnDetail,
  mergeDetailWithLiveTail,
  mergeInitialHistory,
  reconcileProvenCompactionOrphans,
  restoreCachedTurnDetails,
  restoreObservedLiveTurnDetails,
} from "../src/history-merge.ts";
import {
  reconcileBoundCompactionOrphan as reconcileCompactionOnlyOrphan,
} from "../src/compaction-orphans.ts";
import {
  HISTORY_INITIAL_PAGE,
  HISTORY_MORE_PAGE,
} from "../src/history-summary.ts";
import {
  displayHistoryProjection,
  historyConfirmsRecovery,
  historyConfirmsRuntimeRecovery,
  historyNeedsConfirmationRequest,
  isHistoryRecoveryPending,
  isRuntimeHistoryRecoveryPending,
} from "../src/history-recovery.ts";
import {
  acceptsCachedNewerPage,
  appendNewerPage,
  cachedLatestRequiresLiveRuntime,
} from "../src/history-browse.ts";
import {
  acknowledgeCompletion,
  acknowledgeMatchingCompletion,
  catalogCompletionProjection,
  completionAcknowledgementId,
  completionBadgeKind,
  discardBtwCompletionReceipts,
  markCompletionUnread,
  newestCompletionProjection,
  rekeyCompletionReceipts,
} from "../src/completion-badges.ts";
import { imageDimensions } from "../src/img.ts";
import "./presentation-state.test.ts";
import "./slash-command.test.ts";
import {
  historyImageDisplaySource,
  TurnImagePreviewCache,
} from "../src/turn-image-previews.ts";
import {
  HistoryImageAssetCache,
  historyImageAssetKey,
  historyImageAssetCacheSnapshot,
  HISTORY_IMAGE_REQUEST_TIMEOUT_MS,
  shouldAutoloadHistoryImage,
  readyGeneratedImageAsset,
} from "../src/history-image-assets.ts";
import {
  InlineImageAssetCache,
  inlineImageAssetCacheSnapshot,
  INLINE_IMAGE_REQUEST_TIMEOUT_MS,
} from "../src/inline-image-assets.ts";
import {
  boundCachedTurns,
  controlForCachedSession,
  hasOrphanedActiveCompactionProjection,
} from "../src/cache.ts";
import {
  boundRuntimeTurns,
  MAX_RUNTIME_SESSIONS,
  MAX_RUNTIME_TURNS,
  pruneRuntimeMap,
} from "../src/runtime-bounds.ts";
import { ImeSubmitGuard, shouldSubmitTextKey } from "../src/ime-submit.ts";
import {
  makeOpenBtwCommand,
  matchesBtwRequest,
  normalizeDiffTheme,
  normalizeEngine,
  PROTOCOL_VERSION,
} from "../src/protocol.ts";
import {
  protocolRecoveryAction, RelayWs, stableTabClientId,
} from "../src/ws.ts";
import {
  dismissGoalUi,
  GOAL_UI_PREFERENCES_KEY,
  goalStableIdentity,
  goalUiScopeForEvent,
  goalUiScopeKey,
  readGoalUiPreferences,
  reconcileGoalUiPreference,
  rekeyGoalUiPreference,
  rememberGoalUi,
  shouldRecoverGoalUi,
  writeGoalUiPreferences,
} from "../src/scoped-goal-ui.ts";
import {
  clientSlashesFor,
  commandsFor,
  isKnownCodeOnlySlash,
  matchCommands,
  matchSkills,
  matchModelId,
  MODELS,
  modelsFor,
  permissionProfileLabel,
  permsFor,
  skillToken,
} from "../src/data.ts";
import {
  createMobileViewportSync,
  type MobileViewportBindings,
  type MobileViewportEvent,
  type ViewportReading,
} from "../src/use-mobile-viewport.ts";
import type {
  EngineCapabilities,
  History,
  PreviewAsset,
  ServerEvent,
  SessionList,
  SessionControl,
  ThreadGoal,
} from "../src/protocol.ts";
import type { Block, Turn } from "../src/reducer.ts";
import { clampPanelWidth, resolveSidebarSwipe } from "../src/responsive-layout.ts";
import {
  classifyTurnNotification,
  turnNotificationBody,
  turnNotificationPresentation,
  turnNotificationTag,
} from "../src/turn-notification.ts";
import {
  NOTIFICATION_MODE_KEY,
  readNotificationMode,
  writeNotificationMode,
} from "../src/notification-mode.ts";
import {
  captureNotificationFragment,
  encodeNotificationRoute,
  parseNotificationFragment,
} from "../src/notification-route.ts";
import {
  resolveNotificationNavigation,
} from "../src/notification-navigation.ts";
import {
  PushBindingCoordinator,
  type PushBindingTarget,
} from "../src/push.ts";
import {
  classifyBusySubmit,
  isComposerBusy,
  isInterruptSettling,
  isSettlingStopDisabled,
} from "../src/composer-submit.ts";
import { workContextMetrics } from "../src/work-context.ts";
import { processBlocks } from "../src/process-blocks.ts";
import {
  completedGoalHasNewerUserTurn,
  latestPlanProgress,
  planFollowsCompletedGoal,
  planProgressPresentation,
  SessionPlanProgressCache,
} from "../src/plan-progress.ts";
import { PointerTapGuard } from "../src/pointer-tap.ts";
import {
  cacheSkillCatalog,
  MAX_SKILL_CATALOGS,
  SkillCatalogRequestCoordinator,
  skillCatalogFresh,
  skillCatalogKey,
  skillCatalogRefreshSucceeded,
  skillCatalogReadKey,
  skillCatalogResponseMatches,
  SKILL_CATALOG_TTL_MS,
} from "../src/skill-catalog-cache.ts";
import {
  constrainImageTransform,
  panImageTransform,
  pinchImageTransform,
  zoomImageTransform,
} from "../src/image-gesture.ts";
import {
  bumpSessionActivity,
  compareSessionsByActivity,
  mergeSessionActivityState,
  orderCodeDirectoryGroups,
  sessionCommandTarget,
  setSessionPinned,
  sessionActivityTime,
  visibleDirectorySessions,
} from "../src/session-order.ts";
import {
  ComposerDraftStore,
  composerDraftKey,
} from "../src/composer-drafts.ts";
import {
  ENGINE_SPACES_KEY,
  LEGACY_SPACE_KEY,
  readEngineSpaces,
  rememberEngineSpace,
} from "../src/surface-preferences.ts";
import {
  normalizeSessionList,
  updateScopedSessionLifecycle,
} from "../src/session-list.ts";
import {
  codexProfileIdForSession,
  codexProfilePresentation,
} from "../src/codex-profile-presentation.ts";

const composerDrafts = new ComposerDraftStore();
const preferenceValues = new Map<string, string>([[LEGACY_SPACE_KEY, "work"]]);
const preferenceStorage = {
  getItem: (key: string) => preferenceValues.get(key) ?? null,
};
assert.deepEqual(readEngineSpaces(preferenceStorage, "codex"), {
  claude: "code", codex: "work", dsh: "code",
}, "legacy space migrates only to the previously active engine");
preferenceValues.set(ENGINE_SPACES_KEY, JSON.stringify({
  claude: "work", codex: "code", dsh: "code",
}));
assert.deepEqual(readEngineSpaces(preferenceStorage, "codex"), {
  claude: "work", codex: "code", dsh: "code",
}, "persisted engine spaces survive reload independently");
assert.deepEqual(
  rememberEngineSpace({ claude: "work", codex: "code", dsh: "code" }, "codex", "work"),
  { claude: "work", codex: "work", dsh: "code" },
  "changing one engine's space must preserve the other engine's memory",
);
let switchingSpaces: { claude: "code" | "work"; codex: "code" | "work"; dsh: "code" | "work" } = {
  claude: "code", codex: "code", dsh: "code",
};
switchingSpaces = rememberEngineSpace(switchingSpaces, "claude", "work");
assert.equal(switchingSpaces.codex, "code",
  "Codex Code -> Claude Work must leave the remembered Codex surface alone");
assert.equal(switchingSpaces.claude, "work",
  "switching back to Claude must restore Claude Work");
const oneCodexProfile = [{ id: "primary", label: "Main" }];
assert.equal(
  codexProfileIdForSession("stack@same-native-id", "primary"),
  "stack",
  "a catalog-delayed child keeps the account encoded in its wire id",
);
assert.equal(
  codexProfileIdForSession("same-native-id", "primary"),
  "primary",
  "single-profile wire ids retain their configured default ownership",
);
assert.equal(
  codexProfilePresentation(oneCodexProfile, "primary", "primary"),
  null,
  "single-account installs must not gain profile UI or layout",
);
const presentationProfiles = [
  { id: "primary", label: "Main" },
  ...Array.from({ length: 13 }, (_, index) => ({
    id: `secondary-${index}`,
    label: `Account ${index + 2}`,
  })),
];
assert.deepEqual(
  codexProfilePresentation(presentationProfiles, "primary", "primary"),
  {
    name: "default",
    label: "Main",
    fullLabel: "default · Main",
    tone: 0,
  },
);
assert.equal(
  codexProfilePresentation(
    presentationProfiles, "primary", "secondary-0")?.name,
  "nyx",
);
assert.equal(
  codexProfilePresentation(
    presentationProfiles, "primary", "secondary-11")?.name,
  "asteria",
);
assert.equal(
  codexProfilePresentation(
    presentationProfiles, "primary", "secondary-12")?.name,
  "more",
  "accounts beyond the twelve named ribbons share the bounded fallback label",
);
const longPermissionProfileLabel = permissionProfileLabel(
  `custom-${"authorization-boundary-".repeat(20)}`);
assert.ok(longPermissionProfileLabel?.endsWith("…"));
assert.ok(Array.from(longPermissionProfileLabel ?? "").length <= 24,
  "unknown permission-profile labels must remain bounded");
const draftA = composerDraftKey("machine-a", "code", "codex", "session-a");
const draftB = composerDraftKey("machine-a", "code", "codex", "session-b");
const draftOtherSurface = composerDraftKey(
  "machine-a", "work", "codex", "session-a",
);
const goalScopeA = goalUiScopeKey(
  "machine-a", "code", "codex", "session-a");
const goalScopeOtherMachine = goalUiScopeKey(
  "machine-b", "code", "codex", "session-a");
const goalScopeOtherSurface = goalUiScopeKey(
  "machine-a", "work", "codex", "session-a");
assert.notEqual(goalScopeA, goalScopeOtherMachine,
  "the same sid on another machine must have independent Goal UI state");
assert.notEqual(goalScopeA, goalScopeOtherSurface,
  "the same sid on Work must not inherit Code Goal UI state");
assert.equal(goalUiScopeForEvent("unknown-session"), null,
  "Goal UI persistence must fail closed without receive-time ownership");
assert.equal(goalUiScopeForEvent("session-a", {
  machineId: "machine-a", space: "code", engine: "codex",
}), goalScopeA);
const persistedGoal = {
  threadId: "session-a",
  objective: "ship the release without leaking this text to storage",
  status: "active" as const,
  engine: "codex" as const,
  tokensUsed: 1,
  timeUsedSeconds: 2,
  createdAt: 100,
};
const discoveredGoal = reconcileGoalUiPreference(
  {}, goalScopeA, persistedGoal, 10);
assert.equal(discoveredGoal.revealed, true,
  "an authoritative Goal must reveal itself on a new browser or device");
assert.equal(discoveredGoal.preferences[goalScopeA]?.known, true,
  "Goal discovery must survive a refresh without requiring /goal");
const remotelyDismissedGoal = reconcileGoalUiPreference(
  {}, goalScopeOtherMachine, persistedGoal, 10, true);
assert.equal(remotelyDismissedGoal.revealed, false,
  "a server dismissal must hide the same Goal on a fresh device");
assert.equal(
  remotelyDismissedGoal.preferences[goalScopeOtherMachine]?.hiddenGoal,
  goalStableIdentity(persistedGoal),
  "the fresh device caches only the opaque local Goal fingerprint",
);
let goalPreferences = rememberGoalUi(discoveredGoal.preferences, goalScopeA, 10);
let reconciledGoal = reconcileGoalUiPreference(
  goalPreferences, goalScopeA, persistedGoal, 11);
assert.equal(reconciledGoal.revealed, true);
goalPreferences = dismissGoalUi(
  reconciledGoal.preferences, goalScopeA, persistedGoal, 12);
reconciledGoal = reconcileGoalUiPreference(
  goalPreferences, goalScopeA, persistedGoal, 13);
assert.equal(reconciledGoal.revealed, false,
  "refresh must preserve a dismissal for the exact stable Goal identity");
reconciledGoal = reconcileGoalUiPreference(
  goalPreferences, goalScopeA, { ...persistedGoal, createdAt: 101 }, 14);
assert.equal(reconciledGoal.revealed, true,
  "a newly-created Goal must not inherit the previous Goal's dismissal");
reconciledGoal = reconcileGoalUiPreference(
  goalPreferences, goalScopeA, {
    ...persistedGoal,
    objective: "a replacement objective with the same native createdAt",
  }, 15);
assert.equal(reconciledGoal.revealed, true,
  "a replacement Codex Goal must not inherit the previous Goal's dismissal");
assert.equal(goalStableIdentity({ ...persistedGoal, createdAt: undefined }), null);
const identitylessDismissal = dismissGoalUi(
  rememberGoalUi({}, goalScopeA, 20), goalScopeA,
  { ...persistedGoal, createdAt: undefined }, 21);
assert.equal(reconcileGoalUiPreference(
  identitylessDismissal, goalScopeA,
  { ...persistedGoal, createdAt: undefined }, 22).revealed, true,
"an identityless Goal dismissal is page-local and must not hide future Goals");
const goalStorageValues = new Map<string, string>();
const goalStorage = {
  getItem: (key: string) => goalStorageValues.get(key) ?? null,
  setItem: (key: string, value: string) => {
    goalStorageValues.set(key, value);
  },
};
goalPreferences = writeGoalUiPreferences(goalStorage, goalPreferences);
assert.doesNotMatch(
  goalStorageValues.get(GOAL_UI_PREFERENCES_KEY) ?? "",
  /ship the release/,
  "Goal objective text must never be persisted in UI preferences",
);
assert.deepEqual(readGoalUiPreferences(goalStorage), goalPreferences);
function verifyGoalRecoveryPreferences() {
  const absentGoal = reconcileGoalUiPreference(
    discoveredGoal.preferences, goalScopeA, null, 30);
  assert.equal(shouldRecoverGoalUi(absentGoal.preferences[goalScopeA]), false,
    "authoritative absence survives refocus without a recovery flash");
  writeGoalUiPreferences(goalStorage, absentGoal.preferences);
  assert.equal(shouldRecoverGoalUi(readGoalUiPreferences(goalStorage)[goalScopeA]), false,
    "authoritative absence also survives a browser reload");
  assert.equal(shouldRecoverGoalUi({ known: true, seenAt: 1 }), false,
    "legacy visit markers are refreshed silently instead of reviving old Goals");
  assert.equal(shouldRecoverGoalUi(discoveredGoal.preferences[goalScopeA]), true);
  assert.equal(shouldRecoverGoalUi(reconcileGoalUiPreference(
    discoveredGoal.preferences, goalScopeA, { ...persistedGoal, status: "complete" },
  ).preferences[goalScopeA]), false,
  "a completed Goal cannot reappear as an active recovery after reload");
  assert.equal(reconcileGoalUiPreference(absentGoal.preferences, goalScopeA,
    { ...persistedGoal, createdAt: 102 }).revealed, true,
  "a new Goal still reveals itself after an authoritative absent response");
}
verifyGoalRecoveryPreferences();
const rekeyedGoalPreferences = rekeyGoalUiPreference(
  goalPreferences,
  goalScopeA,
  goalUiScopeKey("machine-a", "code", "codex", "session-real"),
);
assert.equal(goalScopeA in rekeyedGoalPreferences, false);
composerDrafts.set(draftA, {
  input: "session A unfinished",
  images: [{ media_type: "image/png", data: "draft-image" }],
  files: [{ filename: "notes.txt", data: "draft-file" }],
  pastes: [],
});
assert.equal(composerDrafts.get(draftB).input, "",
  "switching sessions must not carry the previous composer text");
assert.equal(composerDrafts.get(draftOtherSurface).input, "",
  "the same sid on another surface must have an independent draft");
composerDrafts.set(draftB, {
  input: "session B unfinished",
  images: [],
  files: [],
  pastes: [],
});
assert.equal(composerDrafts.get(draftA).input, "session A unfinished",
  "returning to a session restores only that session's draft");
assert.equal(composerDrafts.get(draftA).images[0]?.data, "draft-image");
assert.equal(composerDrafts.get(draftA).files[0]?.filename, "notes.txt");
const tempDraft = composerDraftKey("machine-a", "code", "codex", "tmp-new");
const realDraft = composerDraftKey("machine-a", "code", "codex", "real-session");
composerDrafts.set(tempDraft, {
  input: "typed while the first turn was running",
  images: [],
  files: [],
  pastes: [],
});
composerDrafts.rekey(tempDraft, realDraft);
assert.equal(composerDrafts.get(realDraft).input,
  "typed while the first turn was running",
  "session id capture must move a temp session draft atomically");
assert.equal(composerDrafts.get(tempDraft).input, "");
const btwDraftA = composerDraftKey(
  "machine-a", "code", "codex", "btw:btw-a");
const btwDraftB = composerDraftKey(
  "machine-a", "code", "codex", "btw:btw-b");
composerDrafts.set(btwDraftA, {
  input: "unfinished side question",
  images: [],
  files: [],
  pastes: [],
});
assert.equal(composerDrafts.get(btwDraftB).input, "",
  "BTW drafts must not follow another fork");
assert.equal(composerDrafts.get(btwDraftA).input,
  "unfinished side question",
  "returning to the same BTW restores only its draft");
composerDrafts.delete(btwDraftA);
assert.equal(composerDrafts.get(btwDraftA).input, "",
  "explicit BTW close discards its private draft");

assert.equal(sessionActivityTime("2026-07-17T10:00:00Z"),
  Date.parse("2026-07-17T10:00:00Z"), "ISO Claude activity timestamps must sort correctly");
assert.ok(sessionActivityTime("1752746400000") > sessionActivityTime("1752746399"),
  "millisecond and second timestamps must share one ordering scale");

let completionReceipts = markCompletionUnread(
  {}, "parent-a", "parent-a", "main", "turn-a");
completionReceipts = markCompletionUnread(
  completionReceipts, "parent-a", "btw-a", "btw");
completionReceipts = markCompletionUnread(
  completionReceipts, "parent-b", "btw-b", "btw");
assert.equal(completionBadgeKind(completionReceipts["parent-a"]), "both");
assert.equal(completionBadgeKind(completionReceipts["parent-b"]), "btw");
assert.equal(completionBadgeKind(
  { main: true, mainCompletionId: "turn-new", mainTurnEndSeq: 10,
    mainTurnEndGeneration: "generation-1", btwSids: [] }, false), "main",
  "a newer local completion remains visible while its authoritative frame is delayed");
assert.equal(completionBadgeKind(
  { main: false, mainCompletionId: null, mainTurnEndSeq: null,
    mainTurnEndGeneration: null, btwSids: [] }, true), "main",
  "an authoritative unread completion remains visible without a local fallback");
assert.equal(completionAcknowledgementId(
  completionReceipts["parent-a"], undefined), "turn-a",
  "a missed completion_state can be acknowledged from its turn_end fallback");
assert.equal(completionAcknowledgementId(
  completionReceipts["parent-a"], { id: "turn-new", unread: true }),
  "turn-new", "a newer authoritative completion wins over the fallback id");
const newerLocalCompletion = markCompletionUnread(
  {}, "catalog-race", "catalog-race", "main", "turn-new");
const staleReadProjection = newestCompletionProjection(undefined, {
  id: "turn-old", unread: false, revision: 2,
});
assert.equal(acknowledgeMatchingCompletion(
  newerLocalCompletion, "catalog-race", staleReadProjection),
newerLocalCompletion,
"a delayed catalog acknowledgement cannot clear a newer local completion");
assert.equal(acknowledgeMatchingCompletion(
  newerLocalCompletion, "catalog-race",
  { id: "turn-new", unread: false, revision: 4 },
)["catalog-race"], undefined,
"the matching authoritative read receipt clears its local fallback");
const newerRuntimeProjection = newestCompletionProjection(
  { id: "turn-new", unread: true, revision: 5 },
  { id: "turn-old", unread: false, revision: 4 },
);
assert.equal(acknowledgeMatchingCompletion(
  newerLocalCompletion, "catalog-race", newerRuntimeProjection),
newerLocalCompletion,
"runtime revision ordering rejects an older catalog acknowledgement");
const firstIdentitylessLocalCompletion = markCompletionUnread(
  {}, "identityless", "identityless", "main", null, 40, "generation-1");
const identitylessLocalCompletion = markCompletionUnread(
  firstIdentitylessLocalCompletion,
  "identityless", "identityless", "main", null, 50, "generation-1",
);
assert.equal(
  identitylessLocalCompletion.identityless.mainTurnEndSeq, 50,
  "a second null-id completion advances its causal TurnEnd boundary",
);
assert.equal(acknowledgeMatchingCompletion(
  identitylessLocalCompletion, "identityless",
  { id: "generated-old", unread: false, revision: 3 },
), identitylessLocalCompletion,
"an unordered catalog receipt cannot clear a null-id local completion");
assert.equal(acknowledgeMatchingCompletion(
  identitylessLocalCompletion, "identityless",
  { id: "generated-old", unread: false, revision: 3 },
  { authoritativeSeq: 41, authoritativeGeneration: "generation-1" },
), identitylessLocalCompletion,
"an older ordered receipt cannot clear the next null-id completion");
assert.equal(acknowledgeMatchingCompletion(
  identitylessLocalCompletion, "identityless",
  { id: "generated-new", unread: false, revision: 5 },
  { authoritativeSeq: 51, authoritativeGeneration: "generation-2" },
), identitylessLocalCompletion,
"sequence numbers from another wrapper generation are not comparable");
assert.equal(acknowledgeMatchingCompletion(
  identitylessLocalCompletion, "identityless",
  { id: "generated-new", unread: false, revision: 5 },
  { authoritativeSeq: 51, authoritativeGeneration: "generation-1" },
)["identityless"], undefined,
"a causally later CompletionState clears a null-id local fallback");
const identifiedLocalCompletion = markCompletionUnread(
  {}, "cleared", "cleared", "main", "turn-cleared", 60, "generation-1");
assert.equal(acknowledgeMatchingCompletion(
  identifiedLocalCompletion, "cleared",
  { id: null, unread: false, revision: 6 },
), identifiedLocalCompletion,
"an unordered catalog clear cannot remove an identified local completion");
assert.equal(acknowledgeMatchingCompletion(
  identifiedLocalCompletion, "cleared",
  { id: null, unread: false, revision: 6 },
  { authoritativeSeq: 59, authoritativeGeneration: "generation-1" },
), identifiedLocalCompletion,
"an older clear cannot remove the next identified local completion");
assert.equal(acknowledgeMatchingCompletion(
  identifiedLocalCompletion, "cleared",
  { id: null, unread: false, revision: 6 },
  { authoritativeSeq: 61, authoritativeGeneration: "generation-2" },
), identifiedLocalCompletion,
"a clear from another wrapper generation cannot remove a local completion");
assert.equal(acknowledgeMatchingCompletion(
  identifiedLocalCompletion, "cleared",
  { id: null, unread: false, revision: 6 },
  { authoritativeSeq: 61, authoritativeGeneration: "generation-1" },
)["cleared"], undefined,
"a causally later clear removes an identified local completion");
completionReceipts = acknowledgeCompletion(
  completionReceipts, "parent-a", { main: true });
assert.equal(completionBadgeKind(completionReceipts["parent-a"]), "btw",
  "opening the main session must not acknowledge an unseen BTW result");
completionReceipts = acknowledgeCompletion(
  completionReceipts, "parent-a", { btwSid: "btw-a" });
assert.equal(completionReceipts["parent-a"], undefined,
  "opening the matching BTW clears the final completion receipt");
completionReceipts = rekeyCompletionReceipts(
  completionReceipts, "parent-b", "parent-real");
assert.equal(completionReceipts["parent-b"], undefined);
assert.deepEqual(completionReceipts["parent-real"], {
  main: false, mainCompletionId: null, mainTurnEndSeq: null,
  mainTurnEndGeneration: null, btwSids: ["btw-b"],
});
completionReceipts = markCompletionUnread(
  completionReceipts, "parent-main", "parent-main", "main", "turn-main");
completionReceipts = discardBtwCompletionReceipts(completionReceipts);
assert.equal(completionReceipts["parent-real"], undefined,
  "a wrapper restart removes receipts for destroyed ephemeral BTW forks");
assert.deepEqual(completionReceipts["parent-main"], {
  main: true, mainCompletionId: "turn-main", mainTurnEndSeq: null,
  mainTurnEndGeneration: null, btwSids: [],
}, "main-session completion receipts survive a wrapper restart");

const processTap = new PointerTapGuard(8);
processTap.pointerDown(1, 20, 20);
assert.equal(processTap.pointerMove(1, 22, 24), false);
processTap.pointerUp(1);
assert.equal(processTap.consumeClick(1), true,
  "a stationary touch remains a real process-header tap");
processTap.pointerDown(2, 20, 20);
assert.equal(processTap.pointerMove(2, 20, 36), true,
  "crossing the drag threshold releases the touch origin exactly once");
assert.equal(processTap.pointerMove(2, 20, 48), false,
  "a released drag pointer cannot repeatedly blur/release its control");
processTap.pointerUp(2);
assert.equal(processTap.consumeClick(1), false,
  "vertical history scrolling must not toggle the process header");
processTap.pointerDown(3, 20, 20);
processTap.pointerDown(4, 30, 20);
processTap.pointerUp(3);
processTap.pointerUp(4);
assert.equal(processTap.consumeClick(1), false,
  "multi-touch must not synthesize a process-header activation");
processTap.pointerDown(5, 20, 20);
processTap.pointerCancel(5);
assert.equal(processTap.consumeClick(1), false,
  "a cancelled touch must not toggle the process header");
assert.equal(processTap.consumeClick(0), true,
  "keyboard-generated clicks must remain accessible");

assert.deepEqual(pinchImageTransform(
  { scale: 1, x: 0, y: 0 },
  { x: 100, y: 150 }, { x: 200, y: 150 },
  { x: 70, y: 160 }, { x: 270, y: 160 },
  { width: 300, height: 300 }, { width: 300, height: 300 },
), { scale: 2, x: 20, y: 10 },
"pinch zoom follows the two-finger midpoint instead of the image center");
assert.deepEqual(panImageTransform(
  { scale: 2, x: 0, y: 0 }, 500, -500,
  { width: 300, height: 200 }, { width: 300, height: 300 },
), { scale: 2, x: 150, y: -50 },
"zoomed image panning stays inside the visible bounds");
assert.deepEqual(constrainImageTransform(
  { scale: 0.5, x: 99, y: 99 },
  { width: 300, height: 200 }, { width: 300, height: 300 },
), { scale: 1, x: 0, y: 0 },
"returning to fit scale resets stale translation");
assert.deepEqual(zoomImageTransform(
  { scale: 1, x: 0, y: 0 }, 2, { x: 225, y: 150 },
  { width: 300, height: 300 }, { width: 300, height: 300 },
), { scale: 2, x: -75, y: 0 },
"wheel zoom keeps the content beneath the trackpad focal point");
assert.equal(mergeSessionActivityState("running", "idle"), "running",
  "catalog-native activity must not be overwritten by an idle resident runtime");
assert.equal(mergeSessionActivityState("idle", "running"), "running",
  "a locally running resident runtime remains authoritative");
assert.equal(mergeSessionActivityState("running", "interrupting"), "interrupting",
  "an explicit local transition overrides catalog activity");
const scopedLifecycleCatalog = {
  "code:claude": [{
    session_id: "same-session", engine: "claude" as const,
    space: "code" as const, state: "running" as const,
  }],
  "work:claude": [{
    session_id: "same-session", engine: "claude" as const,
    space: "work" as const, state: "running" as const,
  }],
  "code:codex": [{
    session_id: "same-session", engine: "codex" as const,
    space: "code" as const, state: "running" as const,
  }],
};
const scopedLifecycleIdle = updateScopedSessionLifecycle(
  scopedLifecycleCatalog, "claude", "code", "same-session", "idle");
assert.equal(scopedLifecycleIdle["code:claude"][0].state, "idle");
assert.equal(scopedLifecycleIdle["work:claude"][0].state, "running");
assert.equal(scopedLifecycleIdle["code:codex"][0].state, "running",
  "a terminal lifecycle frame updates only its frozen engine/space catalog");

assert.deepEqual(
  permsFor("claude").map(({ id, name, short }) => ({ id, name, short })),
  [
    { id: "default", name: "Default", short: "Default" },
    { id: "acceptEdits", name: "Accept Edits", short: "Accept Edits" },
    { id: "plan", name: "Plan", short: "Plan" },
    { id: "auto", name: "Auto", short: "Auto" },
    { id: "bypassPermissions", name: "Bypass Permissions", short: "Bypass Permissions" },
  ],
  "Claude permission labels should match the official English mode names",
);
assert.deepEqual(
  permsFor("codex").map(({ id, name, short }) => ({ id, name, short })),
  [
    { id: "never", name: "Never", short: "Never" },
    { id: "on-request", name: "On Request", short: "On Request" },
    { id: "untrusted", name: "Untrusted", short: "Untrusted" },
  ],
  "Codex permission labels should match the official approval-policy names",
);
for (const engine of ["claude", "codex"] as const) {
  for (const slash of ["extensions", "skills", "plugins", "apps", "mcp", "hooks"]) {
    assert.equal(clientSlashesFor(engine).has(slash), true,
      `/${slash} must remain local for ${engine}`);
    assert.equal(commandsFor(engine, "work").some(
      (entry) => "slash" in entry && entry.slash === slash), true,
      `/${slash} must be reachable from Work`);
  }
}
assert.equal(clientSlashesFor("claude").has("hook"), false,
  "Claude singular /hook remains native; Remote management uses /hooks");
assert.equal(skillToken("$"), "");
assert.equal(skillToken("$cavecrew"), "cavecrew");
assert.equal(skillToken("$cavecrew explain this"), null,
  "Skill suggestions must close as soon as prompt arguments begin");
assert.equal(skillToken("use $cavecrew"), null,
  "only a leading $ is an explicit Codex Skill invocation");
const skillCatalog = [
  { kind: "skill", id: "1", name: "cavecrew", enabled: true,
    description: "Delegate work" },
  { kind: "skill", id: "2", name: "Caveman", enabled: true },
  { kind: "skill", id: "3", name: "cavecrew", enabled: true },
  { kind: "skill", id: "4", name: "disabled-skill", enabled: false },
  { kind: "plugin", id: "5", name: "cave-plugin", installed: true },
] satisfies import("../src/protocol.ts").EngineCapabilityItem[];
assert.deepEqual(matchSkills("cave", skillCatalog).map((item) => item.name),
  ["cavecrew", "Caveman"],
  "Skill completion must prefix-match case-insensitively and deduplicate triggers");
assert.deepEqual(matchSkills("", skillCatalog).map((item) => item.name),
  ["cavecrew", "Caveman"],
  "only enabled Skills may be offered for explicit invocation");
const repoSkillKey = skillCatalogKey(
  "machine-a", "codex", "code", "/repo/a");
const stackSkillKey = skillCatalogKey(
  "machine-a", "codex", "code", "/repo/a", "stack");
assert.notEqual(repoSkillKey, skillCatalogKey(
  "machine-a", "codex", "code", "/repo/b"),
  "Skill catalogs from different repositories must never share a cache entry");
assert.notEqual(repoSkillKey, skillCatalogKey(
  "machine-b", "codex", "code", "/repo/a"),
  "Skill catalogs must remain inside the machine authorization boundary");
assert.notEqual(repoSkillKey, stackSkillKey,
  "Skill catalogs from different CODEX_HOME profiles must never share a cache entry");
assert.notEqual(
  skillCatalogReadKey(repoSkillKey, true),
  skillCatalogReadKey(repoSkillKey, false),
  "a lightweight Skill read must not swallow a queued full extension read",
);
const skillRead = {
  key: repoSkillKey,
  requestId: "skills-request-a",
  engine: "codex",
  space: "code",
  cwd: "/repo/a",
  skillsOnly: true,
} as const;
const skillResponse = {
  v: 26,
  ts: 0,
  type: "engine_capabilities",
  engine: "codex",
  space: "code",
  request_id: "skills-request-a",
  cwd: "/repo/a",
  items: skillCatalog,
  errors: [],
  notes: [],
  skills_only: true,
} satisfies EngineCapabilities;
assert.equal(skillCatalogResponseMatches(skillRead, skillResponse), true);
function testDshSkillCatalog() {
  const dshSkillKey = skillCatalogKey("machine-a", "dsh", "code", "/repo/a", null, null, "dsh@one");
  const dshOtherKey = skillCatalogKey("machine-a", "dsh", "code", "/repo/a", null, null, "dsh@two");
  assert.notEqual(dshSkillKey, dshOtherKey, "DSH catalogs belong to the exact native session");
  const dshSkillRead = { ...skillRead, engine: "dsh" as const, key: dshSkillKey, sid: "dsh@one" };
  const dshSkillResponse = { ...skillResponse, engine: "dsh" as const, sid: "dsh@one" };
  assert.equal(skillCatalogResponseMatches(dshSkillRead, dshSkillResponse), true);
  assert.equal(skillCatalogResponseMatches(dshSkillRead, { ...dshSkillResponse, sid: "dsh@two" }), false);
  const failedSkillStarts: string[] = [];
  const failedSkillReads = new SkillCatalogRequestCoordinator(request => {
    failedSkillStarts.push(request.key);
    return `failure-test-${failedSkillStarts.length}`;
  });
  assert.equal(failedSkillReads.request(dshSkillRead), true);
  assert.equal(failedSkillReads.request({ ...dshSkillRead, key: dshOtherKey, sid: "dsh@two" }), false);
  assert.equal(failedSkillReads.fail("unrelated"), null);
  assert.equal(failedSkillReads.fail("failure-test-1")?.sid, "dsh@one");
  assert.deepEqual(failedSkillStarts, [dshSkillKey, dshOtherKey], "an error releases the next session's read");
  assert.equal(failedSkillReads.hasPendingRead(dshSkillKey, true), false);
  assert.equal(failedSkillReads.accept({ ...dshSkillResponse, sid: "dsh@two", request_id: "failure-test-2" })?.request.sid, "dsh@two");
  assert.equal(failedSkillReads.request(dshSkillRead), true, "a failed catalog can be retried");
}
testDshSkillCatalog();
assert.equal(skillCatalogResponseMatches(
  skillRead, { ...skillResponse, request_id: "replayed-old-request" }), false,
  "a replayed capability response must not complete a newer read",
);
assert.equal(skillCatalogResponseMatches(
  skillRead, { ...skillResponse, cwd: "/repo/b" }), false,
  "a capability response from another cwd must not complete the active read",
);
assert.equal(skillCatalogResponseMatches(
  { ...skillRead, cwd: "" }, skillResponse), true,
  "an omitted cwd must accept the wrapper's resolved default directory",
);
assert.equal(skillCatalogResponseMatches(
  { ...skillRead, key: stackSkillKey, codexProfileId: "stack" },
  { ...skillResponse, codex_profile_id: "stack" },
), true, "a capability response may complete only its exact Codex profile read");
assert.equal(skillCatalogResponseMatches(
  { ...skillRead, key: stackSkillKey, codexProfileId: "stack" },
  { ...skillResponse, codex_profile_id: "primary" },
), false, "a late capability response from another Codex profile is rejected");
assert.equal(skillCatalogRefreshSucceeded(skillResponse), true);
assert.equal(skillCatalogRefreshSucceeded({
  ...skillResponse,
  items: [],
  errors: ["skills: app-server request failed"],
}), false, "a failed Skill refresh must preserve stale cached items");
assert.equal(skillCatalogRefreshSucceeded({
  ...skillResponse,
  skills_only: false,
  errors: ["plugins: app-server request failed"],
}), true, "an unrelated full-inventory failure must not hide valid Skills");

const capabilityRequest = (
  key: string,
  cwd: string,
  skillsOnly: boolean,
) => ({
  key,
  engine: "codex" as const,
  space: "code" as const,
  cwd,
  skillsOnly,
});
const capabilityResponse = (
  requestId: string,
  cwd: string,
  skillsOnly: boolean,
): EngineCapabilities => ({
  ...skillResponse,
  request_id: requestId,
  cwd,
  skills_only: skillsOnly,
});

const parallelLaneStarts: Array<{
  requestId: string;
  key: string;
  skillsOnly: boolean;
}> = [];
const parallelLanes = new SkillCatalogRequestCoordinator((request) => {
  const requestId = `parallel-read-${parallelLaneStarts.length + 1}`;
  parallelLaneStarts.push({
    requestId, key: request.key, skillsOnly: request.skillsOnly,
  });
  return requestId;
});
const parallelFullRequest = capabilityRequest(
  repoSkillKey, "/repo/a", false);
const parallelSkillsRequest = capabilityRequest(
  repoSkillKey, "/repo/a", true);
assert.equal(parallelLanes.request(parallelFullRequest), true);
assert.equal(parallelLanes.request(parallelSkillsRequest), true,
  "a Skills-only read must not wait for a slow full inventory read");
assert.deepEqual(
  parallelLaneStarts.map(({ skillsOnly }) => skillsOnly),
  [false, true],
  "full and Skills-only reads use independent bounded lanes");
assert.equal(parallelLanes.accept(
  capabilityResponse("parallel-read-2", "/repo/a", true))?.request.skillsOnly,
  true);
assert.equal(parallelLanes.hasPendingRead(repoSkillKey, false), true,
  "accepting the fast Skills response must leave full inventory active");
assert.equal(parallelLanes.hasPendingRead(repoSkillKey, true), false);
assert.equal(parallelLanes.accept(
  capabilityResponse("parallel-read-1", "/repo/a", false))?.request.skillsOnly,
  false);

const parallelMutationStarts: string[] = [];
const parallelMutation = new SkillCatalogRequestCoordinator((_request) => {
  const requestId = `parallel-mutation-read-${parallelMutationStarts.length + 1}`;
  parallelMutationStarts.push(requestId);
  return requestId;
});
assert.equal(parallelMutation.request(parallelFullRequest), true);
assert.equal(parallelMutation.request(parallelSkillsRequest), true);
assert.equal(parallelMutation.trackMutation(
  "parallel-mutation", parallelFullRequest), true);
assert.equal(parallelMutation.accept(
  capabilityResponse("parallel-mutation", "/repo/a", false))?.superseded,
  false);
assert.equal(parallelMutation.accept(
  capabilityResponse(
    "parallel-mutation-read-2", "/repo/a", true))?.superseded,
  true, "a same-scope mutation must supersede the active Skills lane");
assert.equal(parallelMutation.accept(
  capabilityResponse(
    "parallel-mutation-read-1", "/repo/a", false))?.superseded,
  true, "a same-scope mutation must supersede the active full lane");

const mutationRaceStarts: Array<{ requestId: string; key: string; skillsOnly: boolean }> = [];
const mutationRace = new SkillCatalogRequestCoordinator((request) => {
  const requestId = `race-read-${mutationRaceStarts.length + 1}`;
  mutationRaceStarts.push({
    requestId, key: request.key, skillsOnly: request.skillsOnly,
  });
  return requestId;
});
const raceSkillsRequest = capabilityRequest(repoSkillKey, "/repo/a", true);
const raceFullRequest = capabilityRequest(repoSkillKey, "/repo/a", false);
assert.equal(mutationRace.request(raceSkillsRequest), true);
assert.equal(mutationRace.trackMutation("race-mutation", raceFullRequest), true);
assert.equal(mutationRace.request(raceFullRequest), false,
  "a read requested during a same-scope mutation must wait");
assert.equal(mutationRaceStarts.length, 1,
  "the queued refresh must not overtake the pending mutation");
const raceMutationAcceptance = mutationRace.accept(
  capabilityResponse("race-mutation", "/repo/a", false));
assert.equal(raceMutationAcceptance?.superseded, false);
assert.deepEqual(mutationRaceStarts.at(-1), {
  requestId: "race-read-2",
  key: repoSkillKey,
  skillsOnly: false,
}, "the full lane may refresh after mutation while stale Skills drain");
const staleReadAcceptance = mutationRace.accept(
  capabilityResponse("race-read-1", "/repo/a", true));
assert.equal(staleReadAcceptance?.superseded, true,
  "a read started before mutation must not roll the catalog back");
assert.equal(mutationRaceStarts.length, 2,
  "draining the stale Skills lane must not duplicate the active full refresh");

const blockedMutationStarts: string[] = [];
const blockedMutation = new SkillCatalogRequestCoordinator((request) => {
  blockedMutationStarts.push(request.key);
  return `blocked-read-${blockedMutationStarts.length}`;
});
assert.equal(blockedMutation.trackMutation(
  "blocked-mutation", raceFullRequest), true);
assert.equal(blockedMutation.request(raceSkillsRequest), false);
assert.deepEqual(blockedMutationStarts, [],
  "same-scope reads must stay queued while a mutation is pending");
assert.equal(blockedMutation.accept(
  capabilityResponse("blocked-mutation", "/repo/b", false)), null,
  "a mutation response from another cwd must not release the scoped queue");
assert.deepEqual(blockedMutationStarts, []);
assert.ok(blockedMutation.accept(
  capabilityResponse("blocked-mutation", "/repo/a", false)));
assert.deepEqual(blockedMutationStarts, [repoSkillKey],
  "a queued read must start after the mutation response");

const isolatedStarts: string[] = [];
const isolatedRequests = new SkillCatalogRequestCoordinator((request) => {
  const requestId = `isolated-${isolatedStarts.length + 1}`;
  isolatedStarts.push(request.key);
  return requestId;
});
const machineARequest = capabilityRequest(repoSkillKey, "/repo/a", true);
const machineBKey = skillCatalogKey(
  "machine-b", "codex", "code", "/repo/b");
const machineBRequest = capabilityRequest(machineBKey, "/repo/b", true);
assert.equal(isolatedRequests.request(machineARequest), true);
isolatedRequests.reset();
assert.equal(isolatedRequests.request(machineBRequest), true);
assert.equal(isolatedRequests.accept(
  capabilityResponse("isolated-1", "/repo/a", true)), null,
  "a delayed response from a reset machine scope must be ignored");
assert.equal(isolatedRequests.accept(
  capabilityResponse("isolated-2", "/repo/a", true)), null,
  "even a current request id must not authorize another cwd");
assert.equal(isolatedRequests.accept(
  capabilityResponse("isolated-2", "/repo/b", true))?.request.key,
  machineBKey);

const failedDrainStarts: string[] = [];
const failedDrain = new SkillCatalogRequestCoordinator((request) => {
  failedDrainStarts.push(request.key);
  if (request.key === "send-fails") return null;
  return `drain-${failedDrainStarts.length}`;
});
const drainActive = capabilityRequest("active", "/active", true);
assert.equal(failedDrain.request(drainActive), true);
assert.equal(failedDrain.request(
  capabilityRequest("send-fails", "/bad", true)), false);
assert.equal(failedDrain.request(
  capabilityRequest("send-works", "/good", true)), false);
assert.ok(failedDrain.accept(
  capabilityResponse("drain-1", "/active", true)));
assert.deepEqual(failedDrainStarts, ["active", "send-fails", "send-works"],
  "one failed queued send must not discard later runnable scopes");
const freshSkillEntry = { items: skillCatalog, fetchedAt: 1_000 };
assert.equal(skillCatalogFresh(freshSkillEntry, 1_000 + SKILL_CATALOG_TTL_MS - 1),
  true);
assert.equal(skillCatalogFresh(freshSkillEntry, 1_000 + SKILL_CATALOG_TTL_MS),
  false, "an expired Skill catalog must refresh without hiding its stale items");
let boundedSkillCache = {};
for (let index = 0; index <= MAX_SKILL_CATALOGS; index += 1) {
  boundedSkillCache = cacheSkillCatalog(
    boundedSkillCache, `scope-${index}`, skillCatalog, index);
}
assert.equal(Object.keys(boundedSkillCache).length, MAX_SKILL_CATALOGS);
assert.equal("scope-0" in boundedSkillCache, false,
  "the bounded cache must evict the oldest cwd catalog first");
const recentProject = { session_id: "project-new", cwd: "/home/nancy/project",
  last_modified: "300" };
const oldHome = { session_id: "home-old", cwd: "/home/nancy", last_modified: "100" };
const projectOld = { session_id: "project-old", cwd: "/home/nancy/project",
  last_modified: "200" };
const directoryGroups = {
  "/home/nancy": [oldHome],
  "/home/nancy/project": [projectOld, recentProject].sort(compareSessionsByActivity),
};
assert.deepEqual(directoryGroups["/home/nancy/project"].map((session) => session.session_id),
  ["project-new", "project-old"], "sessions inside a directory must be newest first");
assert.deepEqual(orderCodeDirectoryGroups(directoryGroups),
  ["/home/nancy/project", "/home/nancy"],
  "the directory containing the newest session must be first");
const bumpedSessions = bumpSessionActivity(
  [oldHome, recentProject], "home-old", 400_000);
assert.equal(bumpedSessions[0].last_modified, "400000",
  "a live message must update sidebar activity without waiting for another list request");
assert.deepEqual(orderCodeDirectoryGroups({
  "/home/nancy": [bumpedSessions[0]],
  "/home/nancy/project": [recentProject],
}), ["/home/nancy", "/home/nancy/project"],
"an optimistically updated directory must move immediately");
assert.equal(setSessionPinned([oldHome], "home-old", true)[0].pinned, true,
  "pinning must update the visible list immediately");
assert.deepEqual(sessionCommandTarget(
  { ...recentProject, engine: "codex", space: "code" }, "claude", "work"),
{ engine: "codex", space: "code" },
"session actions must target the card engine and space, not the current page");
const sevenSessions = Array.from({ length: 7 }, (_, index) => ({
  session_id: `session-${index}`, last_modified: String(100 - index),
}));
assert.equal(visibleDirectorySessions(sevenSessions, false, false).length, 5,
  "a directory must show only five sessions by default");
assert.equal(visibleDirectorySessions(sevenSessions, true, false).length, 7,
  "expanded directories must show every session");
assert.equal(visibleDirectorySessions(sevenSessions, false, true).length, 7,
  "search results must never be hidden behind the five-session limit");

const visibleAgentTimeline = processBlocks([
  { kind: "tool", message_id: "assistant-1", tool_use_id: "agent-tool",
    tool: "Agent", input: {}, category: "agent", done: false },
  { kind: "process", item_id: "agent:agent-tool", processKind: "agent",
    phase: "start", status: "running", parent_id: "agent-tool",
    title: "审查后端", done: false },
] as import("../src/reducer.ts").Block[]);
assert.deepEqual(visibleAgentTimeline.map((block) => block.kind), ["process"],
  "a dedicated live agent row must replace the duplicate generic ToolUse row");

const legacyWorkContext = workContextMetrics({
  v: 20, ts: 0, type: "context_report",
  total_tokens: 25_572, max_tokens: 1_000_000,
  percentage: 2.5572, categories: [],
});
assert.equal(legacyWorkContext.hasBreakdown, false);
assert.equal(legacyWorkContext.sessionTokens, 25_572,
  "an old wrapper must retain the honest legacy total");
assert.equal(legacyWorkContext.sessionPercentage, 2.5572);

const freshWorkContext = workContextMetrics({
  v: 20, ts: 0, type: "context_report",
  total_tokens: 25_572, max_tokens: 1_000_000,
  percentage: 2.5572, session_tokens: 72, fixed_tokens: 25_500,
  session_percentage: 0.0072, categories: [],
});
assert.equal(freshWorkContext.hasBreakdown, true);
assert.equal(freshWorkContext.sessionTokens, 72);
assert.equal(freshWorkContext.fixedTokens, 25_500);
assert.equal(freshWorkContext.sessionPercentage, 0.0072);
assert.equal(freshWorkContext.totalPercentage, 2.5572);

const derivedWorkContext = workContextMetrics({
  v: 20, ts: 0, type: "context_report",
  total_tokens: 11_194, max_tokens: 353_400,
  percentage: 3.1675, fixed_tokens: 11_000, categories: [],
});
assert.equal(derivedWorkContext.sessionTokens, 194);
assert.ok(Math.abs(derivedWorkContext.sessionPercentage - 194 / 353_400 * 100) < 1e-9);

assert.equal(resolveSidebarSwipe(12, 200, 84, 205, 390, false), "open");
assert.equal(resolveSidebarSwipe(300, 200, 230, 205, 390, false), "close");
assert.equal(resolveSidebarSwipe(12, 100, 78, 240, 390, false), null,
  "a mostly vertical gesture must not navigate");
assert.equal(resolveSidebarSwipe(12, 200, 84, 205, 390, true), null,
  "an interactive vertical scroller must own its gesture");
assert.equal(clampPanelWidth(200, 1440), 360);
assert.equal(clampPanelWidth(2_000, 1440), 1_020);
assert.equal(clampPanelWidth(600, 1_000), 580);

assert.equal(isComposerBusy("idle"), false);
assert.equal(isComposerBusy("running"), true);
assert.equal(isComposerBusy("interrupting"), true);
assert.equal(isComposerBusy("draining"), true);
assert.equal(isInterruptSettling("running"), false);
assert.equal(isInterruptSettling("interrupting"), true);
assert.equal(isInterruptSettling("draining"), true);
assert.equal(isSettlingStopDisabled("running", false), false);
assert.equal(isSettlingStopDisabled("interrupting", false), true);
assert.equal(isSettlingStopDisabled("draining", false), true);
assert.equal(isSettlingStopDisabled("interrupting", true), false);
assert.equal(classifyBusySubmit("running", "steer", "codex", true), "steer",
  "the default Codex busy submit appends input to its active native turn");
assert.equal(
  classifyBusySubmit("running", "steer", "claude", true),
  "interrupt-and-replace",
  "Claude retains interrupt-and-replace because it cannot steer an active turn",
);
assert.equal(classifyBusySubmit(
  "interrupting", "steer", "codex", true), "replace",
  "input submitted while an interrupt settles replaces the pending follow-up");
assert.equal(classifyBusySubmit(
  "draining", "steer", "claude", true), "replace",
  "the settling replacement contract is engine-independent");
assert.equal(classifyBusySubmit(
  "running", "queue", "codex", true), "enqueue",
  "explicit queue mode remains an enqueue even when Codex could steer");
for (const engine of ["claude", "codex"] as const) {
  for (const state of ["running", "interrupting", "draining"] as const) {
    assert.equal(classifyBusySubmit(state, "steer", engine, false), "noop",
      "empty Enter is a no-op; Stop remains a separate explicit control");
  }
}

const loginFormSource = readFileSync(resolve(
  process.cwd(), "src/components/LoginForm.tsx"), "utf8");
assert.match(loginFormSource, /type=\{passwordVisible \? "text" : "password"\}/);
assert.match(loginFormSource, /name="password"/);
assert.match(loginFormSource, /autoComplete="current-password"/);
assert.match(loginFormSource, /autoCapitalize="none"/);
assert.match(loginFormSource, /autoCorrect="off"/);
assert.match(loginFormSource, /spellCheck=\{false\}/);
assert.match(loginFormSource, /enterKeyHint="go"/);
assert.match(loginFormSource, /aria-label=\{passwordVisible \? "隐藏密码" : "显示密码"\}/);
assert.match(loginFormSource, /aria-pressed=\{passwordVisible\}/);
assert.match(loginFormSource, /selectionStart/);
assert.match(loginFormSource, /selectionEnd/);
assert.match(loginFormSource, /focus\(\{ preventScroll: true \}\)/);
assert.match(loginFormSource, /setSelectionRange\(/,
  "password visibility toggles must restore the desktop caret and selection");
assert.match(loginFormSource,
  /onPointerDown=\{\(event\) => \{\s*rememberPasswordSelection\(\);/,
  "the caret must be captured before a desktop browser handles pointer focus");
assert.match(loginFormSource, /requestAnimationFrame\(\(\) => \{\s*restoreSelection\(\);/,
  "the caret must be restored after the browser commits the input type change");

const reconnectBannerSource = readFileSync(resolve(
  process.cwd(), "src/components/ReconnectBanner.tsx"), "utf8");
assert.ok(reconnectBannerSource.includes('busy && <span className="sp"'),
  "only an active reconnect/replay may render the progress indicator");

const historyAppSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
const cacheSource = readFileSync(resolve(process.cwd(), "src/cache.ts"), "utf8");
const autoCompactControlSource = readFileSync(resolve(
  process.cwd(), "src/components/AutoCompactControl.tsx"), "utf8");
assert.equal(HISTORY_INITIAL_PAGE, 4,
  "the newest history page must stay small enough for an immediate first paint");
assert.equal(HISTORY_MORE_PAGE, 12,
  "older history must be delivered in bounded follow-up pages");
const historyBeforeResume = historyAppSource.match(
  /requestHistory\([\s\S]{0,160}?HISTORY_INITIAL_PAGE\);[\s\S]{0,160}?(?:resumeListedSession|sendSwitchSession)/g,
) ?? [];
assert.equal(historyBeforeResume.length, 4,
  "every existing-session activation, including unarchive, must paint before resume");
assert.match(historyAppSource,
  /const resumeListedSession[\s\S]{0,420}session\.tag === "archived"[\s\S]{0,220}return;[\s\S]{0,180}sendSwitchSession/,
  "opening archived history must not resume its native writer");
assert.match(historyAppSource,
  /const archivedBrowse = archivedBrowseRef\.current;[\s\S]{0,700}sendSwitchSession/,
  "an authoritative unarchive must resume a focused read-only session");
assert.match(historyAppSource,
  /const archivedBrowse = focusedSession\?\.tag === "archived";/,
  "the focused archived row must define one shared read-only boundary");
assert.match(historyAppSource,
  /focusedGoalScopeKey[\s\S]{0,120}\|\| archivedBrowse[\s\S]{0,500}sendGetGoalTo/,
  "archived history must not auto-load live Goal state");
assert.match(historyAppSource,
  /const refreshStatus = useCallback[\s\S]{0,300}\?\.tag === "archived"[\s\S]{0,220}sendGetStatus/,
  "archived history must not request live Codex status");
assert.match(historyAppSource,
  /!archivedBrowse && focusedEngine !== "dsh" && \(\s*<TerminalControl/,
  "archived history must not expose terminal takeover controls");
assert.match(historyAppSource,
  /<GoalPanel[\s\S]{0,180}revealed=\{!archivedBrowse[\s\S]{0,100}open=\{!archivedBrowse/,
  "archived history must hide Goal mutations while retaining history plans");
assert.match(historyAppSource,
  /const requestContext = \(\) => \{[\s\S]{0,120}archivedBrowseRef\.current === focusedSid/,
  "archived history must not request live context state");
assert.match(historyAppSource,
  /const forkFromTurn = \(forkPointId: string\) => \{[\s\S]{0,180}archivedBrowseRef\.current === focusedSid/,
  "a stale message action must not fork an archived session");
assert.match(historyAppSource,
  /onFork=\{!historyView\.recovering && !archivedBrowse[\s\S]{0,80}space === "code"/,
  "archived history must not render message-level fork actions");
assert.match(historyAppSource,
  /current\.sessions\.find\([\s\S]{0,160}\?\.tag === "archived"\) return;[\s\S]{0,120}sendGetGoalTo/,
  "a delayed Goal retry must stop after the session becomes archived");
assert.match(historyAppSource,
  /if \(msg\.type === "session_list" && !ownership\) return;[\s\S]*historySessionListsRef/,
  "only an ownership-accepted SessionList may seed a Claude history cwd hint");
assert.match(historyAppSource,
  /resolveHistoryCwdHint\(historySessionListsRef\.current, sid\)/,
  "every initial, reconnect, invalidation and pagination read shares the accepted cwd hint");
assert.match(historyAppSource, /history_invalidated[\s\S]*invalidateSessionCache/,
  "history invalidation must evict the matching IndexedDB row");
assert.match(historyAppSource, /historyCacheEpochRef[\s\S]*loadSession/,
  "an IndexedDB read started before the marker must be rejected");
assert.match(historyAppSource, /history_invalidated[\s\S]*requestHistory/,
  "a visible invalidated session must request authoritative history");
assert.match(historyAppSource,
  /msg\.type === "history" && msg\.authoritative !== false[\s\S]*allowSessionCache/,
  "a failed non-authoritative History must not reopen the IndexedDB cache barrier");
assert.match(historyAppSource,
  /const displayRecoveryMatches = !isHistoryRecoveryPending\([\s\S]{0,240}historyConfirmsRecovery/,
  "cache recovery must distinguish a display candidate from a confirmation");
assert.match(historyAppSource,
  /const runtimeRecoveryMatches =[\s\S]{0,240}!isRuntimeHistoryRecoveryPending\([\s\S]{0,240}historyConfirmsRuntimeRecovery/,
  "cache recovery must preserve the per-runtime barrier across focus changes");
assert.match(historyAppSource,
  /displayRecoveryMatches[\s\S]{0,240}runtimeRecoveryMatches[\s\S]{0,240}buildIsCurrent[\s\S]{0,500}allowSessionCache/,
  "the first matching recovery build must remain behind both cache barriers");
assert.match(historyAppSource,
  /historyRequestsRef\.current\.complete\(msg\)[\s\S]{0,5000}historyNeedsConfirmationRequest\([\s\S]{0,500}requestHistory\(\s*msg\.session_id,\s*undefined,\s*HISTORY_INITIAL_PAGE,\s*msg\.generation/,
  "a candidate must release coordinator dedupe before requesting its generation-bound confirmation");
assert.match(historyAppSource,
  /msg\.error == null && !msg\.before[\s\S]{0,180}HISTORY_PROVISIONAL_WATCHDOG_MS[\s\S]{0,600}recoverableReads\.retry\([\s\S]{0,700}retryDelay\)/,
  "a moving newest-page preview must use one slow fallback instead of a parallel 250ms rescan");
assert.match(historyAppSource, /buildIsCurrent[\s\S]{0,900}allowSessionCache/,
  "a lower same-generation build must not reopen cache before reducer acceptance");
assert.match(historyAppSource, /artifact_invalidated[\s\S]*sendGetWorkArtifacts/,
  "file rollback must refresh the Work artifact inventory");
assert.match(historyAppSource,
  /const effectiveState = mergeSessionActivityState\([\s\S]*focusedSessionState/,
  "catalog or mirrored native work must paint a running header without granting a local Stop action");
assert.match(historyAppSource,
  /liveStates=\{Object\.fromEntries\(state\.sessions\.map[\s\S]*mergeSessionActivityState/,
  "catalog or mirrored native work must paint a running session badge in the sidebar");
const rewindChatViewSource = readFileSync(resolve(
  process.cwd(), "src/components/ChatView.tsx"), "utf8");
const rewindComposerSource = readFileSync(resolve(
  process.cwd(), "src/components/Composer.tsx"), "utf8");
assert.equal(commandsFor("claude").some((command) => (
  "slash" in command && command.slash === "rewind"
)), false, "Claude rewind must stay hidden from the command palette");
assert.equal(commandsFor("codex").some((command) => (
  "slash" in command && command.slash === "rollback"
)), false, "Codex rollback must stay hidden from the command palette");
const claudeWorkSlashes = commandsFor("claude", "work")
  .filter((command) => "slash" in command)
  .map((command) => command.slash);
const codexWorkSlashes = commandsFor("codex", "work")
  .filter((command) => "slash" in command)
  .map((command) => command.slash);
assert.equal(codexWorkSlashes.includes("fast"), true,
  "Codex Work must expose its supported Fast service tier");
for (const slash of ["model", "goal", "btw", "preview", "context", "clear"]) {
  assert.equal(claudeWorkSlashes.includes(slash), true, `Work must retain /${slash}`);
}
assert.equal(claudeWorkSlashes.includes("permissions"), false,
  "Work permissions are fixed by its private workspace and must not be presented as editable");
assert.equal(isKnownCodeOnlySlash("permissions", "claude"), true);
assert.equal(isKnownCodeOnlySlash("permissions", "codex"), true);
for (const slash of ["plan", "code-review", "security-review", "verify", "simplify", "run", "init", "diff"]) {
  assert.equal(claudeWorkSlashes.includes(slash), false, `Work must hide Code /${slash}`);
  assert.equal(isKnownCodeOnlySlash(slash, "claude"), true);
}
for (const slash of ["review", "init", "plan", "status", "compact", "rollback"]) {
  assert.equal(codexWorkSlashes.includes(slash), false, `Work must hide Codex Code /${slash}`);
  assert.equal(isKnownCodeOnlySlash(slash, "codex"), true);
}
assert.equal(isKnownCodeOnlySlash("my-personal-skill", "claude"), false,
  "unknown user skills must remain available in Work");
for (const engine of ["claude", "codex"] as const) {
  assert.equal(commandsFor(engine).some((command) => (
    "slash" in command && command.slash === "diff"
  )), true, `/diff must be discoverable for ${engine} Code`);
  assert.equal(clientSlashesFor(engine).has("diff"), true,
    `/diff must remain local for ${engine}`);
}
assert.match(rewindComposerSource, /case "diff":[\s\S]{0,160}p\.onDiff\?\.\(\)/,
  "/diff must open the local diff panel instead of becoming a model prompt");
assert.match(historyAppSource, /onDiff=\{\(\) => getDiff\(""\)\}/,
  "the composer /diff callback must reuse the existing latest-diff request path");
assert.deepEqual(matchCommands("", "claude", "work").map((command) => command.slash),
  claudeWorkSlashes);
assert.deepEqual(matchCommands("pla", "codex", "work"), []);
assert.match(historyAppSource,
  /command\.kind === "resume"[\s\S]{0,300}focusedEngine === "codex"[\s\S]{0,160}sendSetGoal\(null, "active", null\)/,
  "Codex /goal resume must reactivate the existing Goal without replacing its objective");
assert.match(historyAppSource,
  /const completedGoalRequest = msg\.request_id[\s\S]{0,300}goalRecoveryRequestsRef\.current\.delete\([\s\S]{0,120}completedGoalRequest\.scopeKey/,
  "a successful Goal recovery must release its focus-scoped dedupe key");
assert.match(historyAppSource,
  /legacyDismissed[\s\S]{0,500}sendDismissGoalTo\(/,
  "a device-local Goal dismissal must migrate into shared wrapper state");
assert.match(historyAppSource,
  /completionAcknowledgementId\([\s\S]{0,320}sendAcknowledgeCompletionTo\(/,
  "viewing a completed session must acknowledge it through the wrapper");
const codexGoalDescription = commandsFor("codex").flatMap((command) =>
  "slash" in command && command.slash === "goal" ? [command.ds] : [])[0] ?? "";
assert.match(codexGoalDescription, /\/goal resume/,
  "the Codex command palette must advertise /goal resume");
assert.doesNotMatch(historyAppSource, /openClaudeRewind|openLatestClaudeRewind/,
  "Claude rewind must not have an App entry point while unsupported");
assert.doesNotMatch(rewindChatViewSource, /onRewind|回到这里/,
  "Claude messages must not expose a rewind action while unsupported");
assert.match(rewindComposerSource, /case "rewind": flash\("Claude Rewind 暂未开放"\)/,
  "a manually typed hidden rewind command must be blocked locally");
assert.match(rewindComposerSource, /case "rollback": flash\("Codex Rollback 暂未开放"\)/,
  "a manually typed hidden Codex rollback command must be blocked locally");
assert.match(historyAppSource, /replay_start[\s\S]*requestHistory/,
  "a replay gap must request authoritative history instead of ending on an empty view");
assert.match(historyAppSource, /replay_start[\s\S]*setWorkArtifactsBySid/,
  "a replay gap must discard a possibly stale Work artifact inventory");
assert.match(historyAppSource,
  /displayHistoryProjection\(\s*state\.historyRecovery,\s*focusedSid,\s*rt,\s*state\.historyBrowse,\s*state\.retainedHistoryBrowse,\s*\)/,
  "ChatView must select retained recovery, browse, then live runtime without rebuilding rt.turns");
assert.match(historyAppSource,
  /<ChatView key=\{`\$\{activeScopeKey\}\\u0000\$\{focusedSid \?\? ""\}`\}/,
  "session switches must remount ChatView so scroll and disclosure state cannot leak between sessions");
assert.match(historyAppSource,
  /new HistoryPageCache\(\)/,
  "deep-history pages must use their independent best-effort IndexedDB");
assert.match(historyAppSource,
  /new HistoryDetailRequestCoordinator\([\s\S]{0,200}history_detail_cancelled/,
  "turn detail responses need a frozen target with bounded loading recovery");
assert.match(historyAppSource,
  /Any view navigation revokes[\s\S]{0,220}clearHistoryDetailRequests\(\);[\s\S]{0,180}focusedSid/,
  "surface/session navigation must release frozen detail loading rows");
assert.match(historyAppSource,
  /return \(\) => \{[\s\S]{0,260}historyRequests\.clear\(\);\s*clearHistoryDetailRequests\(\);/,
  "WebSocket cleanup must release frozen detail loading rows");
assert.match(historyAppSource,
  /const completedHistory\s*=\s*historyRequestsRef\.current\.complete\(msg\);[\s\S]{0,120}const browseWaiters\s*=\s*completedHistory\.matched/,
  "History.before must consume request-time browse authority instead of current UI state");
assert.match(historyAppSource,
  /await historyPageCacheRef\.current\.getPage[\s\S]{0,400}acceptsCachedNewerPage\(current, frozen\)/,
  "a deferred cached-newer read must revalidate the current browse authority");
assert.match(historyAppSource,
  /if \(msg\.before\) \{[\s\S]{0,2800}History\.before is display-only[\s\S]{0,220}return;/,
  "every History.before path must terminate before the generic live reducer");
assert.match(historyAppSource,
  /browseMode=\{historyView\.browsing\}/);
assert.match(historyAppSource,
  /historyViewId=\{historyView\.viewId\}/);
assert.match(historyAppSource,
  /onLoadNewer=\{loadNewerHistoryPage\}/);
assert.match(historyAppSource,
  /onReturnLatest=\{returnToLatestHistory\}/);
assert.match(historyAppSource,
  /historyInvalidationsRef\.current\.has\(sid\)[\s\S]{0,160}isHistoryRecoveryPending/,
  "cache writes must remain blocked while a display recovery is pending");
assert.match(historyAppSource,
  /historyViewRevision=\{historyView\.viewRevision\}/,
  "an atomic recovery replacement must retain the prior virtual-scroll scope");
assert.match(historyAppSource,
  /replaying=\{rt\.replaying \|\| historyView\.recovering\}/,
  "the retained projection must remain visibly marked as synchronizing");
assert.match(historyAppSource,
  /onEdit=\{historyView\.recovering\s*\? undefined/,
  "display-only recovery turns must not expose edit/resend callbacks");
assert.match(historyAppSource,
  /onOpenFile=\{historyView\.recovering \? undefined/,
  "display-only recovery turns must not issue file reads");
assert.match(historyAppSource,
  /onLoadHistoryImage=\{historyView\.recovering\s*\? undefined/,
  "display-only recovery turns must not issue history-image reads");
assert.match(historyAppSource,
  /loadRuntimeTurnDetail\(request\.turnId, request\.before, false\)/,
  "active-turn recovery must install one bounded detail page without cascading");
assert.match(historyAppSource,
  /requestHistoryTurnDetail = useCallback\([\s\S]{0,120}autoLoad = false/,
  "every detail entry point must default to one bounded page");
assert.match(cacheSource, /const CACHE_VER = 26/,
  "async-question repair must invalidate browser summaries missing unphased replies");
assert.match(cacheSource, /objectStore\(STORE\)\.delete\(sessionId\)/);
assert.match(cacheSource, /job\.epoch !== sessionEpoch\(job\.sid\)/,
  "a debounced pre-marker write must not recreate the deleted cache row");
assert.match(autoCompactControlSource,
  /设置已应用；上下文用量将在下一次可靠读取后更新。/,
  "an applied setting without a fresh context report must not claim it is pending");

const viewportListeners = new Map<MobileViewportEvent, Set<() => void>>();
const viewportCss = new Map<string, string>();
const viewportAttributes = new Map<string, string>();
const viewportFrames = new Map<number, () => void>();
const viewportDelays = new Map<number, () => void>();
let nextViewportTaskId = 1;
let viewportReading: ViewportReading = {
  height: 844, layoutHeight: 844, offsetTop: 0, scale: 1,
};
let editableFocused = false;
let layoutScrollResets = 0;
const viewportBindings: MobileViewportBindings = {
  readViewport: () => viewportReading,
  setCssProperty: (name, value) => { viewportCss.set(name, value); },
  clearCssProperty: (name) => { viewportCss.delete(name); },
  setRootAttribute: (name, value) => { viewportAttributes.set(name, value); },
  clearRootAttribute: (name) => { viewportAttributes.delete(name); },
  listen: (event, listener) => {
    const listeners = viewportListeners.get(event) ?? new Set();
    listeners.add(listener);
    viewportListeners.set(event, listeners);
    return () => listeners.delete(listener);
  },
  requestFrame: (listener) => {
    const id = nextViewportTaskId++;
    viewportFrames.set(id, listener);
    return id;
  },
  cancelFrame: (id) => { viewportFrames.delete(id); },
  setDelay: (listener) => {
    const id = nextViewportTaskId++;
    viewportDelays.set(id, listener);
    return id;
  },
  clearDelay: (id) => { viewportDelays.delete(id); },
  isEditableFocused: () => editableFocused,
  resetLayoutScroll: () => { layoutScrollResets += 1; },
};
const flushViewportFrames = () => {
  const frames = [...viewportFrames.values()];
  viewportFrames.clear();
  for (const frame of frames) frame();
};
const emitViewport = (event: MobileViewportEvent) => {
  for (const listener of viewportListeners.get(event) ?? []) listener();
};

const stopViewportSync = createMobileViewportSync(viewportBindings);
assert.equal(viewportCss.get("--app-height"), "844px");
assert.equal(viewportCss.get("--app-offset-top"), "0px");
assert.equal(viewportCss.get("--keyboard-inset"), "0px");
assert.equal(viewportAttributes.get("data-short-viewport"), "false");

viewportReading = { height: 510.25, layoutHeight: 844, offsetTop: 0, scale: 1 };
emitViewport("viewport-resize");
assert.equal(viewportCss.get("--app-height"), "844px");
flushViewportFrames();
assert.equal(viewportCss.get("--app-height"), "510.25px");
assert.equal(viewportCss.get("--keyboard-inset"), "333.75px");
assert.equal(viewportAttributes.get("data-short-viewport"), "true");

viewportReading = { height: 500, layoutHeight: 844, offsetTop: 24, scale: 1 };
emitViewport("viewport-scroll");
flushViewportFrames();
assert.equal(viewportCss.get("--app-height"), "500px");
assert.equal(viewportCss.get("--app-offset-top"), "24px");
assert.equal(viewportCss.get("--keyboard-inset"), "320px");

// Pinch zoom remains user-controlled and must not be treated as a keyboard.
viewportReading = { height: 420, layoutHeight: 844, offsetTop: 30, scale: 1.5 };
emitViewport("viewport-resize");
flushViewportFrames();
assert.equal(viewportCss.get("--app-offset-top"), "0px");
assert.equal(viewportCss.get("--keyboard-inset"), "0px");
assert.equal(viewportAttributes.get("data-short-viewport"), "false");

// Safari can report its pre-blur viewport for a few animation frames. Delayed
// settling rereads it and clears only the layout-level focus pan.
viewportReading = { height: 844, layoutHeight: 844, offsetTop: 0, scale: 1 };
emitViewport("focus-out");
assert.equal(viewportDelays.size, 2);
flushViewportFrames();
for (const delayed of [...viewportDelays.values()]) delayed();
viewportDelays.clear();
flushViewportFrames();
assert.equal(layoutScrollResets, 2);
assert.equal(viewportCss.get("--app-height"), "844px");

editableFocused = true;
emitViewport("orientation-change");
for (const delayed of [...viewportDelays.values()]) delayed();
viewportDelays.clear();
flushViewportFrames();
assert.equal(layoutScrollResets, 2);
editableFocused = false;

// Repeated keyboard cycles must always settle back to the full viewport instead
// of accumulating height, offset, or bottom-inset drift.
for (let cycle = 0; cycle < 10; cycle += 1) {
  viewportReading = { height: 508, layoutHeight: 844, offsetTop: 18, scale: 1 };
  emitViewport("viewport-resize");
  flushViewportFrames();
  viewportReading = { height: 844, layoutHeight: 844, offsetTop: 0, scale: 1 };
  emitViewport("focus-out");
  flushViewportFrames();
  for (const delayed of [...viewportDelays.values()]) delayed();
  viewportDelays.clear();
  flushViewportFrames();
  assert.equal(viewportCss.get("--app-height"), "844px");
  assert.equal(viewportCss.get("--app-offset-top"), "0px");
  assert.equal(viewportCss.get("--keyboard-inset"), "0px");
}
assert.equal(layoutScrollResets, 22);

stopViewportSync();
assert.equal(viewportCss.has("--app-height"), false);
assert.equal(viewportCss.has("--app-offset-top"), false);
assert.equal(viewportCss.has("--keyboard-inset"), false);
assert.equal(viewportAttributes.has("data-short-viewport"), false);
assert.equal(viewportFrames.size, 0);
assert.equal(viewportDelays.size, 0);
for (const listeners of viewportListeners.values()) assert.equal(listeners.size, 0);

assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), true);
assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: true, isComposing: false, keyCode: 13,
}), false);
assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: false, isComposing: true, keyCode: 13,
}), false);
assert.equal(shouldSubmitTextKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 229,
}), false);
assert.equal(shouldSubmitTextKey({
  key: "Space", shiftKey: false, isComposing: false, keyCode: 32,
}), false);

const imeSubmit = new ImeSubmitGuard();
assert.equal(imeSubmit.shouldSubmitKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), true);
imeSubmit.startComposition();
assert.equal(imeSubmit.shouldSubmitKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), false);
assert.equal(imeSubmit.shouldCommitBeforeButtonSubmit(), true);
imeSubmit.endComposition();
assert.equal(imeSubmit.shouldSubmitKey({
  key: "Enter", shiftKey: false, isComposing: false, keyCode: 13,
}), true);
assert.equal(imeSubmit.shouldCommitBeforeButtonSubmit(), false);

// Every text-entry surface that can submit outside the three composers must use
// the same IME guard. Browser-level composition ordering remains an E2E release
// gate; this integration check prevents a raw Enter handler from bypassing the
// shared path before that suite runs.
for (const filename of [
  "QuestionSheet.tsx",
  "SessionsSidebar.tsx",
  "DirPicker.tsx",
  "LoginForm.tsx",
  "ForkWorktreeSheet.tsx",
]) {
  const source = readFileSync(resolve(process.cwd(), "src/components", filename), "utf8");
  assert.match(source, /useImeSubmit/);
  assert.match(source, /onCompositionStart/);
  assert.match(source, /nativeEvent\.isComposing/);
  assert.match(source, /nativeEvent\.keyCode/);
}

// Desktop sidebar motion must not regress to the old discrete grid-track swap.
// Chromium could strand an animated 0px/1fr grid, so the safe implementation
// slides the fixed sidebar and offsets the pane using ordinary CSS lengths.
const layoutCss = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
assert.doesNotMatch(layoutCss, /transition\s*:\s*grid-template-columns/);
assert.match(layoutCss, /\.shell\.sidebar-open \.pane\s*\{[^}]*margin-left\s*:\s*352px/s);
assert.match(layoutCss, /\.pane\s*\{[^}]*transition\s*:[^}]*margin-left[^}]*width/s);
assert.match(layoutCss, /\.sessions\s*\{[^}]*position\s*:\s*fixed[^}]*width\s*:\s*352px/s);
assert.doesNotMatch(layoutCss, /\.scard\.has-profile-ribbon\s*\{[^}]*padding-top/,
  "profile stamps must not add a second row to every session card");
assert.doesNotMatch(layoutCss,
  /\.scard\.has-profile-ribbon \.scard-top\s*\{[^}]*padding-left/s,
  "profile keycaps must not push session titles into a second column");
assert.match(layoutCss,
  /\.scard\.has-profile-ribbon\s*\{[^}]*border-color\s*:\s*color-mix\([^}]*background\s*:\s*color-mix\(/s,
  "every profile keycap needs a quiet card edge to hang from");
assert.match(layoutCss,
  /:root:is\(\[data-engine="codex"\],\[data-engine="dsh"\]\)\[data-theme="dark"\] \.scard\.has-profile-ribbon\s*\{[^}]*background\s*:\s*color-mix\([^}]*border-color\s*:\s*var\(--border-strong\)/s,
  "dark profile cards need an opaque edge that survives the dark sidebar");
assert.match(layoutCss,
  /\.scard-profile-ribbon\s*\{[^}]*top\s*:\s*-6px[^}]*height\s*:\s*16px[^}]*max-width\s*:\s*64px[^}]*font-family\s*:\s*var\(--mono\)[^}]*font-size\s*:\s*8\.5px/s,
  "profile keycaps must hang compactly from the card edge");
assert.match(layoutCss,
  /\.work-profile-owner\s*\{[^}]*max-width\s*:\s*72px[^}]*height\s*:\s*19px[^}]*font\s*:\s*650 9px\/1 var\(--mono\)/s,
  "a multi-account Work owner stays compact in the shared header");
assert.match(layoutCss,
  /@media \(max-width:980px\)\{\s*\.artifact-panel\{[^}]*top:calc\(var\(--app-offset-top,0px\) \+ 10px\)[^}]*bottom:auto[^}]*height:calc\(var\(--app-height,100dvh\) - 20px\)[^}]*max-height:none/s,
  "mobile artifact previews must fill the visual viewport instead of shrink-wrapping iframe or Markdown content");

let requested = "";
const authenticated = await probeSession(async (input, init) => {
  requested = input;
  assert.equal(init.credentials, "same-origin");
  assert.equal(init.cache, "no-store");
  assert.equal(init.signal.aborted, false);
  return { ok: true, status: 200 };
}, 100);
assert.equal(requested, "/api/session");
assert.equal(authenticated, "authenticated");

const unauthorized = await probeSession(
  async () => ({ ok: false, status: 401 }), 100);
assert.equal(unauthorized, "unauthorized");
assert.equal(shouldReconnectAfterSessionProbe(unauthorized), false);

const serverError = await probeSession(
  async () => ({ ok: false, status: 503 }), 100);
assert.equal(serverError, "unavailable");
assert.equal(shouldReconnectAfterSessionProbe(serverError), true);

const networkError = await probeSession(async () => {
  throw new Error("offline");
}, 100);
assert.equal(networkError, "unavailable");
assert.equal(shouldReconnectAfterSessionProbe(networkError), true);

const timeout = await probeSession(
  async () => new Promise(() => {}), 1);
assert.equal(timeout, "unavailable");

const removed: string[] = [];
clearLegacyAuthMarkers({ removeItem: (key) => { removed.push(key); } });
assert.deepEqual(removed, ["cc_remote_session", "cc_remote_authenticated"]);

const sizedQuery = {
  prompt: "queued",
  files: [{ filename: "secret.txt", data: "sensitive-body" }],
};
const sizedQueryBytes = queuedQueryWireBytes(sizedQuery);
assert.equal(canEnqueueQuery([], sizedQuery, {
  maxCount: 32, maxBytes: sizedQueryBytes,
}), true);
assert.equal(canEnqueueQuery([], sizedQuery, {
  maxCount: 32, maxBytes: sizedQueryBytes - 1,
}), false);
assert.equal(canEnqueueQuery([sizedQuery], sizedQuery, {
  maxCount: 1, maxBytes: sizedQueryBytes * 3,
}), false);
assert.equal(canEnqueueQuery(
  [sizedQuery], sizedQuery, {
    maxCount: 32, maxBytes: sizedQueryBytes * 2,
  }), true);
assert.equal(canEnqueueQuery([], sizedQuery, {
  authoritativeCount: 1,
  authoritativeBytes: MAX_QUEUED_QUERY_BYTES,
}), false, "truncated server previews cannot undercount retained queue bytes");
assert.equal(canEnqueueQuery([], sizedQuery, {
  authoritativeCount: 1,
  authoritativeBytes: MAX_QUEUED_QUERY_BYTES,
  replacingCount: 1,
  replacingBytes: MAX_QUEUED_QUERY_BYTES,
}), true, "an authoritative replacement subtracts the exact retained item");
assert.deepEqual(collectUnconfirmedQueries({
  one: {
    queue: [
      { prompt: "queued", queueState: "queued" as const },
      { prompt: "submitting", queueState: "submitting" as const },
    ],
    pendingSend: {
      prompt: "replace-me", queueState: "submitting" as const,
    },
  },
  two: {
    queue: [],
    pendingSend: {
      prompt: "other-pending", queueState: "submitting" as const,
    },
  },
}, "one").map((query) => query.prompt), [
  "submitting", "other-pending",
]);

const a = {
  state: "idle", syncReady: true, pendingSend: null, queue: [{ prompt: "a" }],
  turns: [{ id: "a-old", prompt: "old" }],
};
const b = {
  state: "idle", syncReady: true, pendingSend: { prompt: "pending-b" },
  queue: [{ prompt: "b0" }, { prompt: "b1" }],
  turns: [{ id: "b-old", prompt: "old" }],
};
const runtimeMap = { a, b };
const withTurn = reduceTargetedRuntime(runtimeMap, "b", {
  type: "query_sent", turn: { id: "b-new", prompt: "new" },
});
assert.strictEqual(withTurn.a, a);
assert.deepEqual(withTurn.a.turns.map((turn) => turn.id), ["a-old"]);
assert.deepEqual(withTurn.b.turns.map((turn) => turn.id), ["b-old", "b-new"]);

const dequeued = reduceTargetedRuntime(withTurn, "b", { type: "dequeue_at", i: 0 });
assert.deepEqual(dequeued.a.queue.map((query) => query.prompt), ["a"]);
assert.deepEqual(dequeued.b.queue.map((query) => query.prompt), ["b1"]);

const cleared = reduceTargetedRuntime(dequeued, "b", { type: "clear_pending" });
assert.equal(cleared.a.pendingSend, null);
assert.equal(cleared.b.pendingSend, null);
assert.strictEqual(
  reduceTargetedRuntime(cleared, "missing", { type: "clear_pending" }),
  cleared,
);

const transcriptTurn = {
  id: "engine-id", prompt: "same prompt", done: true, ts: 1000,
  blocks: [{ kind: "text" as const, message_id: "engine-text", text: "answer", done: true }],
};
const optimisticTurn = {
  id: "client-id", historyTurnId: "engine-id",
  prompt: "same prompt", done: false, ts: 1100,
  blocks: [{ kind: "text" as const, message_id: "live-text", text: "answer tail", done: false }],
};
const laggingDone = {
  id: "client-lag", prompt: "not flushed", done: true, ts: 2000, blocks: [],
};
const mergedHistory = mergeInitialHistory(
  [transcriptTurn], [optimisticTurn, laggingDone]);
assert.deepEqual(mergedHistory.map((turn) => turn.id), ["client-id", "client-lag"]);
assert.equal(mergedHistory[0].done, true);
assert.equal(mergedHistory[0].blocks.length, 1);
assert.equal(mergedHistory[0].historyTurnId, "engine-id",
  "an attachment-free optimistic alias retains its native history lookup id");
assert.equal(mergedHistory[1].prompt, "not flushed");

const nativeAliasMerged = mergeInitialHistory(
  [{
    id: "native-alias", prompt: "same prompt", done: true,
    blocks: [{
      kind: "text" as const, message_id: "native-alias-final",
      text: "answer", done: true, channel: "final" as const,
    }],
  }],
  [{
    id: "client-alias", historyTurnId: "native-alias",
    prompt: "same prompt", done: false,
    blocks: [{
      kind: "text" as const, message_id: "live-alias-final",
      text: "answer", done: false, channel: "final" as const,
    }],
  }],
);
assert.deepEqual(
  nativeAliasMerged.map((turn) => turn.id),
  ["client-alias"],
  "historyTurnId is an exact native/client identity even without timestamps",
);

const compactNativeTurnId = "native-compact-turn";
const compactBoundaryMerged = mergeInitialHistory(
  [{
    id: "history-compact-user",
    forkPointId: compactNativeTurnId,
    prompt: "修复问题",
    done: false,
    blocks: [],
  }],
  [{
    id: "browser-compact-turn",
    prompt: "修复问题",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "live-context-compaction",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
);
assert.deepEqual(
  compactBoundaryMerged.map((turn) => turn.id),
  ["browser-compact-turn"],
  "a native compaction binding must reconcile the live row with History",
);
assert.equal(
  compactBoundaryMerged[0].blocks.some((block) =>
    block.kind === "process" && block.processKind === "compaction"),
  true,
  "reconciling a compact boundary must keep its process marker",
);
const rolloutCompactBoundaryMerged = mergeInitialHistory(
  [{
    id: "rollout-compact-user",
    prompt: "修复问题",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "rollout-context-compaction",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
  [{
    id: "browser-rollout-compact-user",
    prompt: "修复问题",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "live-rollout-context-compaction",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
);
assert.equal(rolloutCompactBoundaryMerged.length, 1,
  "rollout and live compaction markers bind one visible user row");
assert.equal(
  rolloutCompactBoundaryMerged[0].blocks.filter((block) =>
    block.kind === "process" && block.processKind === "compaction").length,
  1,
  "one compaction observed through rollout and live sources stays one process",
);
const compactDoesNotCrossSteer = mergeInitialHistory(
  [{
    id: "history-before-steer",
    forkPointId: compactNativeTurnId,
    prompt: "first direction",
    done: true,
    blocks: [],
  }, {
    id: "history-after-steer",
    forkPointId: compactNativeTurnId,
    prompt: "second direction",
    done: false,
    blocks: [],
  }],
  [{
    id: "browser-after-steer",
    prompt: "second direction",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "compact-after-steer",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
);
assert.deepEqual(
  compactDoesNotCrossSteer.map((turn) => [turn.id, turn.prompt]),
  [
    ["history-before-steer", "first direction"],
    ["browser-after-steer", "second direction"],
  ],
  "a compact task alias must not merge a later steer into an older prompt",
);
const repeatedCompactSteer = mergeInitialHistory(
  [{
    id: "history-repeated-before-steer",
    prompt: "继续",
    done: true,
    blocks: [{
      kind: "process" as const,
      item_id: "history-repeated-old-compact",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }, {
    id: "history-repeated-after-steer",
    forkPointId: compactNativeTurnId,
    prompt: "继续",
    done: false,
    blocks: [],
  }],
  [{
    id: "browser-repeated-after-steer",
    prompt: "继续",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "live-repeated-new-compact",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
  { preserveLiveTailOpen: true },
);
assert.deepEqual(
  repeatedCompactSteer.map((turn) => ({
    id: turn.id,
    historyTurnId: turn.historyTurnId,
    done: turn.done,
  })),
  [{
    id: "history-repeated-before-steer",
    historyTurnId: undefined,
    done: true,
  }, {
    id: "browser-repeated-after-steer",
    historyTurnId: "history-repeated-after-steer",
    done: false,
  }],
  "a repeated steer prompt binds the newest compatible compact segment",
);
const repeatedCompactionEvents = mergeInitialHistory(
  [{
    id: "history-two-compactions",
    prompt: "long task",
    done: false,
    blocks: [
      {
        kind: "process" as const,
        item_id: "history-compact-one",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
      {
        kind: "process" as const,
        item_id: "history-compact-two",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
    ],
  }],
  [{
    id: "browser-two-compactions",
    prompt: "long task",
    done: false,
    blocks: [
      {
        kind: "process" as const,
        item_id: "live-compact-one",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
      {
        kind: "process" as const,
        item_id: "live-compact-two",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
    ],
  }],
);
assert.equal(
  repeatedCompactionEvents[0].blocks.filter((block) =>
    block.kind === "process" && block.processKind === "compaction").length,
  2,
  "two real compactions stay visible when both sources observed both events",
);
const liveHasNewerCompaction = mergeInitialHistory(
  [{
    id: "history-one-compaction",
    prompt: "long task",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "history-known-compact",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
  [{
    id: "browser-one-history-two-live-compactions",
    prompt: "long task",
    done: false,
    blocks: [
      {
        kind: "process" as const,
        item_id: "live-known-compact",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
      {
        kind: "process" as const,
        item_id: "live-newer-compact",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
    ],
  }],
);
assert.deepEqual(
  liveHasNewerCompaction[0].blocks.flatMap((block) =>
    block.kind === "process" && block.processKind === "compaction"
      ? [block.item_id] : []),
  ["live-known-compact", "live-newer-compact"],
  "a live-only newer compaction remains visible after the shared occurrence",
);
const historyHasOlderCompaction = mergeInitialHistory(
  [{
    id: "history-two-live-one-compactions",
    prompt: "long task",
    done: false,
    blocks: [
      {
        kind: "process" as const,
        item_id: "history-older-compact",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
      {
        kind: "process" as const,
        item_id: "history-shared-compact",
        processKind: "compaction" as const,
        phase: "end" as const,
        status: "succeeded" as const,
        turn_id: compactNativeTurnId,
        title: "压缩上下文",
        done: true,
      },
    ],
  }],
  [{
    id: "browser-two-history-one-live-compaction",
    prompt: "long task",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "live-shared-compact",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactNativeTurnId,
      title: "压缩上下文",
      done: true,
    }],
  }],
);
assert.deepEqual(
  historyHasOlderCompaction[0].blocks.flatMap((block) =>
    block.kind === "process" && block.processKind === "compaction"
      ? [block.item_id] : []),
  ["history-older-compact", "live-shared-compact"],
  "a live tail pairs with the latest history occurrence without hiding older compaction",
);

function testCompactionOrphanHelpers() {
const orphanCompactionBlock = {
  kind: "process" as const,
  item_id: "orphan-compaction",
  processKind: "compaction" as const,
  phase: "end" as const,
  status: "succeeded" as const,
  turn_id: compactNativeTurnId,
  title: "压缩上下文",
  done: true,
};
const compactionOrphanOwner: Turn = {
  id: "bound-compaction-user",
  clientMsgId: "bound-compaction-client",
  prompt: "继续修复问题",
  blocks: [{
    kind: "text", message_id: "bound-compaction-final",
    channel: "final", text: "已经修好", done: true,
  }],
  done: true,
  doneTs: 22_000,
  durationMs: 1_200,
};
const compactionOnlyOrphan: Turn = {
  id: "orphan-compaction-row",
  prompt: "",
  blocks: [orphanCompactionBlock],
  done: false,
};
const directOrphanRepaired = reconcileCompactionOnlyOrphan(
  [compactionOnlyOrphan, compactionOrphanOwner],
  ["bound-compaction-client"],
  compactNativeTurnId,
);
assert.equal(directOrphanRepaired.length, 1,
  "one exact binding absorbs one compaction-only live row");
assert.deepEqual({
  id: directOrphanRepaired[0].id,
  done: directOrphanRepaired[0].done,
  doneTs: directOrphanRepaired[0].doneTs,
  durationMs: directOrphanRepaired[0].durationMs,
}, {
  id: "bound-compaction-user", done: true, doneTs: 22_000, durationMs: 1_200,
}, "moving a compact marker cannot inherit the orphan lifecycle");
assert.deepEqual(
  directOrphanRepaired[0].blocks.map((block) => block.kind === "process"
    ? block.item_id : block.message_id),
  ["orphan-compaction", "bound-compaction-final"],
  "a newly bound compact marker renders before the answer rather than as a trailing 已处理 row",
);
assert.deepEqual(
  directOrphanRepaired[0].blocks.map((block) => block.liveOrder),
  [0, 1],
  "moving a live compact marker rebases chronology without duplicate orders",
);
assert.deepEqual(
  reconcileCompactionOnlyOrphan(
    directOrphanRepaired,
    ["bound-compaction-client"],
    compactNativeTurnId,
  ),
  directOrphanRepaired,
  "repeated binding reconciliation is idempotent",
);

const spilledCompactionOwner: Turn = {
  ...compactionOrphanOwner,
  id: "spilled-compaction-owner",
  clientMsgId: "spilled-compaction-client",
  blocks: [{ ...compactionOrphanOwner.blocks[0], liveOrder: 1 }],
  liveBlocksSpilled: true,
  liveSpillBlocks: [{
    kind: "process", item_id: "spilled-before-compaction",
    processKind: "command", phase: "end", status: "succeeded",
    title: "运行命令", done: true, liveOrder: 0,
  }],
  nextLiveBlockOrder: 2,
};
const spilledCompactionRepaired = reconcileCompactionOnlyOrphan(
  [compactionOnlyOrphan, spilledCompactionOwner],
  ["spilled-compaction-client"],
  compactNativeTurnId,
)[0];
assert.deepEqual([
  spilledCompactionRepaired.blocks.map((block) => block.liveOrder),
  spilledCompactionRepaired.liveSpillBlocks?.map((block) => block.liveOrder),
  spilledCompactionRepaired.nextLiveBlockOrder,
], [[0, 2], [1], 3],
"live compaction insertion shifts the bounded spill archive without collisions");
assert.deepEqual(
  mergeDetailWithLiveTail(
    spilledCompactionRepaired.liveSpillBlocks ?? [],
    spilledCompactionRepaired.blocks,
  ).map((block) => block.kind === "text"
    ? block.message_id : block.kind === "tool"
      ? block.tool_use_id : block.item_id),
  ["orphan-compaction", "spilled-before-compaction", "bound-compaction-final"],
  "the final spill/live render follows the repaired cross-slice chronology",
);

const ambiguousCompactionOrphans = reconcileCompactionOnlyOrphan([
  compactionOrphanOwner,
  compactionOnlyOrphan,
  {
    ...compactionOnlyOrphan,
    id: "second-orphan-compaction-row",
    blocks: [{ ...orphanCompactionBlock, item_id: "second-orphan-compaction" }],
  },
], ["bound-compaction-client"], compactNativeTurnId);
assert.equal(ambiguousCompactionOrphans.length, 3,
  "two compaction-only candidates fail closed instead of guessing an owner");
assert.equal(reconcileCompactionOnlyOrphan([
  compactionOrphanOwner,
  { ...compactionOnlyOrphan, done: true },
], ["bound-compaction-client"], compactNativeTurnId).length, 2,
"a completed live row needs canonical History proof before it can be absorbed");
assert.equal(reconcileCompactionOnlyOrphan([
  compactionOrphanOwner,
  { ...compactionOnlyOrphan,
    id: "multiple-live-compaction-row",
    blocks: [
      orphanCompactionBlock,
      { ...orphanCompactionBlock, item_id: "second-live-compaction" },
    ] },
], ["bound-compaction-client"], compactNativeTurnId).length, 2,
"more than one live compaction marker is ambiguous and must fail closed");

const ownerWithExistingCompaction: Turn = {
  ...compactionOrphanOwner,
  blocks: [{ ...orphanCompactionBlock, item_id: "already-owned-compaction" }],
};
assert.equal(reconcileCompactionOnlyOrphan([
  ownerWithExistingCompaction,
  compactionOnlyOrphan,
], ["bound-compaction-client"], compactNativeTurnId).length, 2,
"a distinct second compaction in one native task must wait for History ordering proof");
const exactDuplicateCompaction = reconcileCompactionOnlyOrphan([
  ownerWithExistingCompaction,
  {
    ...compactionOnlyOrphan,
    blocks: [{ ...orphanCompactionBlock,
      item_id: "already-owned-compaction" }],
  },
], ["bound-compaction-client"], compactNativeTurnId);
assert.equal(exactDuplicateCompaction.length, 1,
  "an exact process item duplicate can be removed without guessing occurrence order");
assert.equal(exactDuplicateCompaction[0].blocks.length, 1,
  "exact duplicate reconciliation never paints a second compaction marker");

for (const [label, candidate] of ([
  ["ordinary promptless assistant", {
    ...compactionOnlyOrphan,
    id: "promptless-assistant",
    blocks: [{ kind: "text" as const, message_id: "assistant-final",
      channel: "final" as const, text: "automatic answer", done: true }],
  }],
  ["goal continuation", {
    ...compactionOnlyOrphan,
    id: "goal-continuation",
    forkPointId: compactNativeTurnId,
  }],
  ["optimistic steer", {
    ...compactionOnlyOrphan,
    id: "optimistic-steer-row",
    clientMsgId: "optimistic-steer-row",
    prompt: "change direction",
  }],
  ["ordinary process", {
    ...compactionOnlyOrphan,
    id: "ordinary-process-row",
    blocks: [{ ...orphanCompactionBlock,
      item_id: "ordinary-process", processKind: "command" as const }],
  }],
  ["detail-bearing compaction", {
    ...compactionOnlyOrphan,
    id: "detail-compaction-row",
    detailProjection: {
      segments: [], blocks: [], capped: false, hasMore: false,
      oldestCursor: null, hasNewer: false, newerCursor: null,
    },
  }],
] satisfies Array<[string, Turn]>)) {
  assert.equal(reconcileCompactionOnlyOrphan(
    [compactionOrphanOwner, candidate],
    ["bound-compaction-client"],
    compactNativeTurnId,
  ).length, 2, `${label} is not a disposable compact orphan`);
}

const canonicalCompactionOwner: Turn = {
  id: "canonical-compaction-user",
  forkPointId: compactNativeTurnId,
  prompt: "继续修复问题",
  blocks: [{
    ...orphanCompactionBlock,
    item_id: "canonical-compaction",
    summary: "canonical summary",
  }, {
    kind: "text", message_id: "canonical-compaction-final",
    channel: "final", text: "done", done: true,
  }],
  done: true,
  doneTs: 30_000,
  durationMs: 2_000,
};
const completedCachedOrphan: Turn = {
  ...compactionOnlyOrphan,
  id: "completed-cached-compaction-orphan",
  done: true,
  doneTs: 99_000,
  durationMs: 50_000,
  blocks: [{
    ...orphanCompactionBlock,
    item_id: "cached-compaction",
    summary: "stale cached summary",
  }],
};
const canonicalOrphanRepaired = reconcileProvenCompactionOrphans([
  canonicalCompactionOwner,
  {
    ...completedCachedOrphan,
    blocks: [{ ...orphanCompactionBlock,
      item_id: "canonical-compaction" }],
  },
]);
assert.equal(canonicalOrphanRepaired.length, 1,
  "canonical History removes an exact completed cache occurrence duplicate");
assert.deepEqual({
  doneTs: canonicalOrphanRepaired[0].doneTs,
  durationMs: canonicalOrphanRepaired[0].durationMs,
  compactions: canonicalOrphanRepaired[0].blocks.filter((block) =>
    block.kind === "process" && block.processKind === "compaction").length,
  summary: canonicalOrphanRepaired[0].blocks[0].kind === "process"
    ? canonicalOrphanRepaired[0].blocks[0].summary : null,
}, {
  doneTs: 30_000,
  durationMs: 2_000,
  compactions: 1,
  summary: "canonical summary",
}, "History keeps lifecycle and completed process payload authority");
assert.deepEqual(
  canonicalOrphanRepaired[0].blocks,
  canonicalCompactionOwner.blocks,
  "canonical History never reimports extra compactions from a stale cache row",
);
assert.deepEqual(
  reconcileProvenCompactionOrphans(canonicalOrphanRepaired),
  canonicalOrphanRepaired,
  "repeated canonical History repair is idempotent",
);
assert.equal(reconcileProvenCompactionOrphans([
  canonicalCompactionOwner,
  { ...canonicalCompactionOwner, id: "same-native-steer-owner",
    prompt: "another steer" },
  completedCachedOrphan,
]).length, 3,
"two visible owners of one native task make cache repair ambiguous");

return { orphanCompactionBlock, canonicalCompactionOwner, completedCachedOrphan };
}
const {
  orphanCompactionBlock,
  canonicalCompactionOwner,
  completedCachedOrphan,
} = testCompactionOrphanHelpers();

const sharedTaskSegments = mergeInitialHistory([], [{
  id: "steer-segment-one",
  prompt: "first",
  done: true,
  blocks: [{
    kind: "process" as const,
    item_id: "shared-task-process-one",
    processKind: "command" as const,
    phase: "end" as const,
    status: "succeeded" as const,
    turn_id: "shared-native-task",
    title: "运行命令",
    done: true,
  }],
}, {
  id: "steer-segment-two",
  prompt: "second",
  done: false,
  blocks: [{
    kind: "process" as const,
    item_id: "shared-task-process-two",
    processKind: "command" as const,
    phase: "start" as const,
    status: "running" as const,
    turn_id: "shared-native-task",
    title: "运行命令",
    done: false,
  }],
}]);
assert.equal(sharedTaskSegments.length, 2,
  "ordinary process task ids shared by steer segments are not turn aliases");

const repeatedOld = {
  id: "old-engine", prompt: "继续", done: true, ts: 10_000,
  blocks: [{ kind: "text" as const, message_id: "old-answer", text: "old", done: true }],
};
const repeatedCurrent = {
  id: "current-client", prompt: "继续", done: false, ts: 15_000, blocks: [],
};
const repeatedMerged = mergeInitialHistory([repeatedOld], [repeatedCurrent]);
assert.deepEqual(repeatedMerged.map((turn) => turn.id), ["old-engine", "current-client"]);
assert.equal(repeatedMerged[1].done, false);

const delayedEcho = mergeInitialHistory(
  [{ ...transcriptTurn, id: "engine-delayed", ts: 20_000 }],
  [{
    ...optimisticTurn,
    id: "client-delayed",
    historyTurnId: "engine-delayed",
    ts: 22_500,
  }],
);
assert.deepEqual(delayedEcho.map((turn) => turn.id), ["client-delayed"]);

// History and live ids are generated by different streams. Multiple blocks in
// the same channel must pair in order/content instead of all merging into the
// last commentary block.
const multiCommentHistory = {
  id: "multi-history", prompt: "inspect", done: true, ts: 30_000,
  blocks: [
    { kind: "text" as const, message_id: "hist-a", text: "A", done: true,
      channel: "commentary" as const },
    { kind: "tool" as const, message_id: "hist-a", tool_use_id: "shared-tool",
      tool: "shell", input: {}, done: true,
      result: { content: "ok", is_error: false } },
    { kind: "text" as const, message_id: "hist-b", text: "B", done: true,
      channel: "commentary" as const },
  ],
};
const multiCommentLive = {
  id: "multi-live", historyTurnId: "multi-history",
  prompt: "inspect", done: true, ts: 30_100,
  blocks: [
    { kind: "text" as const, message_id: "live-a", text: "A", done: true,
      channel: "commentary" as const },
    { kind: "tool" as const, message_id: "live-a", tool_use_id: "shared-tool",
      tool: "shell", input: {}, done: true,
      result: { content: "ok", is_error: false } },
    { kind: "text" as const, message_id: "live-b", text: "B", done: true,
      channel: "commentary" as const },
  ],
};
const multiCommentMerged = mergeInitialHistory(
  [multiCommentHistory], [multiCommentLive]);
assert.deepEqual(multiCommentMerged[0].blocks.filter(
  (block) => block.kind === "text").map((block) => block.text), ["A", "B"]);

const unrelatedSameChannelHistory = {
  id: "same-turn-unrelated-text",
  prompt: "inspect",
  done: true,
  blocks: [{
    kind: "text" as const,
    message_id: "history-unrelated",
    text: "canonical earlier commentary",
    done: true,
    channel: "commentary" as const,
  }],
};
const unrelatedSameChannelLive = {
  ...unrelatedSameChannelHistory,
  done: false,
  blocks: [{
    kind: "text" as const,
    message_id: "live-unrelated",
    text: "a distinct later update",
    done: false,
    channel: "commentary" as const,
  }],
};
assert.deepEqual(
  mergeInitialHistory(
    [unrelatedSameChannelHistory],
    [unrelatedSameChannelLive],
    { preserveLiveTailOpen: true },
  )[0].blocks.flatMap((block) =>
    block.kind === "text" ? [block.text] : []),
  ["canonical earlier commentary", "a distinct later update"],
  "same-channel text with zero affinity must remain separate blocks",
);

// Tool-only assistant envelopes have a start/end but no text delta. Summary
// history omits those invisible shells while loaded detail retains them. A
// completed empty shell must not consume the next same-channel summary block:
// doing so shifts every later commentary match and duplicates the final one
// after a focus-triggered History reconciliation.
const toolOnlyShellSummary = {
  id: "tool-shell-turn", prompt: "modify code", done: true, ts: 32_000,
  blocks: [
    { kind: "text" as const, message_id: "comment-a", text: "A", done: true,
      channel: "commentary" as const },
    { kind: "text" as const, message_id: "comment-b", text: "B", done: true,
      channel: "commentary" as const },
    { kind: "text" as const, message_id: "comment-c", text: "C", done: true,
      channel: "commentary" as const },
  ],
};
const toolOnlyShellDetail = {
  ...toolOnlyShellSummary,
  blocks: [
    toolOnlyShellSummary.blocks[0],
    { kind: "text" as const, message_id: "tool-envelope-a", text: "",
      done: true, channel: "commentary" as const },
    { kind: "tool" as const, message_id: "tool-envelope-a",
      tool_use_id: "edit-a", tool: "apply_patch", input: {}, done: true,
      result: { content: "updated", is_error: false } },
    toolOnlyShellSummary.blocks[1],
    { kind: "text" as const, message_id: "tool-envelope-b", text: "",
      done: true, channel: "commentary" as const },
    { kind: "tool" as const, message_id: "tool-envelope-b",
      tool_use_id: "edit-b", tool: "shell", input: {}, done: true,
      result: { content: "verified", is_error: false } },
    toolOnlyShellSummary.blocks[2],
  ],
};
let toolOnlyShellMerged = mergeInitialHistory(
  [toolOnlyShellSummary], [toolOnlyShellDetail]);
toolOnlyShellMerged = mergeInitialHistory(
  [toolOnlyShellSummary], toolOnlyShellMerged);
assert.deepEqual(toolOnlyShellMerged[0].blocks.flatMap(
  (block) => block.kind === "text" && block.text.length > 0
    ? [{ id: block.message_id, text: block.text }]
    : []),
[
  { id: "comment-a", text: "A" },
  { id: "comment-b", text: "B" },
  { id: "comment-c", text: "C" },
], "completed empty tool envelopes must not shift or duplicate commentary");

// Rollout history and the live app-server now share the authoritative
// response_item id. The live delta may still arrive before item/started and
// temporarily carry channel=unknown; identity, not text guessing, must merge it
// into the canonical commentary block.
const stableMessageHistory = {
  id: "stable-history", forkPointId: "stable-turn", prompt: "inspect",
  done: true, ts: 35_000,
  blocks: [{
    kind: "text" as const, message_id: "msg-stable",
    text: "same item", done: true, channel: "commentary" as const,
  }],
};
const stableMessageLive = {
  id: "stable-turn", forkPointId: "stable-turn", prompt: "inspect",
  done: false, ts: 35_100,
  blocks: [{
    kind: "text" as const, message_id: "msg-stable",
    text: "same item", done: false, channel: "unknown" as const,
  }],
};
const stableMessageMerged = mergeInitialHistory(
  [stableMessageHistory], [stableMessageLive],
  { preserveLiveTailOpen: true },
);
assert.equal(stableMessageMerged.length, 1);
assert.deepEqual(stableMessageMerged[0].blocks.filter(
  (block) => block.kind === "text").map((block) => ({
    id: block.message_id, text: block.text, channel: block.channel,
  })), [{
  id: "msg-stable", text: "same item", channel: "commentary",
}]);

const openEmptyMessageMerged = mergeInitialHistory(
  [{
    ...stableMessageHistory,
    blocks: [{
      kind: "text" as const, message_id: "history-before-delta",
      text: "history prefix", done: true, channel: "commentary" as const,
    }],
  }],
  [{
    ...stableMessageLive,
    blocks: [{
      kind: "text" as const, message_id: "live-before-delta",
      text: "", done: false, channel: "commentary" as const,
    }],
  }],
  { preserveLiveTailOpen: true },
);
assert.deepEqual(openEmptyMessageMerged[0].blocks.flatMap(
  (block) => block.kind === "text"
    ? [{ id: block.message_id, text: block.text }]
    : []),
[{ id: "live-before-delta", text: "history prefix" }],
"an open empty placeholder must retain the id targeted by future deltas");

// A focus-triggered History page can use a regenerated response_item id while
// the app-server keeps streaming deltas to the live id. Reconciliation must
// retain that live binding; otherwise the next delta creates a second text
// block, and every Codex -> Claude -> Codex switch paints another copy.
const regeneratedMessageHistory = {
  id: "regenerated-history", forkPointId: "regenerated-turn",
  prompt: "inspect", done: true, ts: 35_500,
  blocks: [{
    kind: "text" as const, message_id: "history-commentary",
    text: "first half", done: true, channel: "commentary" as const,
  }],
};
const regeneratedMessageLive = {
  id: "regenerated-turn", forkPointId: "regenerated-turn",
  prompt: "inspect", done: false, ts: 35_600,
  blocks: [{
    kind: "text" as const, message_id: "live-commentary",
    text: "first half", done: false, channel: "commentary" as const,
  }],
};
let regeneratedMessageMerged = mergeInitialHistory(
  [regeneratedMessageHistory], [regeneratedMessageLive],
  { preserveLiveTailOpen: true },
);
assert.equal(
  regeneratedMessageMerged[0].blocks[0]?.kind === "text"
    && regeneratedMessageMerged[0].blocks[0].message_id,
  "live-commentary",
  "an open history merge must keep the id targeted by future live deltas",
);
for (const suffix of [" plus tools", " and answer"]) {
  regeneratedMessageMerged = regeneratedMessageMerged.map((turn) => ({
    ...turn,
    blocks: turn.blocks.map((block) => block.kind === "text"
      ? { ...block, text: block.text + suffix } : block),
  }));
  regeneratedMessageMerged = mergeInitialHistory(
    [regeneratedMessageHistory], regeneratedMessageMerged,
    { preserveLiveTailOpen: true },
  );
  assert.equal(
    regeneratedMessageMerged[0].blocks.filter(
      (block) => block.kind === "text").length,
    1,
    "repeated focus History merges remain idempotent while the turn runs",
  );
  assert.equal(
    regeneratedMessageMerged[0].blocks[0]?.kind === "text"
      && regeneratedMessageMerged[0].blocks[0].message_id,
    "live-commentary",
  );
}

// Once summary history exposes payload-free image references, it replaces the
// optimistic inline bodies. Keeping both representations renders two image
// rows and leaves a large blank placeholder below the visible thumbnail.
const imageHistory = {
  id: "image-history", prompt: "看图", done: true, ts: 36_000,
  imageRefs: [{
    image_id: "image-1", media_type: "image/png" as const,
    width: 1200, height: 600, byte_size: 1024,
  }],
  blocks: [],
};
const imageLive = {
  id: "image-live", historyTurnId: "image-history",
  prompt: "看图", done: false, ts: 36_100,
  images: [{ media_type: "image/png" as const, data: "inline-body" }],
  blocks: [],
};
const imageMerged = mergeInitialHistory([imageHistory], [imageLive]);
assert.equal(imageMerged.length, 1);
assert.equal(imageMerged[0].images, undefined);
assert.deepEqual(imageMerged[0].imageRefs, imageHistory.imageRefs);
const turnImagePreviews = new TurnImagePreviewCache();
turnImagePreviews.update("image-session", [imageLive]);
turnImagePreviews.update("image-session", [imageMerged[0]]);
const retainedImagePreview = turnImagePreviews.get(imageMerged[0].id, 0);
assert.deepEqual(retainedImagePreview, imageLive.images[0],
  "a canonical History swap retains the already-painted optimistic preview");
assert.equal(
  historyImageDisplaySource({ status: "loading" }, retainedImagePreview),
  "data:image/png;base64,inline-body",
  "the loading thumbnail cannot blank the image during the thinking transition",
);
assert.equal(
  historyImageDisplaySource({
    status: "ready", mediaType: "image/webp", data: "canonical-thumbnail",
  }, retainedImagePreview),
  "data:image/webp;base64,canonical-thumbnail",
  "the canonical thumbnail replaces the fallback without a placeholder frame",
);
turnImagePreviews.release(imageMerged[0].id);
assert.equal(turnImagePreviews.get(imageMerged[0].id, 0), undefined);

function imageCapacityWakeHarness() {
  let nextHandle = 1;
  const scheduled = new Map<number, {
    callback: () => void;
    delayMs: number;
  }>();
  return {
    scheduler: {
      schedule(callback: () => void, delayMs: number): unknown {
        const handle = nextHandle++;
        scheduled.set(handle, { callback, delayMs });
        return handle;
      },
      cancel(handle: unknown): void {
        if (typeof handle === "number") scheduled.delete(handle);
      },
    },
    scheduledDelays(): number[] {
      return [...scheduled.values()].map((task) => task.delayMs);
    },
    fireNext(): boolean {
      const next = scheduled.entries().next().value;
      if (!next) return false;
      const [handle, task] = next;
      scheduled.delete(handle);
      task.callback();
      return true;
    },
  };
}

const rotatingHistoryImages = new HistoryImageAssetCache(1);
const rotatingHistoryAKey = historyImageAssetKey(
  "rotation-turn", "rotation-a", "thumbnail");
const rotatingHistoryBKey = historyImageAssetKey(
  "rotation-turn", "rotation-b", "thumbnail");
assert.equal(rotatingHistoryImages.begin({
  sid: "rotation-session",
  turnId: "rotation-turn",
  imageId: "rotation-a",
  variant: "thumbnail",
  requestId: "rotation-a-first",
  revision: "rotation-r1",
}), true);
assert.equal(rotatingHistoryImages.accept({
  v: 26,
  ts: 10,
  type: "history_image",
  session_id: "rotation-session",
  turn_id: "rotation-turn",
  image_id: "rotation-a",
  variant: "thumbnail",
  request_id: "rotation-a-first",
  revision: "rotation-r1",
  media_type: "image/png",
  data: "AA==",
}), true);
let rotatingHistoryAObserved = false;
const rotatingHistoryA = rotatingHistoryImages.forSession(
  "rotation-session")[rotatingHistoryAKey];
rotatingHistoryAObserved ||= !!rotatingHistoryA;
assert.equal(rotatingHistoryA?.status, "ready");
assert.equal(rotatingHistoryImages.begin({
  sid: "rotation-session",
  turnId: "rotation-turn",
  imageId: "rotation-b",
  variant: "thumbnail",
  requestId: "rotation-b-first",
  revision: "rotation-r1",
}), true);
assert.equal(
  shouldAutoloadHistoryImage(
    rotatingHistoryImages.forSession("rotation-session")[
      rotatingHistoryAKey
    ],
    rotatingHistoryAObserved,
  ),
  false,
  "an image which already held an asset must not auto-compete after LRU eviction",
);
assert.equal(rotatingHistoryImages.accept({
  v: 26,
  ts: 11,
  type: "history_image",
  session_id: "rotation-session",
  turn_id: "rotation-turn",
  image_id: "rotation-b",
  variant: "thumbnail",
  request_id: "rotation-b-first",
  revision: "rotation-r1",
  media_type: "image/png",
  data: "AQ==",
}), true);
assert.deepEqual(
  Object.keys(rotatingHistoryImages.forSession("rotation-session")),
  [rotatingHistoryBKey],
  "limit-one A/B demand must settle on B instead of automatically rotating to A",
);
assert.equal(rotatingHistoryImages.begin({
  sid: "rotation-session",
  turnId: "rotation-turn",
  imageId: "rotation-a",
  variant: "thumbnail",
  requestId: "rotation-a-explicit",
  revision: "rotation-r1",
}), true, "the evicted image remains available to an explicit retry");
rotatingHistoryImages.clear();

let historyCapacityNow = 20_000;
const historyCapacityWake = imageCapacityWakeHarness();
const silentHistoryCapacity = new HistoryImageAssetCache(
  1,
  () => historyCapacityNow,
  historyCapacityWake.scheduler,
);
assert.equal(silentHistoryCapacity.begin({
  sid: "history-capacity-session",
  turnId: "history-capacity-turn",
  imageId: "occupied",
  variant: "thumbnail",
  requestId: "history-capacity-occupied",
  revision: "history-capacity-r1",
}), true);
assert.equal(silentHistoryCapacity.begin({
  sid: "history-capacity-session",
  turnId: "history-capacity-turn",
  imageId: "waiting",
  variant: "thumbnail",
  requestId: "history-capacity-waiting",
  revision: "history-capacity-r1",
}), false);
assert.deepEqual(
  historyCapacityWake.scheduledDelays(),
  [HISTORY_IMAGE_REQUEST_TIMEOUT_MS],
  "a blocked history image schedules the earliest loading-slot expiry",
);
const historyCapacitySnapshot = historyImageAssetCacheSnapshot();
historyCapacityNow += HISTORY_IMAGE_REQUEST_TIMEOUT_MS;
assert.equal(historyCapacityWake.fireNext(), true);
assert.ok(historyImageAssetCacheSnapshot() > historyCapacitySnapshot,
  "time alone publishes a history-image capacity wake");
assert.equal(silentHistoryCapacity.begin({
  sid: "history-capacity-session",
  turnId: "history-capacity-turn",
  imageId: "waiting",
  variant: "thumbnail",
  requestId: "history-capacity-retry",
  revision: "history-capacity-r1",
}), true, "the mounted history-image waiter can claim the expired slot");
silentHistoryCapacity.clear();

let inlineCapacityNow = 30_000;
const inlineCapacityWake = imageCapacityWakeHarness();
const silentInlineCapacity = new InlineImageAssetCache(
  1,
  () => inlineCapacityNow,
  inlineCapacityWake.scheduler,
);
assert.equal(silentInlineCapacity.begin({
  sid: "inline-capacity-session",
  path: "/tmp/occupied.png",
  previewId: "inline-capacity-occupied-preview",
  requestId: "inline-capacity-occupied",
}), true);
assert.equal(silentInlineCapacity.begin({
  sid: "inline-capacity-session",
  path: "/tmp/waiting.png",
  previewId: "inline-capacity-waiting-preview",
  requestId: "inline-capacity-waiting",
}), false);
assert.deepEqual(
  inlineCapacityWake.scheduledDelays(),
  [INLINE_IMAGE_REQUEST_TIMEOUT_MS],
  "a blocked inline image schedules the earliest loading-slot expiry",
);
const inlineCapacitySnapshot = inlineImageAssetCacheSnapshot();
inlineCapacityNow += INLINE_IMAGE_REQUEST_TIMEOUT_MS;
assert.equal(inlineCapacityWake.fireNext(), true);
assert.ok(inlineImageAssetCacheSnapshot() > inlineCapacitySnapshot,
  "time alone publishes an inline-image capacity wake");
assert.equal(silentInlineCapacity.begin({
  sid: "inline-capacity-session",
  path: "/tmp/waiting.png",
  previewId: "inline-capacity-retry-preview",
  requestId: "inline-capacity-retry",
}), true, "the mounted inline-image waiter can claim the expired slot");
silentInlineCapacity.clear();

const stableInlineImages = new InlineImageAssetCache(2);
for (const id of ["view-a", "view-b"]) {
  assert.equal(stableInlineImages.begin({
    sid: "inline-stable-session",
    path: "/tmp/reused.png",
    assetKey: id,
    previewId: id,
    requestId: `request-${id}`,
  }), true);
  assert.equal(stableInlineImages.accept({
    v: 28,
    ts: 10,
    type: "preview_asset",
    sid: "inline-stable-session",
    path: "/tmp/reused.png",
    preview_id: id,
    request_id: `request-${id}`,
    media_type: "image/png",
    data: id === "view-a" ? "aW1hZ2UtYQ==" : "aW1hZ2UtYg==",
  } as PreviewAsset), true);
}
assert.deepEqual(
  Object.keys(stableInlineImages.forSession(
    "inline-stable-session")).sort(),
  ["view-a", "view-b"],
  "two imageView items that reuse one path retain independent snapshots",
);
stableInlineImages.clear();

const reconnectingAuthorizedImage = new InlineImageAssetCache(1);
assert.equal(reconnectingAuthorizedImage.begin({
  sid: "authorization-session",
  path: "/tmp/authorized.png",
  previewId: "authorization-preview",
  requestId: "authorization-request",
}), true);
assert.equal(reconnectingAuthorizedImage.requireAuthorization({
  v: 28,
  ts: 11,
  type: "preview_authorization_required",
  sid: "authorization-session",
  authorization_id: "authorization-id",
  request_id: "authorization-request",
  operation: "preview_asset",
  path: "/tmp/authorized.png",
  resolved_path: "/private/tmp/authorized.png",
  format: "image",
  preview_id: "authorization-preview",
}), true);
const reconnectingAuthorization = reconnectingAuthorizedImage.forSession(
  "authorization-session")["/tmp/authorized.png"].authorization!;
assert.equal(reconnectingAuthorizedImage.markAuthorizationSubmitting(
  reconnectingAuthorization), true);
assert.equal(reconnectingAuthorizedImage.acceptAuthorizationResult({
  v: 28,
  ts: 12,
  type: "preview_authorization_result",
  sid: "stale-socket-session",
  authorization_id: "authorization-id",
  request_id: "authorization-request",
  operation: "preview_asset",
  path: "/tmp/authorized.png",
  preview_id: "authorization-preview",
  status: "granted",
}), false, "a stale socket result cannot complete another session's challenge");
assert.notEqual(reconnectingAuthorizedImage.acceptAuthorizationResult({
  v: 28,
  ts: 13,
  type: "preview_authorization_result",
  sid: "authorization-session",
  authorization_id: "authorization-id",
  request_id: "authorization-request",
  operation: "preview_asset",
  path: "/tmp/authorized.png",
  preview_id: "authorization-preview",
  status: "granted",
}), false, "the same cache survives a RelayWs reconnect and accepts its result");
assert.equal(reconnectingAuthorizedImage.acceptAuthorizationResult({
  v: 28,
  ts: 14,
  type: "preview_authorization_result",
  sid: "authorization-session",
  authorization_id: "authorization-id",
  request_id: "authorization-request",
  operation: "preview_asset",
  path: "/tmp/authorized.png",
  preview_id: "authorization-preview",
  status: "granted",
}), false, "a duplicate result cannot issue the authorized read twice");
reconnectingAuthorizedImage.clear();

let historyImageNow = 1_000;
const retryableHistoryImages = new HistoryImageAssetCache(
  1, () => historyImageNow);
assert.equal(retryableHistoryImages.begin({
  sid: "image-session",
  turnId: "image-turn",
  imageId: "image-id",
  variant: "thumbnail",
  requestId: "history-image-first",
  revision: "image-r1",
}), true);
assert.equal(retryableHistoryImages.has(
  "image-session", "image-turn", "image-id", "thumbnail"), true);
historyImageNow += HISTORY_IMAGE_REQUEST_TIMEOUT_MS;
assert.equal(retryableHistoryImages.has(
  "image-session", "image-turn", "image-id", "thumbnail"), false,
"a silent history-image request becomes retryable after its timeout");
assert.equal(retryableHistoryImages.begin({
  sid: "image-session",
  turnId: "image-turn",
  imageId: "image-id",
  variant: "thumbnail",
  requestId: "history-image-second",
  revision: "image-r1",
}), true);
assert.equal(retryableHistoryImages.accept({
  v: 26,
  ts: 10,
  type: "history_image",
  session_id: "image-session",
  turn_id: "image-turn",
  image_id: "image-id",
  variant: "thumbnail",
  request_id: "history-image-second",
  revision: "image-r1",
  error: "temporary read failure",
}), true);
assert.equal(retryableHistoryImages.has(
  "image-session", "image-turn", "image-id", "thumbnail"), false,
"an explicit history-image failure must not poison future retries");
assert.equal(retryableHistoryImages.begin({
  sid: "image-session",
  turnId: "image-turn",
  imageId: "image-id",
  variant: "thumbnail",
  requestId: "history-image-third",
  revision: "image-r1",
}), true);

let lateHistoryNow = 3_000;
const lateHistoryImages = new HistoryImageAssetCache(
  1, () => lateHistoryNow);
assert.equal(lateHistoryImages.begin({
  sid: "image-session",
  turnId: "image-turn",
  imageId: "evicted-a",
  variant: "thumbnail",
  requestId: "history-evicted-a",
  revision: "image-r1",
}), true);
lateHistoryNow += HISTORY_IMAGE_REQUEST_TIMEOUT_MS;
assert.equal(lateHistoryImages.begin({
  sid: "image-session",
  turnId: "image-turn",
  imageId: "active-b",
  variant: "thumbnail",
  requestId: "history-active-b",
  revision: "image-r1",
}), true);
assert.equal(lateHistoryImages.accept({
  v: 26,
  ts: 10,
  type: "history_image",
  session_id: "image-session",
  turn_id: "image-turn",
  image_id: "evicted-a",
  variant: "thumbnail",
  request_id: "history-evicted-a",
  revision: "image-r1",
  media_type: "image/png",
  data: "AA==",
}), false,
"a late response for a timeout-evicted history image must be rejected");
assert.deepEqual(
  Object.keys(lateHistoryImages.forSession("image-session")),
  ["image-turn\u0000active-b\u0000thumbnail"],
  "late responses cannot grow the history-image cache beyond its hard limit",
);

let inlineImageNow = 2_000;
const retryableInlineImages = new InlineImageAssetCache(
  1, () => inlineImageNow);
assert.equal(retryableInlineImages.begin({
  sid: "inline-session",
  path: "/tmp/diagram.png",
  previewId: "inline-preview-one",
  requestId: "inline-request-one",
}), true);
inlineImageNow += INLINE_IMAGE_REQUEST_TIMEOUT_MS;
assert.equal(retryableInlineImages.has(
  "inline-session", "/tmp/diagram.png"), false,
"a silent inline-image request becomes retryable after its timeout");
assert.equal(retryableInlineImages.begin({
  sid: "inline-session",
  path: "/tmp/diagram.png",
  previewId: "inline-preview-two",
  requestId: "inline-request-two",
}), true);
assert.equal(retryableInlineImages.accept({
  v: 26,
  ts: 10,
  type: "preview_asset",
  sid: "inline-session",
  path: "/tmp/diagram.png",
  preview_id: "inline-preview-two",
  request_id: "inline-request-two",
  error: "temporary read failure",
}), true);
assert.equal(retryableInlineImages.has(
  "inline-session", "/tmp/diagram.png"), false,
"an explicit inline-image failure must not poison future retries");
assert.equal(retryableInlineImages.begin({
  sid: "inline-session",
  path: "/tmp/diagram.png",
  previewId: "inline-preview-three",
  requestId: "inline-request-three",
}), true);

let lateInlineNow = 4_000;
const lateInlineImages = new InlineImageAssetCache(
  1, () => lateInlineNow);
assert.equal(lateInlineImages.begin({
  sid: "inline-session",
  path: "/tmp/evicted-a.png",
  previewId: "inline-evicted-preview",
  requestId: "inline-evicted-request",
}), true);
lateInlineNow += INLINE_IMAGE_REQUEST_TIMEOUT_MS;
assert.equal(lateInlineImages.begin({
  sid: "inline-session",
  path: "/tmp/active-b.png",
  previewId: "inline-active-preview",
  requestId: "inline-active-request",
}), true);
assert.equal(lateInlineImages.accept({
  v: 26,
  ts: 10,
  type: "preview_asset",
  sid: "inline-session",
  path: "/tmp/evicted-a.png",
  preview_id: "inline-evicted-preview",
  request_id: "inline-evicted-request",
  media_type: "image/png",
  data: "AA==",
}), false,
"a late response for a timeout-evicted inline image must be rejected");
assert.deepEqual(
  Object.keys(lateInlineImages.forSession("inline-session")),
  ["/tmp/active-b.png"],
  "late responses cannot grow the inline-image cache beyond its hard limit",
);

// A complete transcript must not be reopened forever by stale cache state.
const terminalHistory = {
  id: "terminal-history", prompt: "run", done: true, ts: 40_000,
  blocks: [
    { kind: "tool" as const, message_id: "tool-msg", tool_use_id: "tool-terminal",
      tool: "shell", input: {}, done: true,
      result: { content: "ok", is_error: false } },
    { kind: "process" as const, item_id: "agent-terminal", processKind: "agent" as const,
      phase: "end" as const, status: "succeeded" as const, title: "代理完成", done: true },
  ],
};
const staleLive = {
  id: "terminal-live", historyTurnId: "terminal-history",
  prompt: "run", done: false, ts: 40_100,
  blocks: [
    { kind: "tool" as const, message_id: "tool-msg-live", tool_use_id: "tool-terminal",
      tool: "shell", input: {}, done: false },
    { kind: "process" as const, item_id: "agent-terminal", processKind: "agent" as const,
      phase: "update" as const, status: "running" as const, title: "代理运行中", done: false },
  ],
};
const terminalMerged = mergeInitialHistory([terminalHistory], [staleLive]);
assert.equal(terminalMerged[0].blocks.every((block) => block.done), true);
assert.equal(terminalMerged[0].blocks[0].kind === "tool"
  && terminalMerged[0].blocks[0].result?.content, "ok");
assert.equal(terminalMerged[0].blocks[1].kind === "process"
  && terminalMerged[0].blocks[1].title, "代理完成");
const liveDurationMerged = mergeInitialHistory(
  [{ ...terminalHistory, durationMs: 0 }],
  [{ ...staleLive, durationMs: 5000 }],
);
assert.equal(liveDurationMerged[0].durationMs, 5000,
  "a synthetic Claude history duration must not erase a real live duration");
const openTailMerged = mergeInitialHistory(
  [terminalHistory], [staleLive], { preserveLiveTailOpen: true });
assert.equal(openTailMerged[0].blocks.some((block) => !block.done), true);

// Never collapse two real repeated turns just because both prompt and response
// happen to be identical. Stable ids remain the only authoritative identity.
const repeatedAnswerTurns = [
  { id: "repeat-1", prompt: "在？", done: true, ts: 5000, doneTs: 6000,
    blocks: [{ kind: "text" as const, message_id: "repeat-answer-1",
      text: "在的", done: true }] },
  { id: "repeat-2", prompt: "在？", done: true, ts: 7000, doneTs: 8000,
    blocks: [{ kind: "text" as const, message_id: "repeat-answer-2",
      text: "在的", done: true }] },
];
assert.deepEqual(
  mergeInitialHistory(repeatedAnswerTurns, []).map((turn) => turn.id),
  ["repeat-1", "repeat-2"],
);
assert.deepEqual(
  mergeInitialHistory(
    [repeatedAnswerTurns[0]],
    [{ ...repeatedAnswerTurns[1], done: false }],
  ).map((turn) => turn.id),
  ["repeat-1", "repeat-2"],
  "same prompt and nearby timestamps cannot replace an authoritative alias",
);

// Command+R can paint an older IndexedDB item id, then reconnect replay paints
// the same completed item under its canonical app-server id. A settled Codex
// History page contains the exact canonical item and is authoritative that
// there was only one source item. Collapse that stale third projection without
// enabling text-only dedupe for running, cache-only, Claude, or distinct rows.
const reconnectCanonicalHistory = [{
  id: "reconnect-browser-turn", historyTurnId: "reconnect-native-user",
  prompt: "deploy", done: true,
  blocks: [{
    kind: "text" as const, message_id: "canonical-final-item",
    text: "deployed once", done: true, channel: "final" as const,
  }],
}];
const reconnectCachedAndLive = [{
  id: "reconnect-browser-turn", historyTurnId: "reconnect-native-user",
  prompt: "deploy", done: true,
  blocks: [{
    kind: "text" as const, message_id: "old-cache-final-item",
    text: "deployed once", done: true, channel: "final" as const,
  }, {
    kind: "text" as const, message_id: "canonical-final-item",
    text: "deployed once", done: true, channel: "final" as const,
  }],
}];
assert.equal(
  mergeInitialHistory(
    reconnectCanonicalHistory, reconnectCachedAndLive,
  )[0].blocks.filter((block) => block.kind === "text").length,
  2,
  "an unsettled merge must not infer duplicate native items from equal text",
);
const settledReconnectMerge = mergeInitialHistory(
  reconnectCanonicalHistory, reconnectCachedAndLive, {}, true,
);
assert.deepEqual(
  settledReconnectMerge[0].blocks.flatMap((block) => block.kind === "text"
    ? [{ id: block.message_id, text: block.text }] : []),
  [{ id: "canonical-final-item", text: "deployed once" }],
  "settled Codex History removes the stale cache/replay copy",
);

// Exact production race at the pure merge boundary: a focus-triggered History
// synthesizes TurnEnd while the matching live tail is still running. Preserve
// that open tail, let the live answer finish in place, then reconcile the full
// transcript without creating a second assistant-only turn.
const partialHistory = {
  id: "engine-active", prompt: "在？", done: true, ts: 10_000, doneTs: 11_000,
  forkPointId: "codex-turn-a",
  blocks: [] as Array<{ kind: "text"; message_id: string; text: string; done: boolean }>,
};
const liveActive = {
  id: "client-active", historyTurnId: "engine-active",
  prompt: "在？", done: false, ts: 10_100,
  blocks: [{ kind: "text" as const, message_id: "live-active-answer",
    text: "", done: false }],
};
const activeMerge = mergeInitialHistory(
  [partialHistory], [liveActive], { preserveLiveTailOpen: true });
assert.equal(activeMerge.length, 1);
assert.equal(activeMerge[0].done, false);
assert.equal(activeMerge[0].doneTs, undefined);
assert.equal(activeMerge[0].forkPointId, "codex-turn-a");
const liveFinished = [{
  ...activeMerge[0], done: true, doneTs: 12_000,
  blocks: activeMerge[0].blocks.map((block) => block.kind === "text"
    ? { ...block, text: "only once", done: true } : block),
}];
const completeHistory = [{
  ...partialHistory, doneTs: 12_000,
  blocks: [{ kind: "text" as const, message_id: "engine-active-answer",
    text: "only once", done: true }],
}];
const activeReconciled = mergeInitialHistory(completeHistory, liveFinished);
assert.equal(activeReconciled.length, 1);
assert.deepEqual(activeReconciled[0].blocks.map((block) => block.kind === "text"
  ? block.text : "tool"), ["only once"]);

// A Codex goal continuation has no user prompt. Live and rollout use different
// display ids, but their authoritative app-server turn id must dedupe them.
const assistantOnlyMerged = mergeInitialHistory(
  [{ id: "history-agent-message", forkPointId: "goal-turn-1", prompt: "",
    done: true, blocks: [{ kind: "text" as const, message_id: "history-answer",
      text: "goal complete", done: true, channel: "final" as const }] }],
  [{ id: "goal-turn-1", forkPointId: "goal-turn-1", prompt: "",
    done: true, blocks: [{ kind: "text" as const, message_id: "live-answer",
      text: "goal complete", done: true, channel: "final" as const }] }],
);
assert.equal(assistantOnlyMerged.length, 1);
assert.equal(assistantOnlyMerged[0].id, "goal-turn-1");
assert.equal(assistantOnlyMerged[0].blocks.filter((block) => block.kind === "text").length, 1);

// A reconnect can begin replay at an assistant item after the user/native turn
// binding has fallen out of the relay ring. The provisional row then uses the
// assistant message id as its display id. An authoritative newest History page
// owns that same message inside the native turn and must absorb the orphan
// without comparing prompt text or timestamps.
const replayAssistantMessageId = "msg-replayed-assistant";
const replayOrphanMerged = mergeInitialHistory(
  [{
    id: "native-history-turn",
    prompt: "inspect the repository",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: "one authoritative answer",
      done: true,
    }],
    detailEventCount: 3,
    detailLoaded: false,
  }],
  [{
    id: replayAssistantMessageId,
    prompt: "",
    done: false,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: "one authoritative answer",
      done: false,
    }],
    detailError: "stale orphan detail",
  }],
  { reconcileReplayOrphans: true },
);
assert.equal(replayOrphanMerged.length, 1);
assert.equal(replayOrphanMerged[0].id, "native-history-turn");
assert.equal(replayOrphanMerged[0].detailError, undefined,
  "an invalid orphan detail request cannot replace canonical detail authority");
assert.deepEqual(
  replayOrphanMerged[0].blocks.filter((block) => block.kind === "text")
    .map((block) => block.text),
  ["one authoritative answer"],
);

const sharedReplayMessageId = "msg-replayed-tool-batch";
const replayTool = (toolUseId: string) => ({
  kind: "tool" as const,
  message_id: sharedReplayMessageId,
  tool_use_id: toolUseId,
  tool: "Command",
  input: {},
  done: true,
});
const replayToolBatch = [
  replayTool("replayed-tool-a"), replayTool("replayed-tool-b"),
];
const multiToolReplayMerged = mergeInitialHistory(
  [{
    id: "native-history-tool-turn",
    prompt: "deploy",
    done: true,
    blocks: replayToolBatch,
  }],
  [{
    id: sharedReplayMessageId,
    prompt: "",
    done: true,
    blocks: replayToolBatch.map((block) => ({ ...block })),
  }],
  { reconcileReplayOrphans: true },
);
assert.equal(multiToolReplayMerged.length, 1,
  "tool calls sharing one assistant message must not leave a prompt-less turn");
assert.deepEqual(
  multiToolReplayMerged[0].blocks.flatMap((block) =>
    block.kind === "tool" ? [block.tool_use_id] : []),
  ["replayed-tool-a", "replayed-tool-b"],
);

const mixedReplayMerged = mergeInitialHistory(
  [{
    id: "native-history-mixed-turn",
    prompt: "inspect",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: sharedReplayMessageId,
      channel: "commentary" as const,
      text: "checking",
      done: true,
    }, replayTool("replayed-tool-c")],
  }],
  [{
    id: sharedReplayMessageId,
    prompt: "",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: sharedReplayMessageId,
      channel: "commentary" as const,
      text: "checking",
      done: true,
    }, replayTool("replayed-tool-c")],
  }],
  { reconcileReplayOrphans: true },
);
assert.equal(mixedReplayMerged.length, 1,
  "text and tools from one assistant message are one replay projection");

const duplicatedPrimaryReplay = mergeInitialHistory(
  [{
    id: "native-history-duplicate-tool-turn",
    prompt: "inspect",
    done: true,
    blocks: [replayTool("duplicated-tool")],
  }],
  [{
    id: sharedReplayMessageId,
    prompt: "",
    done: true,
    blocks: [replayTool("duplicated-tool"), replayTool("duplicated-tool")],
  }],
  { reconcileReplayOrphans: true },
);
assert.equal(duplicatedPrimaryReplay.length, 2,
  "a genuinely duplicated primary block identity remains unsafe to absorb");

assert.equal(mergeInitialHistory(
  replayOrphanMerged,
  [{
    id: replayAssistantMessageId,
    prompt: "",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: "one authoritative answer",
      done: true,
    }],
  }],
).length, 2, "non-authoritative merge paths never absorb replay orphans");

const ambiguousReplayOrphans = mergeInitialHistory(
  replayOrphanMerged,
  [0, 1].map((index) => ({
    id: replayAssistantMessageId,
    prompt: "",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: `replay candidate ${index}`,
      done: true,
    }],
  })),
  { reconcileReplayOrphans: true },
);
assert.equal(ambiguousReplayOrphans.length, 2,
  "multiple provisional rows claiming one item cannot replace canonical history");

const ambiguousCanonicalReplay = mergeInitialHistory(
  ["canonical-a", "canonical-b"].map((id) => ({
    id,
    prompt: `prompt ${id}`,
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: `answer ${id}`,
      done: true,
    }],
  })),
  [{
    id: replayAssistantMessageId,
    prompt: "",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: "ambiguous replay",
      done: true,
    }],
  }],
  { reconcileReplayOrphans: true },
);
assert.equal(ambiguousCanonicalReplay.length, 3,
  "a message identity shared by canonical rows is not safe merge evidence");

const unrelatedAssistantOnly = mergeInitialHistory(
  [{
    id: "native-history-turn",
    prompt: "inspect the repository",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: replayAssistantMessageId,
      channel: "final" as const,
      text: "same visible text",
      done: true,
    }],
  }],
  [{
    id: "different-assistant-message",
    prompt: "",
    done: true,
    blocks: [{
      kind: "text" as const,
      message_id: "different-assistant-message",
      channel: "final" as const,
      text: "same visible text",
      done: true,
    }],
  }],
  { reconcileReplayOrphans: true },
);
assert.equal(unrelatedAssistantOnly.length, 2,
  "equal assistant text without an exact message identity remains separate");

// A bounded head refresh merges against rows retained from earlier pages.
// Replayed assistant-only continuations can come from legacy/cache projections
// whose synthetic start time is newer than their authoritative terminal time.
// That impossible timestamp must not append old history after the active tail.
const impossibleReplayTime = {
  id: "old-assistant-only", prompt: "", done: true,
  ts: 20_000, doneTs: 8_000, durationMs: 1_000, blocks: [],
};
const currentHistoryTurn = {
  id: "current-history", prompt: "current", done: true,
  ts: 10_000, doneTs: 11_000, blocks: [],
};
const currentLiveTurn = {
  ...currentHistoryTurn, done: false, doneTs: undefined,
};
assert.deepEqual(mergeInitialHistory(
  [currentHistoryTurn],
  [impossibleReplayTime, currentLiveTurn],
  { preserveLiveTailOpen: true },
).map((turn) => turn.id), [
  "old-assistant-only", "current-history",
]);
const aliaslessSameText = mergeInitialHistory(
  [{
    id: "history-active-no-overlap",
    prompt: "guide",
    forkPointId: "shared-route-only",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "history-message-only",
      channel: "commentary",
      text: "the same visible words",
      done: true,
    }],
  }],
  [{
    id: "live-active-no-overlap",
    clientMsgId: "live-active-no-overlap",
    liveTaskId: "shared-route-only",
    prompt: "guide",
    done: false,
    blocks: [{
      kind: "text",
      message_id: "live-message-only",
      channel: "commentary",
      text: "the same visible words",
      done: true,
    }],
  }],
  {
    preserveLiveTailOpen: true,
    newestHistoryId: "history-active-no-overlap",
    reconcileReplayOrphans: true,
  },
);
assert.deepEqual(
  aliaslessSameText.map((turn) => turn.id),
  ["history-active-no-overlap", "live-active-no-overlap"],
  "equal prompt/commentary text and one shared routing id cannot prove UI identity",
);
const terminalSameTaskDifferentItems = mergeInitialHistory(
  [{
    id: "terminal-history-head",
    prompt: "same native task",
    forkPointId: "terminal-shared-task",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "terminal-history-final",
      channel: "final",
      text: "history final",
      done: true,
    }],
  }],
  [{
    id: "terminal-history-head",
    prompt: "same native task",
    forkPointId: "terminal-shared-task",
    done: true,
    blocks: [],
  }, {
    id: "terminal-distinct-steer",
    clientMsgId: "terminal-distinct-steer",
    prompt: "same native task",
    forkPointId: "terminal-shared-task",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "terminal-distinct-final",
      channel: "final",
      text: "another real segment",
      done: true,
    }],
  }],
  {
    newestHistoryId: "terminal-history-head",
    reconcileReplayOrphans: true,
  },
);
assert.deepEqual(
  terminalSameTaskDifferentItems.map((turn) => turn.id),
  ["terminal-history-head", "terminal-distinct-steer"],
  "a terminal head cannot absorb another steer on task/prompt identity alone",
);
const conflictingTaskSharedItem = mergeInitialHistory(
  [{
    id: "conflicting-task-history",
    prompt: "shared item is still not enough",
    forkPointId: "history-native-task",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "conflicting-task-shared-message",
      channel: "final",
      text: "shared native item",
      done: true,
    }],
  }],
  [{
    id: "conflicting-task-history",
    prompt: "shared item is still not enough",
    forkPointId: "history-native-task",
    done: true,
    blocks: [],
  }, {
    id: "conflicting-task-live",
    clientMsgId: "conflicting-task-live",
    liveTaskId: "different-native-task",
    prompt: "shared item is still not enough",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "conflicting-task-shared-message",
      channel: "final",
      text: "shared native item",
      done: true,
    }],
  }],
  {
    newestHistoryId: "conflicting-task-history",
    reconcileReplayOrphans: true,
  },
);
assert.deepEqual(
  conflictingTaskSharedItem.map((turn) => turn.id),
  ["conflicting-task-history", "conflicting-task-live"],
  "an exact block overlap cannot merge rows with conflicting native tasks",
);

// Exercise the real reducer through Vite's zero-network SSR loader. The plain
// Node test output cannot import reducer.js directly because the browser build
// intentionally uses extensionless module specifiers.
const reducerHarness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const {
    createRuntime, initialState, modelCatalogScopeKey, reduce,
    MAX_TURN_BLOCKS, MAX_TURN_BLOCK_CHARS,
    OMITTED_PROCESS_ITEM_ID,
  } = await reducerHarness.ssrLoadModule("/src/reducer.ts");
  const event = (body: Record<string, unknown>): ServerEvent => ({
    v: 22, ts: 10, ...body,
  } as ServerEvent);
  assert.equal(createRuntime().sendMode, "steer",
    "Codex running input uses steer mode by default");
  const autoCompactSid = "claude-autocompact-context";
  const oldContextReport = event({
    type: "context_report",
    sid: autoCompactSid,
    total_tokens: 125_000,
    max_tokens: 500_000,
    percentage: 25,
    auto_compact_threshold_tokens: 500_000,
    raw_max_tokens: 1_000_000,
    categories: [],
  });
  let autoCompactState = {
    ...initialState,
    focusedSid: autoCompactSid,
    runtimes: {
      [autoCompactSid]: {
        ...createRuntime(),
        contextReport: oldContextReport,
        autoCompact: event({
          type: "auto_compact",
          sid: autoCompactSid,
          mode: "custom",
          threshold_tokens: 500_000,
          applied_mode: "custom",
          applied_threshold_tokens: 500_000,
          pending: false,
          phase: "stable",
          mutable: true,
        }),
      },
    },
  };
  autoCompactState = reduce(autoCompactState, {
    type: "event",
    event: event({
      type: "auto_compact",
      sid: autoCompactSid,
      mode: "custom",
      threshold_tokens: 400_000,
      applied_mode: "custom",
      applied_threshold_tokens: 500_000,
      pending: true,
      phase: "waiting_terminal",
      mutable: true,
    }),
  });
  assert.equal(
    autoCompactState.runtimes[autoCompactSid].contextReport,
    oldContextReport,
    "a desired autocompact change must retain usage from the still-live child",
  );
  autoCompactState = reduce(autoCompactState, {
    type: "event",
    event: event({
      type: "auto_compact",
      sid: autoCompactSid,
      mode: "custom",
      threshold_tokens: 400_000,
      applied_mode: "custom",
      applied_threshold_tokens: 400_000,
      pending: false,
      phase: "stable",
      mutable: true,
    }),
  });
  assert.equal(
    autoCompactState.runtimes[autoCompactSid].contextReport,
    null,
    "an applied autocompact change must invalidate the prior child context",
  );
  const refreshedContextReport = event({
    type: "context_report",
    sid: autoCompactSid,
    total_tokens: 126_000,
    max_tokens: 400_000,
    percentage: 31.5,
    auto_compact_threshold_tokens: 400_000,
    raw_max_tokens: 1_000_000,
    categories: [],
  });
  autoCompactState = reduce(autoCompactState, {
    type: "event",
    event: refreshedContextReport,
  });
  assert.equal(
    autoCompactState.runtimes[autoCompactSid].contextReport,
    refreshedContextReport,
    "the next-generation context report must repopulate applied autocompact usage",
  );
  let desktopCompletion = reduce(initialState, {
    type: "event",
    event: event({
      type: "completion_state",
      sid: "shared-completion",
      completion_id: "turn-1",
      unread: true,
      revision: 1,
    }),
  });
  let phoneCompletion = reduce(initialState, {
    type: "event",
    event: event({
      type: "completion_state",
      sid: "shared-completion",
      completion_id: "turn-1",
      unread: true,
      revision: 1,
    }),
  });
  const sharedAcknowledgement = event({
    type: "completion_state",
    sid: "shared-completion",
    completion_id: "turn-1",
    unread: false,
    revision: 2,
  });
  desktopCompletion = reduce(desktopCompletion, {
    type: "event", event: sharedAcknowledgement,
  });
  phoneCompletion = reduce(phoneCompletion, {
    type: "event", event: sharedAcknowledgement,
  });
  assert.equal(
    desktopCompletion.runtimes["shared-completion"].completion?.unread,
    false,
  );
  assert.equal(
    phoneCompletion.runtimes["shared-completion"].completion?.unread,
    false,
    "one authoritative acknowledgement clears every browser projection",
  );
  const staleCompletion = reduce(phoneCompletion, {
    type: "event",
    event: event({
      type: "completion_state",
      sid: "shared-completion",
      completion_id: "turn-1",
      unread: true,
      revision: 1,
    }),
  });
  assert.equal(
    staleCompletion.runtimes["shared-completion"].completion?.unread,
    false,
    "a replayed old unread receipt cannot resurrect a cleared badge",
  );
  const coldCatalogRepair = reduce(phoneCompletion, {
    type: "event",
    event: event({
      type: "session_list",
      engine: "codex",
      space: "code",
      sessions: [{
        session_id: "shared-completion",
        completion_id: "turn-1",
        completion_unread: false,
        completion_revision: 3,
      }],
    }),
  });
  assert.equal(
    coldCatalogRepair.runtimes["shared-completion"].completion?.revision,
    3,
    "a cold catalog repairs a browser which missed the live acknowledgement",
  );
  const coldUnreadCatalog = reduce(initialState, {
    type: "event",
    event: event({
      type: "session_list",
      engine: "claude",
      space: "code",
      sessions: [{
        session_id: "cold-unread",
        completion_id: "turn-cold",
        completion_unread: true,
        completion_revision: 1,
      }],
    }),
  });
  assert.equal(
    coldUnreadCatalog.runtimes["cold-unread"],
    undefined,
    "a cold receipt must not allocate an otherwise empty runtime",
  );
  assert.equal(
    newestCompletionProjection(
      undefined,
      catalogCompletionProjection(coldUnreadCatalog.sessions.find(
        (session: { session_id: string }) => (
          session.session_id === "cold-unread"))),
    )?.unread,
    true,
    "an unread cold session must be visible before it is resumed",
  );
  const manyColdCatalog = reduce(initialState, {
    type: "event",
    event: event({
      type: "session_list",
      engine: "claude",
      space: "code",
      sessions: Array.from(
        { length: MAX_RUNTIME_SESSIONS + 5 },
        (_, index) => ({
          session_id: `cold-unread-${index}`,
          completion_id: `turn-cold-${index}`,
          completion_unread: true,
          completion_revision: 1,
        }),
      ),
    }),
  });
  const prunedColdCatalog = reduce(manyColdCatalog, {
    type: "prune_runtimes", protectedSids: [],
  });
  assert.equal(Object.keys(prunedColdCatalog.runtimes).length, 0);
  assert.equal(prunedColdCatalog.sessions.filter((session: {
    completion_id?: string | null;
    completion_unread?: boolean | null;
    completion_revision?: number | null;
  }) => (
    catalogCompletionProjection(session)?.unread === true
  )).length, MAX_RUNTIME_SESSIONS + 5,
    "pruning cannot hide durable cold completion receipts from the sidebar");
  const migrationOwner = {
    scopeKey: "machine:code:codex",
    machineId: "machine",
    engine: "codex" as const,
    space: "code" as const,
    surfaceEpoch: 1,
    connectionGeneration: 1,
  };
  let migrationState = {
    ...initialState,
    focusedSid: "migrated-session",
    sessions: [{
      session_id: "migrated-session",
      cwd: "/repo/old",
      engine: "codex" as const,
      space: "code" as const,
    }],
    cwdByScope: { [migrationOwner.scopeKey]: "/repo/old" },
  };
  migrationState = reduce(migrationState, {
    type: "event",
    ownership: migrationOwner,
    event: event({
      type: "session_migrated",
      sid: "migrated-session",
      session_id: "migrated-session",
      previous_cwd: "/repo/old",
      cwd: "/repo/new",
      request_id: "migration-1",
    }),
  });
  assert.equal(migrationState.sessions[0].cwd, "/repo/new");
  assert.equal(
    migrationState.cwdByScope[migrationOwner.scopeKey],
    "/repo/new",
    "a focused migration updates the cwd-scoped composer defaults",
  );
  const backgroundMigration = reduce({
    ...migrationState,
    focusedSid: "other-session",
  }, {
    type: "event",
    ownership: migrationOwner,
    event: event({
      type: "session_migrated",
      sid: "migrated-session",
      session_id: "migrated-session",
      previous_cwd: "/repo/new",
      cwd: "/repo/background",
      request_id: "migration-2",
    }),
  });
  assert.equal(backgroundMigration.sessions[0].cwd, "/repo/background");
  assert.equal(
    backgroundMigration.cwdByScope[migrationOwner.scopeKey],
    "/repo/new",
    "a background migration must not replace the focused surface cwd",
  );
  const reconnectAfterOfflineMigration = reduce({
    ...migrationState,
    sessions: [{
      ...migrationState.sessions[0],
      cwd: "/repo/old",
    }],
    cwdByScope: { [migrationOwner.scopeKey]: "/repo/old" },
  }, {
    type: "event",
    ownership: migrationOwner,
    event: event({
      type: "session_list",
      engine: "codex",
      space: "code",
      sessions: [
        {
          session_id: "background-session",
          cwd: "/repo/background",
          engine: "codex",
          space: "code",
        },
        {
          session_id: "migrated-session",
          cwd: "/repo/new",
          engine: "codex",
          space: "code",
        },
      ],
    }),
  });
  assert.equal(
    reconnectAfterOfflineMigration.cwdByScope[migrationOwner.scopeKey],
    "/repo/new",
    "a reconnect catalog repairs the focused scope after an offline migration",
  );
  const unownedReconnect = reduce({
    ...reconnectAfterOfflineMigration,
    cwdByScope: { [migrationOwner.scopeKey]: "/repo/old" },
  }, {
    type: "event",
    event: event({
      type: "session_list",
      engine: "codex",
      space: "code",
      sessions: reconnectAfterOfflineMigration.sessions,
    }),
  });
  assert.equal(
    unownedReconnect.cwdByScope[migrationOwner.scopeKey],
    "/repo/old",
    "an unowned reconnect list must not update scoped cwd",
  );
  const sendModeA = "send-mode-a";
  const sendModeB = "send-mode-b";
  const isolatedSendModes = reduce({
    ...initialState,
    focusedSid: sendModeA,
    runtimes: {
      [sendModeA]: createRuntime(),
      [sendModeB]: createRuntime(),
    },
  }, {
    type: "set_send_mode",
    sid: sendModeA,
    mode: "queue",
  });
  assert.equal(isolatedSendModes.runtimes[sendModeA].sendMode, "queue");
  assert.equal(isolatedSendModes.runtimes[sendModeB].sendMode, "steer",
    "busy-send selection must not leak between sessions");
  const questionA = "question-session-a";
  const questionB = "question-session-b";
  let questionState = {
    ...initialState,
    focusedSid: questionA,
    runtimes: {
      [questionA]: createRuntime(),
      [questionB]: createRuntime(),
    },
  };
  questionState = reduce(questionState, { type: "event", event: event({
    type: "ask_user", sid: questionA, ask_id: "ask-a",
    question: "A?", options: [{ label: "Yes" }, { label: "No" }],
  }) });
  questionState = { ...questionState, focusedSid: questionB };
  questionState = reduce(questionState, {
    type: "answer_question", sid: questionA, ask_id: "ask-a",
  });
  assert.equal(questionState.runtimes[questionA].pendingQuestion, null,
    "answering after navigation must clear the originating session");
  assert.equal(questionState.runtimes[questionB].pendingQuestion, null);
  questionState = reduce(questionState, { type: "event", event: event({
    type: "ask_user", sid: questionA, ask_id: "ask-new",
    question: "New?", options: [{ label: "Yes" }, { label: "No" }],
  }) });
  questionState = reduce(questionState, { type: "event", event: event({
    type: "ask_user_closed", sid: questionA, ask_id: "ask-old",
    reason: "superseded",
  }) });
  assert.equal(questionState.runtimes[questionA].pendingQuestion?.ask_id, "ask-new",
    "a delayed close for an older prompt must not close the current prompt");
  questionState = reduce(questionState, { type: "event", event: event({
    type: "ask_user_closed", sid: questionA, ask_id: "ask-new",
    reason: "answered",
  }) });
  assert.equal(questionState.runtimes[questionA].pendingQuestion, null);
  const problemSid = "safe-problem-presentation";
  let problemState = reduce({
    ...initialState, focusedSid: problemSid,
    runtimes: { [problemSid]: createRuntime() },
  }, {
    type: "query_sent", sid: problemSid, msg_id: "problem-turn",
    prompt: "hello", ts: 1,
  });
  problemState = reduce(problemState, { type: "event", event: event({
    type: "error", sid: problemSid, msg_id: "problem-turn",
    code: "cc_crash",
    message: "provider crash at /private/token; see wrapper logs",
  }) });
  assert.equal(problemState.runtimes[problemSid].turns[0].error,
    "本次回复未完成，请重试。");
  assert.doesNotMatch(problemState.runtimes[problemSid].turns[0].error ?? "",
    /cc_crash|provider|private|wrapper/i);
  const networkProblemSid = "safe-network-problem-presentation";
  let networkProblemState = reduce({
    ...initialState, focusedSid: networkProblemSid,
    runtimes: { [networkProblemSid]: createRuntime() },
  }, {
    type: "query_sent", sid: networkProblemSid,
    msg_id: "network-problem-turn", prompt: "continue", ts: 2,
  });
  networkProblemState = reduce(networkProblemState, {
    type: "event", event: event({
      type: "error", sid: networkProblemSid,
      msg_id: "network-problem-turn", code: "cc_crash",
      message: "网络连接异常，请检查网络后重试。",
    }),
  });
  assert.equal(networkProblemState.runtimes[networkProblemSid].turns[0].error,
    "网络连接异常，请检查网络后重试。");
  const commandProblem = reduce(problemState, { type: "event", event: event({
    type: "error", sid: problemSid, code: "internal",
    message: "Traceback: secret path /private/token",
  }) });
  assert.equal(commandProblem.banner, "操作未完成，请稍后重试。");
  const incompleteClaudeSession = reduce(
    commandProblem,
    { type: "event", event: event({
      type: "error", sid: problemSid, code: "not_running",
      message: "Claude 会话历史不完整，无法恢复；可从会话菜单删除该条目。",
    }) },
  );
  assert.equal(incompleteClaudeSession.banner,
    "Claude 会话历史不完整，无法恢复；可从会话菜单删除该条目。");
  // Work/Code and engine switches restore the target surface's last accepted
  // list immediately. The authoritative refresh may take ~1s for Codex because
  // app-server is started on demand, but it must not blank the sidebar meanwhile.
  const cachedSessions = [{
    session_id: "cached-work-session",
    engine: "codex" as const,
    space: "work" as const,
    summary: "cached",
  }];
  const restored = reduce(
    { ...initialState, focusedSid: "old-code-session" },
    { type: "restore_session_list", sessions: cachedSessions },
  );
  assert.deepEqual(restored.sessions, cachedSessions);
  assert.equal(restored.focusedSid, null);
  const refocused = reduce(reduce(reduce(restored,
    { type: "enter_new_chat", cwd: "~" }),
  { type: "focus_session", sid: "cached-work-session" }),
  { type: "exit_new_chat" });
  assert.equal(refocused.focusedSid, "cached-work-session");
  assert.equal(refocused.newChat, null,
    "restoring a surface focus must also leave the temporary new-work page");
  const staleFocused = reduce({
    ...refocused,
    sessions: [{ session_id: "deleted-elsewhere", engine: "codex", space: "work" }],
    focusedSid: "deleted-elsewhere",
  }, { type: "event", event: event({
    type: "session_list", engine: "codex", space: "work", sessions: cachedSessions,
  }) });
  assert.equal(staleFocused.focusedSid, null,
    "an authoritative list must clear a session deleted by another client");
  assert.ok(staleFocused.newChat,
    "the removed transcript must not remain painted while a replacement focus is chosen");
  const ownerA = {
    scopeKey: "machine-a:code:claude", machineId: "machine-a",
    engine: "claude" as const, space: "code" as const,
    surfaceEpoch: 1, connectionGeneration: 1,
  };
  const ownerB = {
    scopeKey: "machine-b:code:codex", machineId: "machine-b",
    engine: "codex" as const, space: "code" as const,
    surfaceEpoch: 4, connectionGeneration: 2,
  };
  const scopedA = reduce(initialState, { type: "event", ownership: ownerA,
    event: event({ type: "session_focus", session_id: "a", cwd: "/work/a" }) });
  const scopedB = reduce(scopedA, { type: "event", ownership: ownerB,
    event: event({ type: "session_focus", session_id: "b", cwd: "/work/b" }) });
  assert.deepEqual(scopedB.cwdByScope, {
    [ownerA.scopeKey]: "/work/a", [ownerB.scopeKey]: "/work/b",
  });
  const unknownOwner = reduce(scopedB, { type: "event",
    event: event({ type: "session_focus", session_id: "unknown", cwd: "/deleted" }) });
  assert.deepEqual(unknownOwner.cwdByScope, scopedB.cwdByScope,
    "an unowned focus must never update any scoped cwd");
  const backgroundRekey = reduce(scopedB, { type: "event", ownership: ownerA,
    event: event({
      type: "session_rekey", old_key: "tmp-background",
      session_id: "real-background", cwd: "/work/a/new",
    }) });
  assert.equal(backgroundRekey.cwdByScope[ownerA.scopeKey], "/work/a/new");
  assert.equal(backgroundRekey.cwdByScope[ownerB.scopeKey], "/work/b",
    "a valid background rekey updates only its own scope");
  const clearedInherited = reduce(backgroundRekey, {
    type: "clear_scope_cwd", scopeKey: ownerA.scopeKey,
  });
  assert.equal(ownerA.scopeKey in clearedInherited.cwdByScope, false);

  const profileOwner = {
    ...ownerB,
    scopeKey: "machine-b:code:codex",
    codexProfileId: "stack",
  };
  let profileState = {
    ...initialState,
    newChat: {
      cwd: "/work/profile",
      cwdSource: "explicit" as const,
      model: null,
      effort: null,
      codexProfileId: "stack",
    },
  };
  profileState = reduce(profileState, {
    type: "event",
    ownership: profileOwner,
    event: event({
      type: "session_list",
      engine: "codex",
      space: "code",
      codex_profiles: [
        { id: "primary", label: "主账号" },
        { id: "stack", label: "Stack" },
      ],
      default_codex_profile_id: "primary",
      sessions: [{
        session_id: "stack@same-native-id",
        native_session_id: "same-native-id",
        codex_profile_id: "stack",
        codex_profile_label: "Stack",
        engine: "codex",
        space: "code",
      }],
    }),
  });
  assert.equal(
    profileState.codexProfileByScope[profileOwner.scopeKey], "stack",
    "the selected new-session profile survives an authoritative Code list",
  );
  assert.equal(profileState.newChat?.codexProfileId, "stack");
  profileState = reduce(profileState, { type: "event", event: event({
    type: "models",
    engine: "codex",
    codex_profile_id: "primary",
    models: [{
      id: "primary-model", display_name: "Primary", description: "",
      efforts: ["high"], default_effort: "high",
    }],
  }) });
  profileState = reduce(profileState, { type: "event", event: event({
    type: "models",
    engine: "codex",
    codex_profile_id: "stack",
    models: [{
      id: "stack-model", display_name: "Stack", description: "",
      efforts: ["xhigh"], default_effort: "xhigh",
    }],
  }) });
  assert.equal(
    profileState.catalog[modelCatalogScopeKey("codex", "primary")][0].id,
    "primary-model",
  );
  assert.equal(
    profileState.catalog[modelCatalogScopeKey("codex", "stack")][0].id,
    "stack-model",
    "late model catalogs must remain in their own CODEX_HOME lane",
  );
  const workProfileState = reduce(profileState, {
    type: "event",
    ownership: {
      ...profileOwner,
      scopeKey: "machine-b:work:codex",
      space: "work" as const,
      codexProfileId: null,
    },
    event: event({
      type: "session_list",
      engine: "codex",
      space: "work",
      codex_profiles: profileState.codexProfiles,
      default_codex_profile_id: "primary",
      sessions: [],
    }),
  });
  assert.equal(workProfileState.newChat?.codexProfileId, "stack",
    "Codex Work preserves the selected account");

  const removedProfileState = reduce(profileState, {
    type: "event",
    ownership: profileOwner,
    event: event({
      type: "session_list",
      engine: "codex",
      space: "code",
      codex_profiles: [{ id: "primary", label: "Main" }],
      default_codex_profile_id: "primary",
      sessions: [],
    }),
  });
  assert.equal(removedProfileState.newChat?.codexProfileId, "stack");
  assert.equal(
    removedProfileState.codexProfileByScope[profileOwner.scopeKey], "stack",
    "a removed selection must not silently fall back while a draft is open",
  );

  const profileCatalogFailure = reduce({
    ...profileState,
    focusedSid: "stack@same-native-id",
    sessions: [
      {
        session_id: "primary-old",
        native_session_id: "primary-old",
        codex_profile_id: "primary",
        engine: "codex",
        space: "code",
      },
      {
        session_id: "stack@same-native-id",
        native_session_id: "same-native-id",
        codex_profile_id: "stack",
        engine: "codex",
        space: "code",
      },
    ],
  }, {
    type: "event",
    ownership: profileOwner,
    event: event({
      type: "session_list",
      engine: "codex",
      space: "code",
      codex_profiles: [
        { id: "primary", label: "主账号" },
        { id: "stack", label: "Stack", error: "会话列表暂不可用" },
      ],
      default_codex_profile_id: "primary",
      sessions: [{
        session_id: "primary-new",
        native_session_id: "primary-new",
        codex_profile_id: "primary",
        engine: "codex",
        space: "code",
      }],
    }),
  });
  assert.deepEqual(
    profileCatalogFailure.sessions.map(
      (session: { session_id: string }) => session.session_id),
    ["primary-new", "stack@same-native-id"],
    "a partial catalog failure keeps only the failed profile's prior rows",
  );
  assert.equal(profileCatalogFailure.focusedSid, "stack@same-native-id",
    "a transient sibling profile failure must not eject the focused session");

  const normalizedPartialCatalog = normalizeSessionList(
    [
      {
        session_id: "primary-old",
        native_session_id: "primary-old",
        codex_profile_id: "primary",
        engine: "codex",
        space: "code",
      },
      {
        session_id: "stack@same-native-id",
        native_session_id: "same-native-id",
        codex_profile_id: "stack",
        engine: "codex",
        space: "code",
      },
    ],
    profileState.codexProfiles,
    "primary",
    event({
      type: "session_list",
      engine: "codex",
      space: "code",
      codex_profiles: [
        { id: "primary", label: "主账号" },
        { id: "stack", label: "Stack", error: "会话列表暂不可用" },
      ],
      default_codex_profile_id: "primary",
      sessions: [{
        session_id: "primary-new",
        native_session_id: "primary-new",
        codex_profile_id: "primary",
        engine: "codex",
        space: "code",
      }],
    }) as SessionList,
  );
  assert.deepEqual(
    normalizedPartialCatalog.sessions.map((session) => session.session_id),
    ["primary-new", "stack@same-native-id"],
    "App refs and reducer share the same partial-success projection",
  );

  const createdPlaceholder = reduce(initialState, {
    type: "event", ownership: ownerB,
    event: event({
      type: "session_focus", session_id: "tmp-created", cwd: "/work/new",
      request_id: "create-message",
    }),
  });
  assert.deepEqual(createdPlaceholder.sessions.map((session: { session_id: string }) =>
    session.session_id), ["tmp-created"],
  "a correlated create focus must paint a temporary sidebar row immediately");
  assert.equal(createdPlaceholder.sessions[0].engine, "codex");
  assert.equal(createdPlaceholder.sessions[0].space, "code");
  const rekeyedPlaceholder = reduce(createdPlaceholder, {
    type: "event", ownership: ownerB,
    event: event({
      type: "session_rekey", old_key: "tmp-created", session_id: "real-created",
      cwd: "/work/new",
    }),
  });
  assert.deepEqual(rekeyedPlaceholder.sessions.map((session: { session_id: string }) =>
  session.session_id), ["real-created"],
  "rekey must atomically migrate the temporary sidebar row");
  const profiledPlaceholder = reduce(initialState, {
    type: "event", ownership: profileOwner,
    event: event({
      type: "session_focus", session_id: "tmp-profiled", cwd: "/work/new",
      request_id: "create-profiled",
    }),
  });
  const profiledRekey = reduce(profiledPlaceholder, {
    type: "event", ownership: profileOwner,
    event: event({
      type: "session_rekey", old_key: "tmp-profiled",
      session_id: "stack@native-created", cwd: "/work/new",
    }),
  });
  assert.equal(profiledRekey.sessions[0].native_session_id, "native-created",
    "a rekeyed Codex placeholder must expose the native id before SessionList");
  const lifecycleRekey = reduce({
    ...initialState,
    focusedSid: "tmp-running",
    runtimes: {
      "tmp-running": {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        lastLifecycleSeq: 12,
      },
      "real-running": {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
        lastLifecycleSeq: 4,
      },
    },
  }, {
    type: "event",
    ownership: ownerB,
    event: event({
      type: "session_rekey",
      old_key: "tmp-running",
      session_id: "real-running",
      cwd: "/work/new",
    }),
  });
  assert.equal(
    lifecycleRekey.runtimes["real-running"].state,
    "running",
    "rekey keeps the lifecycle from the runtime with the newer lifecycle sequence",
  );
  assert.equal(lifecycleRekey.focusedSid, "real-running");
  const aliasRekey = reduce({
    ...initialState,
    focusedSid: "tmp-alias-running",
    runtimes: {
      "tmp-alias-running": {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "rekey-client-turn",
          historyTurnId: "rekey-native-turn",
          prompt: "same rekeyed turn",
          blocks: [{
            kind: "text",
            message_id: "rekey-live-answer",
            channel: "final",
            text: "live tail",
            done: false,
          }],
          done: false,
        }],
      },
      "real-alias-running": {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
        turns: [{
          id: "rekey-native-turn",
          clientMsgId: "rekey-client-turn",
          prompt: "same rekeyed turn",
          blocks: [{
            kind: "text",
            message_id: "rekey-history-answer",
            channel: "final",
            text: "history",
            done: true,
          }],
          done: true,
        }],
      },
    },
  }, {
    type: "event",
    ownership: ownerB,
    event: event({
      type: "session_rekey",
      old_key: "tmp-alias-running",
      session_id: "real-alias-running",
      cwd: "/work/new",
    }),
  });
  const aliasRekeyTurns =
    aliasRekey.runtimes["real-alias-running"].turns;
  assert.equal(
    aliasRekeyTurns.length,
    1,
    "rekey merges a temp client id with its already-replayed native alias",
  );
  assert.deepEqual(
    {
      id: aliasRekeyTurns[0].id,
      historyTurnId: aliasRekeyTurns[0].historyTurnId,
      done: aliasRekeyTurns[0].done,
    },
    {
      id: "rekey-client-turn",
      historyTurnId: "rekey-native-turn",
      done: false,
    },
    "rekey preserves the live temp identity while retaining native history authority",
  );
  assert.equal(
    aliasRekeyTurns[0].blocks.some((block: Block) =>
      block.kind === "text" && block.text.includes("live tail")),
    true,
    "rekey cannot discard the live tail when its native alias already exists",
  );
  const authoritativeCreated = reduce(rekeyedPlaceholder, {
    type: "event", ownership: ownerB,
    event: event({
      type: "session_list", engine: "codex", space: "code", sessions: [{
        session_id: "real-created", engine: "codex", space: "code",
        summary: "authoritative title",
      }],
    }),
  });
  assert.equal(authoritativeCreated.sessions[0].summary, "authoritative title");

  const acceptanceSid = "query-acceptance";
  const acceptanceOtherSid = "query-acceptance-other";
  let acceptanceState = {
    ...initialState,
    focusedSid: acceptanceSid,
    runtimes: {
      [acceptanceSid]: { ...createRuntime(), syncReady: true },
      [acceptanceOtherSid]: { ...createRuntime(), syncReady: true },
    },
  };
  acceptanceState = reduce(acceptanceState, {
    type: "query_sent", sid: acceptanceSid, prompt: "first",
    msg_id: "acceptance-first", ts: 900,
  });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].acceptancePending,
    "acceptance-first",
  );
  acceptanceState = reduce(acceptanceState, {
    type: "query_sent", sid: acceptanceSid, prompt: "must be deferred",
    msg_id: "acceptance-second", ts: 901,
  });
  assert.deepEqual(
    acceptanceState.runtimes[acceptanceSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["acceptance-first"],
    "a second idle submit cannot create another optimistic turn before acceptance",
  );
  acceptanceState = reduce(acceptanceState, {
    type: "set_pending", sid: acceptanceSid,
    query: { prompt: "must be deferred" },
  });
  acceptanceState = reduce(acceptanceState, {
    type: "set_pending", sid: acceptanceOtherSid,
    query: { prompt: "other session" },
  });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].pendingSend?.prompt,
    "must be deferred",
  );
  assert.equal(
    acceptanceState.runtimes[acceptanceOtherSid].pendingSend?.prompt,
    "other session",
    "deferred queries are targeted by sid rather than current focus",
  );
  acceptanceState = reduce(acceptanceState, { type: "event", event: event({
    type: "command_ack", client_id: "browser", cmd_id: "acceptance-command",
  }) });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].acceptancePending,
    "acceptance-first",
    "command ACK must not unlock a direct query",
  );
  acceptanceState = reduce(acceptanceState, { type: "event", event: event({
    type: "error", sid: acceptanceOtherSid, msg_id: "unrelated",
    code: "busy", message: "other",
  }) });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].acceptancePending,
    "acceptance-first",
    "another session's error cannot release this query",
  );
  acceptanceState = reduce(acceptanceState, { type: "event", event: event({
    type: "error", sid: acceptanceSid, msg_id: "acceptance-first",
    code: "busy", message: "rejected",
  }) });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].acceptancePending,
    null,
    "the exact correlated Error releases query acceptance",
  );
  acceptanceState = reduce(acceptanceState, {
    type: "query_sent", sid: acceptanceSid, prompt: "offline",
    msg_id: "acceptance-offline", ts: 902,
  });
  acceptanceState = reduce(acceptanceState, { type: "event", event: event({
    type: "error", sid: acceptanceSid, msg_id: "acceptance-offline",
    code: "wrapper_offline", message: "offline",
  }) });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].acceptancePending,
    "acceptance-offline",
    "offline/reconnect keeps the unaccepted query latched for outbox replay",
  );
  acceptanceState = reduce(acceptanceState, { type: "event", event: event({
    type: "user_msg", sid: acceptanceSid, msg_id: "acceptance-offline",
    prompt: "offline",
  }) });
  assert.equal(
    acceptanceState.runtimes[acceptanceSid].acceptancePending,
    null,
    "the authoritative user echo releases acceptance after reconnect",
  );

  // A materialized History uses transcript/native ids, not the browser msg_id.
  // Freeze the pre-send authoritative head so a genuinely appended newest turn
  // can recover a live UserMsg/TurnBinding which fell outside replay. Repeated
  // old prompts and a different newly-appended prompt are not acceptance proof.
  const nativeAcceptanceSid = "native-history-acceptance";
  const nativeOldTurn = {
    id: "native-old", prompt: "继续", blocks: [], done: true,
    ts: 1_000, doneTs: 1_100, detailEventCount: 0, detailLoaded: false,
  };
  let nativeAcceptanceState = reduce({
    ...initialState,
    focusedSid: nativeAcceptanceSid,
    runtimes: { [nativeAcceptanceSid]: createRuntime() },
  }, { type: "event", event: event({
    type: "history", sid: nativeAcceptanceSid,
    session_id: nativeAcceptanceSid, revision: "native-r1",
    generation: "native-g1", build_seq: 1, live_seq: 10,
    detail: "summary", events: [], turns: [nativeOldTurn],
    has_more: false, newest_id: "native-old", oldest_id: "native-old",
  }) });
  nativeAcceptanceState = reduce(nativeAcceptanceState, {
    type: "query_sent", sid: nativeAcceptanceSid, prompt: "继续",
    msg_id: "browser-native-pending", ts: 900_000,
    images: [{ media_type: "image/png", data: "optimistic-image" }],
  });
  nativeAcceptanceState = reduce(nativeAcceptanceState, {
    type: "event", event: event({
      type: "history", sid: nativeAcceptanceSid,
      session_id: nativeAcceptanceSid, revision: "native-r1",
      generation: "native-g1", build_seq: 1, live_seq: 10,
      detail: "summary", events: [], turns: [nativeOldTurn],
      has_more: false, newest_id: "native-old", oldest_id: "native-old",
    }),
  });
  assert.equal(
    nativeAcceptanceState.runtimes[nativeAcceptanceSid].acceptancePending,
    "browser-native-pending",
    "an old materialized row with the same prompt cannot release acceptance",
  );
  const unrelatedNativeTurn = {
    id: "native-unrelated", prompt: "another writer", blocks: [], done: true,
    ts: 2_000, doneTs: 2_100, detailEventCount: 0, detailLoaded: false,
  };
  nativeAcceptanceState = reduce(nativeAcceptanceState, {
    type: "event", event: event({
      type: "history", sid: nativeAcceptanceSid,
      session_id: nativeAcceptanceSid, revision: "native-r1",
      generation: "native-g1", build_seq: 2, live_seq: 11,
      detail: "summary", events: [],
      turns: [nativeOldTurn, unrelatedNativeTurn],
      has_more: false, newest_id: "native-unrelated", oldest_id: "native-old",
    }),
  });
  assert.equal(
    nativeAcceptanceState.runtimes[nativeAcceptanceSid].acceptancePending,
    "browser-native-pending",
    "a different newly-materialized prompt cannot release acceptance",
  );
  const acceptedNativeTurn = {
    id: "native-accepted", prompt: "继续", blocks: [], done: true,
    imageRefs: [{
      image_id: "native-accepted-image", media_type: "image/png" as const,
      width: 1200, height: 800, byte_size: 1024,
    }],
    // Deliberately far from the browser clock: the missing live echo never
    // replaced the optimistic timestamp with authoritative server time.
    ts: 3_000, doneTs: 3_100, detailEventCount: 0, detailLoaded: false,
  };
  nativeAcceptanceState = reduce(nativeAcceptanceState, {
    type: "event", event: event({
      type: "history", sid: nativeAcceptanceSid,
      session_id: nativeAcceptanceSid, revision: "native-r1",
      generation: "native-g1", build_seq: 3, live_seq: 12,
      detail: "summary", events: [],
      turns: [nativeOldTurn, unrelatedNativeTurn, acceptedNativeTurn],
      has_more: false, newest_id: "native-accepted", oldest_id: "native-old",
    }),
  });
  assert.equal(
    nativeAcceptanceState.runtimes[nativeAcceptanceSid].acceptancePending,
    null,
    "a matching appended native History head releases the browser acceptance",
  );
  assert.deepEqual(
    nativeAcceptanceState.runtimes[nativeAcceptanceSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["native-old", "native-unrelated", "browser-native-pending"],
    "native History binds into the optimistic row even when client/server clocks differ",
  );
  const acceptedImageTurn =
    nativeAcceptanceState.runtimes[nativeAcceptanceSid].turns.at(-1)!;
  assert.equal(acceptedImageTurn.historyTurnId, "native-accepted",
    "canonical image requests must retain the native transcript turn id");
  assert.equal(acceptedImageTurn.images, undefined,
    "the accepted history image reference replaces its optimistic inline body");
  assert.equal(
    acceptedImageTurn.imageRefs?.[0]?.image_id,
    "native-accepted-image",
  );

  const exactEndSid = "exact-turn-end-owner";
  let exactEndState = {
    ...acceptanceState,
    focusedSid: exactEndSid,
    runtimes: {
      ...acceptanceState.runtimes,
      [exactEndSid]: {
        ...createRuntime(),
        acceptancePending: "promptless-tail",
        turns: [{
          id: "older-browser", forkPointId: "older-native",
          prompt: "older", blocks: [], done: false,
        }, {
          id: "promptless-tail", prompt: "", blocks: [{
            kind: "process",
            item_id: "promptless-claude-process",
            processKind: "agent",
            phase: "start",
            status: "running",
            turn_id: "promptless-tail",
            title: "Claude child process",
            done: false,
          }], done: false,
        }],
      },
    },
  };
  exactEndState = reduce(exactEndState, { type: "event", event: event({
    type: "turn_end", sid: exactEndSid, turn_id: "older-native",
    result: { subtype: "success", duration_ms: 10, is_error: false },
  }) });
  assert.equal(exactEndState.runtimes[exactEndSid].turns[0].done, true);
  assert.equal(exactEndState.runtimes[exactEndSid].turns[1].done, false,
    "a delayed TurnEnd closes its exact owner, not the promptless live tail");
  assert.equal(
    exactEndState.runtimes[exactEndSid].acceptancePending,
    "promptless-tail",
    "a delayed older TurnEnd cannot unlock a newer query acceptance latch",
  );
  exactEndState = reduce(exactEndState, { type: "event", event: event({
    type: "turn_end", sid: exactEndSid, turn_id: "new-native",
    result: { subtype: "success", duration_ms: 11, is_error: false },
  }) });
  assert.equal(exactEndState.runtimes[exactEndSid].turns[1].done, true,
    "an unbound promptless tail retains the safe legacy TurnEnd fallback");
  assert.equal(
    exactEndState.runtimes[exactEndSid].turns[1].forkPointId,
    "new-native",
  );
  assert.equal(
    exactEndState.runtimes[exactEndSid].turns[1].blocks[0]?.done,
    true,
    "Claude child process ids cannot disable the unbound TurnEnd fallback",
  );
  assert.equal(exactEndState.runtimes[exactEndSid].acceptancePending, null);

  const lateClaudeDetailSid = "claude-late-terminal-detail";
  const lateDetailInputTurn: Turn = {
    id: "claude-late-detail-turn",
    forkPointId: "claude-late-detail-native",
    prompt: "finish before detail arrives",
    blocks: [{
      kind: "process",
      item_id: "live-process-before-terminal",
      processKind: "command",
      phase: "start",
      status: "running",
      turn_id: "claude-late-detail-native",
      title: "live process",
      done: false,
    }],
    detailProjection: {
      segments: [],
      blocks: [{
        kind: "tool",
        message_id: "detail-message-before-terminal",
        tool_use_id: "detail-tool-before-terminal",
        tool: "Read",
        input: { file_path: "README.md" },
        done: false,
      }],
      capped: false,
      hasMore: false,
      oldestCursor: null,
      hasNewer: false,
      newerCursor: null,
    },
    done: false,
  };
  let lateClaudeDetailState = {
    ...initialState,
    focusedSid: lateClaudeDetailSid,
    runtimes: {
      [lateClaudeDetailSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        historyRevision: "claude-late-detail-r1",
        turns: [lateDetailInputTurn],
      },
    },
  };
  lateClaudeDetailState = reduce(lateClaudeDetailState, {
    type: "event", event: event({
      type: "turn_end", sid: lateClaudeDetailSid, seq: 20,
      turn_id: "claude-late-detail-native",
      result: { subtype: "success", duration_ms: 20, is_error: false },
    }),
  });
  const afterClaudeTurnEnd =
    lateClaudeDetailState.runtimes[lateClaudeDetailSid].turns[0];
  assert.equal(afterClaudeTurnEnd.blocks[0].done, true,
    "TurnEnd closes the live process present at its terminal boundary");
  assert.equal(afterClaudeTurnEnd.detailProjection?.blocks[0].done, true,
    "TurnEnd also closes the detail projection already on screen");
  assert.equal(lateDetailInputTurn.blocks[0].done, false,
    "TurnEnd does not mutate reducer input live blocks");
  assert.equal(lateDetailInputTurn.detailProjection?.blocks[0].done, false,
    "TurnEnd does not mutate reducer input detail blocks");

  lateClaudeDetailState = reduce(lateClaudeDetailState, {
    type: "event", event: event({
      type: "process", sid: lateClaudeDetailSid, seq: 21,
      item_id: "real-background-after-terminal", kind: "agent",
      phase: "start", status: "running",
      turn_id: "claude-late-detail-native",
      title: "real background work",
    }),
  });
  const withBackgroundProcess =
    lateClaudeDetailState.runtimes[lateClaudeDetailSid].turns[0];
  assert.equal(
    withBackgroundProcess.blocks.find((block: Block) =>
      block.kind === "process"
      && block.item_id === "real-background-after-terminal")?.done,
    false,
    "genuine process activity after TurnEnd may reactivate the completed row",
  );

  lateClaudeDetailState = reduce(lateClaudeDetailState, {
    type: "event", event: event({
      type: "turn_detail", sid: lateClaudeDetailSid,
      session_id: lateClaudeDetailSid,
      turn_id: "claude-late-detail-turn",
      revision: "claude-late-detail-r1",
      authoritative: true,
      before: null,
      has_more: false,
      oldest_cursor: null,
      has_newer: false,
      newer_cursor: null,
      events: [{
        ...event({
          type: "tool_use", sid: lateClaudeDetailSid, seq: 10,
          message_id: "old-detail-message",
          tool_use_id: "old-detail-tool",
          tool: "Read", input: { file_path: "old.md" },
        }),
      }],
    }),
  });
  const afterLateClaudeDetail =
    lateClaudeDetailState.runtimes[lateClaudeDetailSid].turns[0];
  assert.equal(afterLateClaudeDetail.detailProjection?.blocks[0].done, true,
    "TurnDetail arriving after TurnEnd closes its newly-installed old tool");
  assert.equal(
    afterLateClaudeDetail.blocks.find((block: Block) =>
      block.kind === "process"
      && block.item_id === "real-background-after-terminal")?.done,
    false,
    "old TurnDetail cannot close real background work that began after TurnEnd",
  );

  const steeredTurnSid = "codex-turn-steered";
  const steeredNativeTurnId = "native-task-steered";
  let steeredTurnState = {
    ...initialState,
    focusedSid: steeredTurnSid,
    runtimes: {
      [steeredTurnSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        acceptancePending: "steered-follow-up",
        turns: [{
          id: "steered-original",
          forkPointId: steeredNativeTurnId,
          prompt: "start the task",
          blocks: [{
            kind: "text" as const,
            message_id: "steered-original-commentary",
            channel: "commentary" as const,
            text: "working",
            done: false,
          }, {
            kind: "process" as const,
            item_id: "steered-original-process",
            processKind: "command" as const,
            phase: "start" as const,
            status: "running" as const,
            turn_id: steeredNativeTurnId,
            title: "original native work",
            done: false,
          }],
          done: false,
          ts: 1_000,
        }],
      },
    },
  };
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "turn_binding", sid: steeredTurnSid, seq: 20,
      msg_id: "steered-original", turn_id: steeredNativeTurnId,
    }),
  });
  steeredTurnState = reduce(steeredTurnState, {
    type: "steer_sent",
    sid: steeredTurnSid,
    prompt: "change direction",
    msg_id: "steered-follow-up",
    images: [{ media_type: "image/png", data: "steered-image" }],
    files: [{ filename: "steered.txt", data: "steered-file" }],
    ts: 10_500,
  });
  assert.deepEqual(
    steeredTurnState.runtimes[steeredTurnSid].turns.map((turn: Turn) => ({
      id: turn.id,
      prompt: turn.prompt,
      done: turn.done,
    })),
    [{
      id: "steered-original",
      prompt: "start the task",
      done: false,
    }, {
      id: "steered-follow-up",
      prompt: "change direction",
      done: false,
    }],
    "a sent steer paints immediately without waiting for the wrapper echo",
  );
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].acceptancePending,
    "steered-follow-up",
  );
  const preFenceBinding =
    steeredTurnState.runtimes[steeredTurnSid].pendingLiveBinding;
  assert.equal(
    preFenceBinding?.msgId,
    "steered-original",
    "the active turn binding remains available until the steer fence",
  );
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "assistant_msg_start",
      sid: steeredTurnSid,
      seq: 21,
      message_id: "new-pre-fence-message",
      channel: "commentary",
    }),
  });
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "delta",
      sid: steeredTurnSid,
      seq: 22,
      message_id: "new-pre-fence-message",
      channel: "commentary",
      text: "new item admitted before the steer fence",
    }),
  });
  assert.ok(
    steeredTurnState.runtimes[steeredTurnSid].turns[0].blocks.some(
      (block: Block) => block.kind === "text"
        && block.message_id === "new-pre-fence-message"),
    "a new pre-fence assistant item still belongs to the pre-steer segment",
  );
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[1].blocks.length,
    0,
    "an unseen pre-fence item cannot leak into the optimistic steer segment",
  );
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "delta",
      sid: steeredTurnSid,
      message_id: "steered-original-commentary",
      channel: "commentary",
      text: " before the steer fence",
    }),
  });
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[0].blocks[0].kind
      === "text"
      ? steeredTurnState.runtimes[steeredTurnSid].turns[0].blocks[0].text
      : "",
    "working before the steer fence",
    "a delayed assistant delta must return to the turn which owns its message id",
  );
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[1].blocks.length,
    0,
    "the delayed pre-steer delta cannot leak into the optimistic steer segment",
  );
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "tool_use",
      sid: steeredTurnSid,
      message_id: "steered-original-commentary",
      tool_use_id: "steered-original-tool",
      tool: "Read",
      input: { file_path: "/tmp/old" },
    }),
  });
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "tool_use",
      sid: steeredTurnSid,
      message_id: "steered-original-commentary",
      tool_use_id: "steered-original-tool",
      tool: "Read",
      input: { file_path: "/tmp/old-updated" },
    }),
  });
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[0].blocks.filter(
      (block: Block) => block.kind === "tool"
        && block.tool_use_id === "steered-original-tool").length,
    1,
    "a replayed delayed tool start updates its original owner exactly once",
  );
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[1].blocks.length,
    0,
    "a delayed tool start cannot attach to the optimistic steer segment",
  );
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "turn_steered",
      sid: steeredTurnSid,
      msg_id: "steered-follow-up",
      turn_id: steeredNativeTurnId,
      prompt: "change direction",
      images: [{ media_type: "image/png", data: "steered-image" }],
      files: [{ filename: "steered.txt" }],
      ts: 11,
    }),
  });
  const firstSteeredTurns = steeredTurnState.runtimes[steeredTurnSid].turns;
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].acceptancePending,
    null,
    "TurnSteered is the narrative acceptance boundary for the steer command",
  );
  assert.deepEqual(firstSteeredTurns.map((turn: Turn) => ({
    id: turn.id,
    prompt: turn.prompt,
    done: turn.done,
    forkPointId: turn.forkPointId,
    liveTaskId: turn.liveTaskId,
  })), [{
    id: "steered-original",
    prompt: "start the task",
    done: true,
    forkPointId: undefined,
    liveTaskId: undefined,
  }, {
    id: "steered-follow-up",
    prompt: "change direction",
    done: false,
    forkPointId: undefined,
    liveTaskId: steeredNativeTurnId,
  }], "TurnSteered atomically closes the prior segment and opens the steered one");
  assert.equal(firstSteeredTurns[0].durationMs, undefined,
    "a steer fence without one authoritative clock domain has unknown duration");
  assert.equal(firstSteeredTurns[0].doneTs, 11_000);
  assert.ok(firstSteeredTurns[0].blocks.every((block: Block) => block.done),
    "closing the old segment also settles every open block it owned");
  assert.equal(firstSteeredTurns[1].images?.[0]?.data, "steered-image");
  assert.deepEqual(firstSteeredTurns[1].files, [{
    filename: "steered.txt", data: "",
  }]);

  const idleSnapshotRepairSid = "idle-history-repairs-dangling";
  let idleRepairState = {
    ...initialState,
    focusedSid: idleSnapshotRepairSid,
    runtimes: {
      [idleSnapshotRepairSid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
        turns: [{
          id: "dangling-history-turn",
          prompt: "finished while disconnected",
          done: false,
          blocks: [{
            kind: "text" as const,
            message_id: "dangling-history-answer",
            text: "partial answer",
            done: false,
            channel: "final" as const,
          }],
        }],
      },
    },
  };
  idleRepairState = reduce(idleRepairState, {
    type: "event", event: event({
      type: "history",
      session_id: idleSnapshotRepairSid,
      revision: "idle-r1",
      generation: "idle-g1",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      has_more: false,
      in_progress: false,
      events: [],
      turns: [{
        id: "dangling-history-turn",
        prompt: "finished while disconnected",
        done: false,
        blocks: [{
          kind: "text",
          message_id: "dangling-history-answer",
          text: "partial answer",
          done: true,
          channel: "final",
        }],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    idleRepairState.runtimes[idleSnapshotRepairSid].turns[0].done,
    true,
    "an authoritative idle History closes a dangling row even after an idle snapshot",
  );
  assert.equal(
    idleRepairState.runtimes[idleSnapshotRepairSid].turns[0].interrupted,
    false,
    "an idle authoritative transcript must not invent a user interruption",
  );
  assert.equal(
    idleRepairState.runtimes[idleSnapshotRepairSid].turns[0].error,
    undefined,
    "normal idle reconciliation must not show an unknown-terminal warning",
  );

  const ambiguousIdleSid = "idle-history-keeps-ambiguous-terminal";
  let ambiguousIdleState = {
    ...initialState,
    focusedSid: ambiguousIdleSid,
    runtimes: {
      [ambiguousIdleSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "ambiguous-idle-turn",
          prompt: "crashed before a terminal",
          done: false,
          blocks: [{
            kind: "text" as const,
            message_id: "ambiguous-partial-answer",
            text: "partial answer",
            done: false,
            channel: "final" as const,
          }],
        }],
      },
    },
  };
  ambiguousIdleState = reduce(ambiguousIdleState, {
    type: "event", event: event({
      type: "history",
      session_id: ambiguousIdleSid,
      revision: "ambiguous-r1",
      generation: "ambiguous-g1",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      has_more: false,
      in_progress: false,
      events: [],
      turns: [{
        id: "ambiguous-idle-turn",
        prompt: "crashed before a terminal",
        done: false,
        blocks: [{
          kind: "text",
          message_id: "ambiguous-partial-answer",
          text: "partial answer",
          done: false,
          channel: "final",
        }],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    ambiguousIdleState.runtimes[ambiguousIdleSid].turns[0].interrupted,
    true,
    "idle alone cannot turn a partial dangling answer into success",
  );
  assert.match(
    ambiguousIdleState.runtimes[ambiguousIdleSid].turns[0].error ?? "",
    /终止状态/,
    "an ambiguous terminal remains visibly distinguishable from success",
  );

  const optimisticIdleSid = "idle-history-protects-optimistic";
  let optimisticIdleState = reduce({
    ...initialState,
    focusedSid: optimisticIdleSid,
    runtimes: {
      [optimisticIdleSid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
      },
    },
  }, {
    type: "query_sent",
    sid: optimisticIdleSid,
    prompt: "not accepted yet",
    msg_id: "optimistic-idle-query",
    ts: 20_000,
  });
  optimisticIdleState = reduce(optimisticIdleState, {
    type: "event", event: event({
      type: "history",
      session_id: optimisticIdleSid,
      revision: "optimistic-idle-r1",
      generation: "optimistic-idle-g1",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      has_more: false,
      in_progress: false,
      events: [],
      turns: [],
    }),
  });
  assert.equal(
    optimisticIdleState.runtimes[optimisticIdleSid].turns[0].done,
    false,
    "idle History cannot close a query which is still awaiting acceptance",
  );

  const acceptedIdleSid = "idle-history-confirms-and-closes";
  let acceptedIdleState = reduce({
    ...initialState,
    focusedSid: acceptedIdleSid,
    runtimes: {
      [acceptedIdleSid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
      },
    },
  }, {
    type: "query_sent",
    sid: acceptedIdleSid,
    prompt: "accepted but terminal was lost",
    msg_id: "accepted-idle-query",
    ts: 21_000,
  });
  acceptedIdleState = reduce(acceptedIdleState, {
    type: "event", event: event({
      type: "history",
      session_id: acceptedIdleSid,
      revision: "accepted-idle-r1",
      generation: "accepted-idle-g1",
      build_seq: 1,
      live_seq: 1,
      detail: "summary",
      has_more: false,
      in_progress: false,
      events: [],
      turns: [{
        id: "accepted-idle-query",
        prompt: "accepted but terminal was lost",
        done: false,
        blocks: [{
          kind: "text",
          message_id: "accepted-idle-answer",
          text: "completed answer",
          done: true,
          channel: "final",
        }],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    acceptedIdleState.runtimes[acceptedIdleSid].acceptancePending,
    null,
    "the authoritative row confirms the optimistic query",
  );
  assert.equal(
    acceptedIdleState.runtimes[acceptedIdleSid].turns[0].done,
    true,
    "the same idle History must close the just-confirmed dangling row",
  );
  assert.equal(
    acceptedIdleState.runtimes[acceptedIdleSid].turns[0].interrupted,
    false,
    "a confirmed idle row without an explicit abort must settle normally",
  );

  const interruptedClaudeSid = "claude-interrupted-live-detail";
  let interruptedClaudeState = {
    ...initialState,
    focusedSid: interruptedClaudeSid,
    runtimes: {
      [interruptedClaudeSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        historyRevision: "claude-live-r1",
        historyGeneration: "claude-live-g1",
        historyBuildSeq: 1,
        historyLiveSeq: 10,
      },
    },
  };
  for (const liveEvent of [
    {
      type: "user_msg", sid: interruptedClaudeSid, seq: 11,
      msg_id: "claude-live-turn", prompt: "inspect",
    },
    {
      type: "assistant_msg_start", sid: interruptedClaudeSid, seq: 12,
      message_id: "claude-thinking", channel: "thinking",
    },
    {
      type: "delta", sid: interruptedClaudeSid, seq: 13,
      message_id: "claude-thinking", channel: "thinking",
      text: "visible reasoning",
    },
    {
      type: "assistant_msg_end", sid: interruptedClaudeSid, seq: 14,
      message_id: "claude-thinking", channel: "thinking",
    },
    {
      type: "tool_use", sid: interruptedClaudeSid, seq: 15,
      message_id: "claude-tools", tool_use_id: "claude-tool",
      tool: "Read", input: { file_path: "README.md" },
    },
    {
      type: "tool_result", sid: interruptedClaudeSid, seq: 16,
      tool_use_id: "claude-tool", content: "ok", is_error: false,
    },
    {
      type: "assistant_msg_start", sid: interruptedClaudeSid, seq: 17,
      message_id: "claude-final", channel: "final",
    },
    {
      type: "delta", sid: interruptedClaudeSid, seq: 18,
      message_id: "claude-final", channel: "final", text: "partial final",
    },
    {
      type: "assistant_msg_end", sid: interruptedClaudeSid, seq: 19,
      message_id: "claude-final", channel: "final",
    },
    {
      type: "turn_end", sid: interruptedClaudeSid, seq: 20,
      turn_id: "claude-native-turn",
      result: {
        subtype: "error_during_execution",
        duration_ms: 750,
        is_error: true,
      },
    },
  ]) {
    interruptedClaudeState = reduce(interruptedClaudeState, {
      type: "event", event: event(liveEvent),
    });
  }
  const interruptedSummary = (buildSeq: number) => event({
    type: "history",
    session_id: interruptedClaudeSid,
    revision: "claude-live-r1",
    generation: "claude-live-g1",
    build_seq: buildSeq,
    live_seq: 20,
    detail: "summary",
    authoritative: true,
    in_progress: false,
    has_more: false,
    events: [],
    turns: [{
      id: "claude-live-turn",
      forkPointId: "claude-native-turn",
      prompt: "inspect",
      done: true,
      interrupted: true,
      blocks: [{
        kind: "text",
        message_id: "history-final",
        text: "partial final",
        channel: "final",
        done: true,
      }],
      detailEventCount: 4,
      detailLoaded: false,
    }],
  });
  interruptedClaudeState = reduce(interruptedClaudeState, {
    type: "event", event: interruptedSummary(2),
  });
  const interruptedClaudeTurn =
    interruptedClaudeState.runtimes[interruptedClaudeSid].turns[0];
  const describeInterruptedBlock = (
    block: { kind: string; channel?: string; text?: string },
  ) => block.kind === "text" ? `${block.channel}:${block.text}` : block.kind;
  assert.deepEqual(
    interruptedClaudeTurn.blocks.map(describeInterruptedBlock),
    ["final:partial final"],
    "authoritative summary keeps ownership of the final answer",
  );
  assert.deepEqual(
    interruptedClaudeTurn.detailProjection?.blocks.map(describeInterruptedBlock),
    ["thinking:visible reasoning", "tool"],
    "the process already painted before an interrupt survives summary install",
  );
  interruptedClaudeState = reduce(interruptedClaudeState, {
    type: "event", event: interruptedSummary(3),
  });
  assert.deepEqual(
    interruptedClaudeState.runtimes[interruptedClaudeSid].turns[0]
      .detailProjection?.blocks.map(describeInterruptedBlock),
    ["thinking:visible reasoning", "tool"],
    "repeated summary refreshes do not duplicate restored live detail",
  );
  interruptedClaudeState = reduce(interruptedClaudeState, {
    type: "event", event: event({
      type: "history_invalidated",
      session_id: interruptedClaudeSid,
      revision: "claude-live-r2",
    }),
  });
  assert.deepEqual(
    interruptedClaudeState.runtimes[interruptedClaudeSid].turns,
    [],
    "rollback invalidation removes the completed local projection",
  );
  interruptedClaudeState = reduce(interruptedClaudeState, {
    type: "event", event: event({
      ...interruptedSummary(1),
      revision: "claude-live-r2",
      build_seq: 1,
    }),
  });
  assert.equal(
    interruptedClaudeState.runtimes[interruptedClaudeSid].turns[0]
      .detailProjection,
    undefined,
    "a replacement revision cannot resurrect pre-rollback live process",
  );

  const historyAliasSid = "history-client-message-alias";
  let historyAliasState = reduce({
    ...initialState,
    focusedSid: historyAliasSid,
    runtimes: {
      [historyAliasSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
      },
    },
  }, {
    type: "steer_sent",
    sid: historyAliasSid,
    prompt: "alias steer",
    msg_id: "browser-alias-steer",
    ts: 22_000,
  });
  historyAliasState = reduce(historyAliasState, {
    type: "event", event: event({
      type: "history",
      session_id: historyAliasSid,
      revision: "alias-r1",
      generation: "alias-g1",
      build_seq: 1,
      live_seq: 2,
      detail: "summary",
      has_more: false,
      in_progress: false,
      events: [],
      turns: [{
        id: "native-alias-steer",
        clientMsgId: "browser-alias-steer",
        prompt: "alias steer",
        done: true,
        blocks: [],
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    historyAliasState.runtimes[historyAliasSid].acceptancePending,
    null,
    "History clientMsgId must release the same reducer latch as RelayWs",
  );
  assert.deepEqual(
    historyAliasState.runtimes[historyAliasSid].turns.map(
      (turn: Turn) => turn.id),
    ["browser-alias-steer"],
    "a native History alias reconciles into the optimistic steer exactly once",
  );

  const delayedNativeAlias = mergeInitialHistory(
    [{
      id: "native-delayed-user",
      clientMsgId: "browser-delayed-steer",
      prompt: "guide from Remote",
      ts: 22_200,
      done: true,
      blocks: [{
        kind: "text" as const,
        message_id: "native-delayed-answer",
        text: "native answer",
        done: true,
      }],
    }],
    [
      {
        id: "browser-delayed-steer",
        clientMsgId: "browser-delayed-steer",
        prompt: "guide from Remote",
        ts: 22_100,
        done: false,
        blocks: [],
      },
      {
        id: "native-delayed-user",
        prompt: "guide from Remote",
        ts: 22_200,
        done: true,
        blocks: [{
          kind: "text" as const,
          message_id: "native-delayed-answer",
          text: "native answer",
          done: true,
        }],
      },
    ],
  );
  assert.deepEqual(
    delayedNativeAlias.map((turn) => ({
      id: turn.id,
      historyTurnId: turn.historyTurnId,
      messages: turn.blocks.map((block) => block.kind === "text"
        ? block.message_id : null),
    })),
    [{
      id: "browser-delayed-steer",
      historyTurnId: "native-delayed-user",
      messages: ["native-delayed-answer"],
    }],
    "one canonical alias must absorb both live ids without dropping native history",
  );

  const liveSteerAliasSid = "live-steer-official-alias";
  let liveSteerAliasState = {
    ...initialState,
    focusedSid: liveSteerAliasSid,
    runtimes: {
      [liveSteerAliasSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
      },
    },
  };
  liveSteerAliasState = reduce(liveSteerAliasState, {
    type: "event", event: event({
      type: "turn_steered",
      session_id: liveSteerAliasSid,
      msg_id: "browser-live-steer",
      turn_id: "native-live-task",
      prompt: "guide from Remote",
    }),
  });
  liveSteerAliasState = reduce(liveSteerAliasState, {
    type: "event", event: event({
      type: "user_msg",
      session_id: liveSteerAliasSid,
      msg_id: "official-live-steer",
      client_msg_id: "browser-live-steer",
      prompt: "guide from Remote",
    }),
  });
  liveSteerAliasState = reduce(liveSteerAliasState, {
    type: "event", event: event({
      type: "turn_binding",
      session_id: liveSteerAliasSid,
      msg_id: "browser-live-steer",
      turn_id: "native-live-task",
    }),
  });
  assert.deepEqual(
    liveSteerAliasState.runtimes[liveSteerAliasSid].turns.map(
      (turn: Turn) => ({
        id: turn.id,
        clientMsgId: turn.clientMsgId,
        historyTurnId: turn.historyTurnId,
        forkPointId: turn.forkPointId,
        liveTaskId: turn.liveTaskId,
      })),
    [{
      id: "browser-live-steer",
      clientMsgId: "browser-live-steer",
      historyTurnId: "official-live-steer",
      forkPointId: "native-live-task",
      liveTaskId: "native-live-task",
    }],
    "an app-server userMessage echo aliases the published Remote steer once",
  );

  const claudeSwitchAliasSid = "claude-switch-history-alias";
  const claudeOtherSid = "codex-viewed-between-claude-history";
  let claudeSwitchAliasState = reduce({
    ...initialState,
    focusedSid: claudeSwitchAliasSid,
    runtimes: {
      [claudeSwitchAliasSid]: {
        ...createRuntime(), state: "running" as const, syncReady: true,
      },
      [claudeOtherSid]: createRuntime(),
    },
  }, {
    type: "query_sent",
    sid: claudeSwitchAliasSid,
    prompt: "我又改完了一版，你看看还有没得问题？",
    msg_id: "6b09ee37-f861-4422-b98a-21f509c951b0",
    ts: 22_100,
  });
  claudeSwitchAliasState = reduce(claudeSwitchAliasState, {
    type: "event", event: event({
      type: "session_focus", session_id: claudeOtherSid,
    }),
  });
  claudeSwitchAliasState = reduce(claudeSwitchAliasState, {
    type: "event", event: event({
      type: "session_focus", session_id: claudeSwitchAliasSid,
    }),
  });
  claudeSwitchAliasState = reduce(claudeSwitchAliasState, {
    type: "event", event: event({
      type: "history",
      session_id: claudeSwitchAliasSid,
      revision: "claude-switch-r1",
      generation: "claude-switch-g1",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      has_more: false,
      in_progress: true,
      events: [],
      turns: [{
        id: "2259073b-7676-455f-b7b0-b9b3892dbe93",
        clientMsgId: "6b09ee37-f861-4422-b98a-21f509c951b0",
        prompt: "我又改完了一版，你看看还有没得问题？",
        done: false,
        blocks: [],
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.deepEqual(
    claudeSwitchAliasState.runtimes[claudeSwitchAliasSid].turns.map(
      (turn: Turn) => ({
        id: turn.id,
        historyTurnId: turn.historyTurnId,
        prompt: turn.prompt,
      })),
    [{
      id: "6b09ee37-f861-4422-b98a-21f509c951b0",
      historyTurnId: "2259073b-7676-455f-b7b0-b9b3892dbe93",
      prompt: "我又改完了一版，你看看还有没得问题？",
    }],
    "switching to Codex and back reconciles Claude History with the one live prompt",
  );

  // Claude's live TurnBinding names the replayed native user/checkpoint UUID,
  // but ResultMessage closes the turn with the final assistant UUID.  They are
  // deliberately different identities: route the terminal through checkpoint_id
  // so an idle sidebar cannot leave the answer row spinning forever.
  const claudeTerminalAliasSid = "claude-terminal-checkpoint-alias";
  const claudeClientMessage = "11111111-1111-4111-8111-111111111111";
  const claudeUserCheckpoint = "22222222-2222-4222-8222-222222222222";
  const claudeAssistantTerminal = "33333333-3333-4333-8333-333333333333";
  let claudeTerminalAliasState = reduce({
    ...initialState,
    focusedSid: claudeTerminalAliasSid,
    runtimes: {
      [claudeTerminalAliasSid]: {
        ...createRuntime(), state: "running" as const, syncReady: true,
      },
    },
  }, {
    type: "query_sent",
    sid: claudeTerminalAliasSid,
    prompt: "完成这项工作",
    msg_id: claudeClientMessage,
    ts: 22_200,
  });
  for (const liveEvent of [
    event({
      type: "turn_binding",
      sid: claudeTerminalAliasSid,
      seq: 1,
      msg_id: claudeClientMessage,
      turn_id: claudeUserCheckpoint,
    }),
    event({
      type: "assistant_msg_start",
      sid: claudeTerminalAliasSid,
      seq: 2,
      message_id: claudeAssistantTerminal,
      channel: "final",
    }),
    event({
      type: "delta",
      sid: claudeTerminalAliasSid,
      seq: 3,
      message_id: claudeAssistantTerminal,
      channel: "final",
      text: "已经完成。",
    }),
    event({
      type: "turn_end",
      sid: claudeTerminalAliasSid,
      seq: 4,
      turn_id: claudeAssistantTerminal,
      checkpoint_id: claudeUserCheckpoint,
      result: { subtype: "success", duration_ms: 5, is_error: false },
    }),
  ]) {
    claudeTerminalAliasState = reduce(claudeTerminalAliasState, {
      type: "event", event: liveEvent,
    });
  }
  const settledClaudeAlias = claudeTerminalAliasState.runtimes[
    claudeTerminalAliasSid
  ];
  assert.equal(settledClaudeAlias.turns.length, 1);
  assert.equal(settledClaudeAlias.turns[0].done, true,
    "Claude's assistant terminal closes the checkpoint-bound client row");
  assert.ok(
    settledClaudeAlias.turns[0].blocks.every(
      (block: Block) => block.done),
    "the terminal also settles the answer block which drove the tail spinner",
  );
  assert.equal(
    settledClaudeAlias.turns[0].forkPointId,
    claudeAssistantTerminal,
    "the final assistant UUID remains the authoritative Claude fork point",
  );
  assert.equal(
    settledClaudeAlias.turns[0].checkpointId,
    claudeUserCheckpoint,
    "the native user UUID remains available for Claude file rewind",
  );
  assert.equal(settledClaudeAlias.pendingLiveBinding, null,
    "the checkpoint-matched terminal consumes its pending live binding");
  assert.equal(settledClaudeAlias.state, "running",
    "TurnEnd repairs presentation without unlocking before State(idle)");

  const legacyClaudeNativeId = "b1934482-d098-4e11-990d-3e91f2586358";
  const legacyClaudeClientId = "d37608f7-713d-42e2-b10f-4a93f60b03e1";
  const legacyClaudePrompt =
    "int32累加器和fp32累加器有什么区别？和寄存器的关系是什么？";
  let legacyClaudeCacheState = reduce({
    ...initialState,
    focusedSid: claudeSwitchAliasSid,
    runtimes: {
      [claudeSwitchAliasSid]: createRuntime(),
      [claudeOtherSid]: createRuntime(),
    },
  }, {
    type: "hydrate_cache",
    sid: claudeSwitchAliasSid,
    revision: "claude-legacy-r1",
    generation: "claude-legacy-g1",
    // v10 could persist the transcript UUID before promptId became an exact
    // client alias. CACHE_VER=12 prevents this row from reaching the reducer;
    // keep the downstream convergence check as a second safety boundary.
    turns: [{
      id: legacyClaudeNativeId,
      prompt: legacyClaudePrompt,
      blocks: [],
      done: false,
    }],
  });
  legacyClaudeCacheState = reduce(legacyClaudeCacheState, {
    type: "event", event: event({
      type: "session_focus", session_id: claudeOtherSid,
    }),
  });
  legacyClaudeCacheState = reduce(legacyClaudeCacheState, {
    type: "event", event: event({
      type: "session_focus", session_id: claudeSwitchAliasSid,
    }),
  });
  legacyClaudeCacheState = reduce(legacyClaudeCacheState, {
    type: "event", event: event({
      type: "user_msg",
      sid: claudeSwitchAliasSid,
      msg_id: legacyClaudeClientId,
      prompt: legacyClaudePrompt,
    }),
  });
  assert.equal(
    legacyClaudeCacheState.runtimes[claudeSwitchAliasSid].turns.length,
    2,
    "the v10 shape explains the transient duplicate before canonical History",
  );
  legacyClaudeCacheState = reduce(legacyClaudeCacheState, {
    type: "event", event: event({
      type: "history",
      session_id: claudeSwitchAliasSid,
      revision: "claude-legacy-r1",
      generation: "claude-legacy-g1",
      build_seq: 2,
      live_seq: 1,
      detail: "summary",
      authoritative: true,
      has_more: false,
      in_progress: true,
      events: [],
      turns: [{
        id: legacyClaudeNativeId,
        clientMsgId: legacyClaudeClientId,
        prompt: legacyClaudePrompt,
        done: false,
        blocks: [],
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.deepEqual(
    legacyClaudeCacheState.runtimes[claudeSwitchAliasSid].turns.map(
      (turn: Turn) => [turn.id, turn.clientMsgId, turn.historyTurnId]),
    [[legacyClaudeClientId, legacyClaudeClientId, legacyClaudeNativeId]],
    "authoritative Claude aliases collapse a legacy projection if one survives",
  );
  const repeatedClaudePrompt = mergeInitialHistory(
    [{
      id: "claude-native-first", clientMsgId: "claude-client-first",
      prompt: "继续", blocks: [], done: true,
    }],
    [{
      id: "claude-client-second", clientMsgId: "claude-client-second",
      prompt: "继续", blocks: [], done: false,
    }],
  );
  assert.equal(repeatedClaudePrompt.length, 2,
    "two real Claude prompts with equal text and distinct ids must both remain");
  const repeatedClaudeSid = "claude-repeated-prompt-history";
  let repeatedClaudeState = reduce({
    ...initialState,
    focusedSid: repeatedClaudeSid,
    runtimes: {
      [repeatedClaudeSid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
        turns: [{
          id: "claude-client-first",
          clientMsgId: "claude-client-first",
          prompt: "继续",
          blocks: [],
          done: true,
        }],
      },
    },
  }, {
    type: "query_sent",
    sid: repeatedClaudeSid,
    prompt: "继续",
    msg_id: "claude-client-second",
    ts: 23_000,
  });
  repeatedClaudeState = reduce(repeatedClaudeState, {
    type: "event",
    event: event({
      type: "history",
      session_id: repeatedClaudeSid,
      revision: "claude-repeated-r1",
      generation: "claude-repeated-g1",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      authoritative: true,
      has_more: false,
      in_progress: true,
      events: [],
      turns: [
        {
          id: "claude-native-first",
          clientMsgId: "claude-client-first",
          prompt: "继续",
          blocks: [],
          done: true,
          detailEventCount: 0,
          detailLoaded: false,
        },
        {
          id: "claude-native-second",
          clientMsgId: "claude-client-second",
          prompt: "继续",
          blocks: [],
          done: false,
          detailEventCount: 0,
          detailLoaded: false,
        },
      ],
    }),
  });
  assert.deepEqual(
    repeatedClaudeState.runtimes[repeatedClaudeSid].turns.map(
      (turn: Turn) => [turn.clientMsgId, turn.prompt]),
    [
      ["claude-client-first", "继续"],
      ["claude-client-second", "继续"],
    ],
    "switch-back History reconciles equal Claude prompts by exact client id without collapsing either turn",
  );

  const runningAliasSid = "history-running-steer-alias";
  const runningAliasNativeTurn = "history-running-native-task";
  let runningAliasState = reduce({
    ...initialState,
    focusedSid: runningAliasSid,
    runtimes: {
      [runningAliasSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "history-running-g1",
        historyGeneration: "history-running-g1",
        historyRevision: "history-running-r1",
        historyBuildSeq: 1,
        historyLiveSeq: 1,
        historyNewestId: "history-running-original",
        lastLiveSeq: 1,
        lastLifecycleSeq: 1,
        liveOwner: { turnId: "history-running-original", seq: 1 },
        pendingLiveBinding: {
          msgId: "history-running-original",
          turnId: runningAliasNativeTurn,
          seq: 1,
          generation: "history-running-g1",
        },
        turns: [{
          id: "history-running-original",
          forkPointId: runningAliasNativeTurn,
          liveTaskId: runningAliasNativeTurn,
          prompt: "start",
          blocks: [],
          done: false,
          ts: 10_000,
        }],
      },
    },
  }, {
    type: "steer_sent",
    sid: runningAliasSid,
    prompt: "history confirms this steer",
    msg_id: "history-running-client-steer",
    ts: 20_000,
  });
  runningAliasState = reduce(runningAliasState, {
    type: "event", event: event({
      type: "history",
      session_id: runningAliasSid,
      revision: "history-running-r1",
      generation: "history-running-g1",
      build_seq: 2,
      live_seq: 2,
      detail: "summary",
      has_more: true,
      in_progress: true,
      newest_id: "history-running-native-steer",
      events: [],
      turns: [{
        id: "history-running-native-steer",
        clientMsgId: "history-running-client-steer",
        prompt: "history confirms this steer",
        done: false,
        blocks: [],
        ts: 20_000,
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.deepEqual(
    runningAliasState.runtimes[runningAliasSid].turns.map((turn: Turn) => ({
      id: turn.id,
      done: turn.done,
      liveTaskId: turn.liveTaskId,
    })),
    [{
      id: "history-running-original",
      done: true,
      liveTaskId: undefined,
    }, {
      id: "history-running-client-steer",
      done: false,
      liveTaskId: runningAliasNativeTurn,
    }],
    "a running History alias moves native ownership across the steer fence",
  );
  assert.deepEqual(
    runningAliasState.runtimes[runningAliasSid].liveOwner,
    { turnId: "history-running-client-steer", seq: 2 },
    "History-only steer acceptance moves the live display owner to the new segment",
  );
  assert.equal(
    runningAliasState.runtimes[runningAliasSid].pendingLiveBinding,
    null,
    "the same History watermark retires the predecessor binding it superseded",
  );
  runningAliasState = reduce(runningAliasState, {
    type: "event", event: event({
      type: "process",
      sid: runningAliasSid,
      item_id: "history-running-process",
      turn_id: runningAliasNativeTurn,
      kind: "command",
      phase: "start",
      status: "running",
      title: "new segment work",
    }),
  });
  assert.equal(
    runningAliasState.runtimes[runningAliasSid].turns[1].blocks[0]?.kind,
    "process",
    "native process events follow the History-confirmed steer segment",
  );
  runningAliasState = reduce(runningAliasState, {
    type: "event", event: event({
      type: "turn_end",
      sid: runningAliasSid,
      turn_id: runningAliasNativeTurn,
      result: { subtype: "success", duration_ms: 3, is_error: false },
    }),
  });
  assert.equal(
    runningAliasState.runtimes[runningAliasSid].turns[1].done,
    true,
    "the native terminal closes the History-confirmed steer segment",
  );

  const exactSteerSid = "history-running-exact-steer";
  const exactSteerNativeTurn = "history-running-exact-native-task";
  let exactSteerState = reduce({
    ...initialState,
    focusedSid: exactSteerSid,
    runtimes: {
      [exactSteerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "history-running-exact-original",
          forkPointId: exactSteerNativeTurn,
          liveTaskId: exactSteerNativeTurn,
          prompt: "start",
          blocks: [],
          done: false,
          ts: 10_000,
        }],
      },
    },
  }, {
    type: "steer_sent",
    sid: exactSteerSid,
    prompt: "history confirms the exact steer id",
    msg_id: "history-running-exact-client-steer",
    ts: 20_000,
  });
  exactSteerState = reduce(exactSteerState, {
    type: "event", event: event({
      type: "history",
      session_id: exactSteerSid,
      revision: "history-running-exact-r1",
      generation: "history-running-exact-g1",
      build_seq: 1,
      live_seq: 2,
      detail: "summary",
      has_more: true,
      in_progress: true,
      events: [],
      turns: [{
        id: "history-running-exact-client-steer",
        prompt: "history confirms the exact steer id",
        done: false,
        blocks: [],
        ts: 20_000,
        detailEventCount: 0,
        detailLoaded: false,
      }],
    }),
  });
  assert.deepEqual(
    exactSteerState.runtimes[exactSteerSid].turns.map((turn: Turn) => ({
      id: turn.id,
      done: turn.done,
      liveTaskId: turn.liveTaskId,
    })),
    [{
      id: "history-running-exact-original",
      done: true,
      liveTaskId: undefined,
    }, {
      id: "history-running-exact-client-steer",
      done: false,
      liveTaskId: exactSteerNativeTurn,
    }],
    "an exact History steer id moves ownership without clientMsgId metadata",
  );
  exactSteerState = reduce(exactSteerState, {
    type: "event", event: event({
      type: "process",
      sid: exactSteerSid,
      item_id: "history-running-exact-process",
      turn_id: exactSteerNativeTurn,
      kind: "command",
      phase: "start",
      status: "running",
      title: "new exact segment work",
    }),
  });
  assert.equal(
    exactSteerState.runtimes[exactSteerSid].turns[1].blocks[0]?.kind,
    "process",
    "native process follows an exact-id History-confirmed steer",
  );

  const unknownSteerSid = "unknown-steer-terminal-owner";
  const unknownCurrentNative = "unknown-current-native";
  let unknownSteerState = reduce({
    ...initialState,
    focusedSid: unknownSteerSid,
    runtimes: {
      [unknownSteerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "unknown-old",
          forkPointId: "unknown-old-native",
          prompt: "old",
          blocks: [],
          done: true,
        }, {
          id: "unknown-current",
          forkPointId: unknownCurrentNative,
          liveTaskId: unknownCurrentNative,
          prompt: "current",
          blocks: [],
          done: false,
        }],
      },
    },
  }, {
    type: "steer_sent",
    sid: unknownSteerSid,
    prompt: "uncertain steer",
    msg_id: "unknown-client-steer",
    ts: 30_000,
  });
  unknownSteerState = reduce(unknownSteerState, {
    type: "event", event: event({
      type: "error",
      sid: unknownSteerSid,
      msg_id: "unknown-client-steer",
      code: "steer_outcome_unknown",
      message: "connection closed before acceptance was observed",
    }),
  });
  const secondUnknownDispatch = reduce(unknownSteerState, {
    type: "steer_sent",
    sid: unknownSteerSid,
    prompt: "must not be sent",
    msg_id: "unknown-second-steer",
    ts: 31_000,
  });
  assert.equal(
    secondUnknownDispatch.runtimes[unknownSteerSid].turns.some(
      (turn: Turn) => turn.id === "unknown-second-steer"),
    false,
    "the reducer keeps one steer acceptance lane while its outcome is unknown",
  );
  unknownSteerState = reduce(unknownSteerState, {
    type: "event", event: event({
      type: "turn_end",
      sid: unknownSteerSid,
      turn_id: "unknown-old-native",
      result: { subtype: "success", duration_ms: 1, is_error: false },
    }),
  });
  assert.equal(
    unknownSteerState.runtimes[unknownSteerSid].acceptancePending,
    "unknown-client-steer",
    "a stale older TurnEnd cannot resolve the current unknown steer",
  );
  assert.equal(
    unknownSteerState.runtimes[unknownSteerSid].turns[1].done,
    false,
  );
  unknownSteerState = reduce(unknownSteerState, {
    type: "event", event: event({
      type: "turn_end",
      sid: unknownSteerSid,
      turn_id: unknownCurrentNative,
      result: { subtype: "success", duration_ms: 2, is_error: false },
    }),
  });
  assert.equal(
    unknownSteerState.runtimes[unknownSteerSid].acceptancePending,
    null,
    "the active native owner's terminal resolves the unknown steer",
  );
  assert.equal(
    unknownSteerState.runtimes[unknownSteerSid].turns.at(-1)?.interrupted,
    true,
  );

  const prunedStaleSid = "unknown-steer-pruned-stale-terminal";
  const prunedCurrentNative = "unknown-pruned-current-native";
  let prunedStaleState = reduce({
    ...initialState,
    focusedSid: prunedStaleSid,
    runtimes: {
      [prunedStaleSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "unknown-pruned-current",
          prompt: "current after old rows were pruned",
          blocks: [{
            kind: "process",
            item_id: "unknown-pruned-process",
            processKind: "command",
            phase: "start",
            status: "running",
            turn_id: prunedCurrentNative,
            title: "current work",
            done: false,
          }],
          done: false,
        }],
      },
    },
  }, {
    type: "steer_sent",
    sid: prunedStaleSid,
    prompt: "uncertain steer after pruning",
    msg_id: "unknown-pruned-client-steer",
    ts: 35_000,
  });
  prunedStaleState = reduce(prunedStaleState, {
    type: "event", event: event({
      type: "error",
      sid: prunedStaleSid,
      msg_id: "unknown-pruned-client-steer",
      code: "steer_outcome_unknown",
      message: "connection closed before acceptance was observed",
    }),
  });
  prunedStaleState = reduce(prunedStaleState, {
    type: "event", event: event({
      type: "turn_end",
      sid: prunedStaleSid,
      turn_id: "unknown-pruned-unrelated-old-native",
      result: { subtype: "success", duration_ms: 1, is_error: false },
    }),
  });
  assert.equal(
    prunedStaleState.runtimes[prunedStaleSid].acceptancePending,
    "unknown-pruned-client-steer",
    "a pruned stale TurnEnd cannot consume the sole explicitly-bound owner",
  );
  assert.equal(
    prunedStaleState.runtimes[prunedStaleSid].turns[0].done,
    false,
  );
  prunedStaleState = reduce(prunedStaleState, {
    type: "event", event: event({
      type: "state",
      sid: prunedStaleSid,
      state: "idle",
      ts: 36,
    }),
  });
  const prunedIdleTurns =
    prunedStaleState.runtimes[prunedStaleSid].turns;
  assert.equal(
    prunedStaleState.runtimes[prunedStaleSid].acceptancePending,
    null,
    "authoritative idle resolves an unknown steer without a TurnEnd",
  );
  assert.deepEqual(
    prunedIdleTurns.map((turn: Turn) => ({
      done: turn.done,
      interrupted: turn.interrupted,
    })),
    [
      { done: true, interrupted: true },
      { done: true, interrupted: true },
    ],
    "authoritative idle closes both the predecessor and unknown steer segment",
  );
  assert.equal(
    prunedIdleTurns[0].blocks[0]?.done,
    true,
    "authoritative idle cannot leave the predecessor process spinning",
  );

  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "process",
      sid: steeredTurnSid,
      item_id: "steered-native-process",
      kind: "command",
      phase: "start",
      status: "running",
      turn_id: steeredNativeTurnId,
      title: "native work after steer",
    }),
  });
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "assistant_msg_start",
      sid: steeredTurnSid,
      message_id: "steered-final",
      channel: "final",
    }),
  });
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "delta",
      sid: steeredTurnSid,
      message_id: "steered-final",
      channel: "final",
      text: "finished after steering",
    }),
  });
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "assistant_msg_end",
      sid: steeredTurnSid,
      message_id: "steered-final",
      channel: "final",
    }),
  });
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[0].blocks.some(
      (block: Block) => block.kind === "process"
        && block.item_id === "steered-native-process"),
    false,
    "native task events after a steer must not route back to the old segment",
  );
  const latestSteered = steeredTurnState.runtimes[steeredTurnSid].turns[1];
  assert.ok(latestSteered.blocks.some(
    (block: Block) => block.kind === "process"
      && block.item_id === "steered-native-process"));
  assert.ok(latestSteered.blocks.some(
    (block: Block) => block.kind === "text"
      && block.channel === "final"
      && block.text === "finished after steering"),
  "the final answer after a steer belongs to the latest visible segment");

  const beforeDuplicateSteer = JSON.stringify(
    steeredTurnState.runtimes[steeredTurnSid].turns);
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "turn_steered",
      sid: steeredTurnSid,
      msg_id: "steered-follow-up",
      turn_id: steeredNativeTurnId,
      prompt: "change direction",
      images: [{ media_type: "image/png", data: "steered-image" }],
      files: [{ filename: "steered.txt" }],
      ts: 11,
    }),
  });
  assert.equal(
    JSON.stringify(steeredTurnState.runtimes[steeredTurnSid].turns),
    beforeDuplicateSteer,
    "a reliable duplicate TurnSteered cannot close or duplicate a segment",
  );
  steeredTurnState = reduce(steeredTurnState, {
    type: "event", event: event({
      type: "turn_end",
      sid: steeredTurnSid,
      turn_id: steeredNativeTurnId,
      result: { subtype: "success", duration_ms: 42, is_error: false },
      ts: 12,
    }),
  });
  assert.equal(
    steeredTurnState.runtimes[steeredTurnSid].turns[0].forkPointId,
    undefined,
  );
  assert.deepEqual(
    {
      done: steeredTurnState.runtimes[steeredTurnSid].turns[1].done,
      forkPointId:
        steeredTurnState.runtimes[steeredTurnSid].turns[1].forkPointId,
      liveTaskId:
        steeredTurnState.runtimes[steeredTurnSid].turns[1].liveTaskId,
    },
    { done: true, forkPointId: steeredNativeTurnId, liveTaskId: undefined },
    "the native TurnEnd closes and binds only the latest steered segment",
  );

  const concurrentSteerSid = "codex-concurrent-steer";
  const concurrentTurnId = "native-concurrent-turn";
  let concurrentSteerState = {
    ...initialState,
    focusedSid: concurrentSteerSid,
    runtimes: {
      [concurrentSteerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "concurrent-original",
          forkPointId: concurrentTurnId,
          liveTaskId: concurrentTurnId,
          prompt: "original",
          blocks: [],
          done: false,
        }],
      },
    },
  };
  concurrentSteerState = reduce(concurrentSteerState, {
    type: "steer_sent",
    sid: concurrentSteerSid,
    prompt: "mine",
    msg_id: "mine-steer",
    ts: 40_000,
  });
  concurrentSteerState = reduce(concurrentSteerState, {
    type: "event", event: event({
      type: "turn_steered",
      sid: concurrentSteerSid,
      msg_id: "external-steer",
      turn_id: concurrentTurnId,
      prompt: "external",
      ts: 41,
    }),
  });
  concurrentSteerState = reduce(concurrentSteerState, {
    type: "event", event: event({
      type: "turn_steered",
      sid: concurrentSteerSid,
      msg_id: "mine-steer",
      turn_id: concurrentTurnId,
      prompt: "mine",
      ts: 42,
    }),
  });
  assert.deepEqual(
    concurrentSteerState.runtimes[concurrentSteerSid].turns.map(
      (turn: Turn) => ({
        id: turn.id,
        done: turn.done,
        liveTaskId: turn.liveTaskId,
      })),
    [{
      id: "concurrent-original", done: true, liveTaskId: undefined,
    }, {
      id: "external-steer", done: true, liveTaskId: undefined,
    }, {
      id: "mine-steer", done: false, liveTaskId: concurrentTurnId,
    }],
    "an external steer accepted first must stay before the later local optimistic steer",
  );

  const rejectedSteerSid = "codex-steer-rejected";
  const rejectedSteerBefore: Turn = {
    id: "still-running-after-rejection",
    prompt: "keep working",
    blocks: [{
      kind: "text",
      message_id: "still-running-message",
      channel: "commentary",
      text: "before rejection",
      done: false,
    }],
    done: false,
  };
  let rejectedSteerState = {
    ...initialState,
    focusedSid: rejectedSteerSid,
    runtimes: {
      [rejectedSteerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [rejectedSteerBefore],
      },
    },
  };
  rejectedSteerState = reduce(rejectedSteerState, {
    type: "steer_sent",
    sid: rejectedSteerSid,
    prompt: "rejected steer",
    msg_id: "rejected-steer-message",
    ts: 30_000,
  });
  rejectedSteerState = reduce(rejectedSteerState, {
    type: "event", event: event({
      type: "error",
      sid: rejectedSteerSid,
      msg_id: "rejected-steer-message",
      code: "not_steerable",
      message: "active turn cannot be steered",
    }),
  });
  assert.deepEqual(
    rejectedSteerState.runtimes[rejectedSteerSid].turns,
    [rejectedSteerBefore],
    "a rejected steer is a command problem and cannot close the active turn",
  );
  assert.equal(
    rejectedSteerState.runtimes[rejectedSteerSid].acceptancePending,
    null,
    "a definitive steer rejection releases the optimistic acceptance latch",
  );
  assert.equal(
    rejectedSteerState.runtimes[rejectedSteerSid].state,
    "running",
  );
  assert.ok(rejectedSteerState.banner);
  rejectedSteerState = reduce(rejectedSteerState, {
    type: "event", event: event({
      type: "delta",
      sid: rejectedSteerSid,
      message_id: "still-running-message",
      channel: "commentary",
      text: " and after rejection",
    }),
  });
  assert.equal(
    rejectedSteerState.runtimes[rejectedSteerSid].turns.length,
    1,
  );
  assert.equal(
    rejectedSteerState.runtimes[rejectedSteerSid].turns[0].blocks[0].kind
      === "text"
      ? rejectedSteerState.runtimes[rejectedSteerSid].turns[0].blocks[0].text
      : "",
    "before rejection and after rejection",
    "subsequent native deltas continue on the still-running segment",
  );

  const duplicateSid = "duplicate-first-message";
  let duplicateState = reduce({
    ...initialState, focusedSid: duplicateSid,
    runtimes: { [duplicateSid]: createRuntime() },
  }, {
    type: "query_sent", sid: duplicateSid, prompt: "在？测试测试",
    msg_id: "browser-message", ts: 1_000,
  });
  duplicateState = reduce(duplicateState, { type: "event", event: event({
    type: "state", sid: duplicateSid, state: "running",
  }) });
  duplicateState = reduce(duplicateState, { type: "event", event: event({
    type: "user_msg", sid: duplicateSid, msg_id: "browser-message",
    prompt: "在？测试测试", ts: 1,
  }) });
  duplicateState = reduce(duplicateState, { type: "event", event: event({
    type: "history", session_id: duplicateSid, revision: "r1",
    generation: "g1", build_seq: 1, live_seq: 0, has_more: false,
    in_progress: true, events: [
      event({ type: "user_msg", sid: duplicateSid, msg_id: "native-user",
        prompt: "在？测试测试", ts: 10 }),
      event({ type: "assistant_msg_start", sid: duplicateSid,
        message_id: "native-answer", channel: "final", ts: 11 }),
      event({ type: "delta", sid: duplicateSid, message_id: "native-answer",
        channel: "final", text: "收到", ts: 11 }),
      event({ type: "assistant_msg_end", sid: duplicateSid,
        message_id: "native-answer", channel: "final", ts: 11 }),
      event({ type: "turn_end", sid: duplicateSid, turn_id: "native-turn",
        result: { subtype: "success", duration_ms: 1, is_error: false }, ts: 11 }),
    ],
  }) });
  assert.equal(duplicateState.runtimes[duplicateSid].turns.length, 2,
    "timestamps outside the narrow legacy heuristic intentionally remain separate");
  duplicateState = reduce(duplicateState, { type: "event", event: event({
    type: "turn_binding", sid: duplicateSid,
    msg_id: "browser-message", turn_id: "native-turn",
  }) });
  assert.equal(duplicateState.runtimes[duplicateSid].turns.length, 1,
    "authoritative turn binding collapses history/live regardless of arrival order");
  assert.equal(duplicateState.runtimes[duplicateSid].turns[0].id, "browser-message");
  assert.equal(duplicateState.runtimes[duplicateSid].turns[0].forkPointId,
    "native-turn");
  assert.equal(duplicateState.runtimes[duplicateSid].turns[0].done, true);

  const goalPromptSid = "goal-prompt-race";
  let goalPromptState = reduce({
    ...initialState, focusedSid: goalPromptSid,
    runtimes: { [goalPromptSid]: createRuntime() },
  }, { type: "event", event: event({
    type: "user_msg", sid: goalPromptSid, msg_id: "native-goal-turn",
    prompt: "", ts: 20,
  }) });
  goalPromptState = reduce(goalPromptState, {
    type: "event", event: event({
      type: "user_msg", sid: goalPromptSid, msg_id: "native-goal-turn",
      prompt: "证明泰勒展开", ts: 20,
    }),
  });
  goalPromptState = reduce(goalPromptState, {
    type: "event", event: event({
      type: "user_msg", sid: goalPromptSid, msg_id: "native-goal-turn",
      prompt: "证明泰勒展开", ts: 20,
    }),
  });
  assert.equal(goalPromptState.runtimes[goalPromptSid].turns.length, 1,
    "late and retried Goal objective frames reuse the native empty anchor");
  assert.equal(
    goalPromptState.runtimes[goalPromptSid].turns[0].prompt,
    "证明泰勒展开",
  );

  const summarySid = "materialized-summary";
  const summaryState = reduce({
    ...initialState, focusedSid: summarySid,
    runtimes: { [summarySid]: createRuntime() },
  }, { type: "event", event: event({
    type: "history", session_id: summarySid, revision: "summary-r1",
    generation: "summary-g1", build_seq: 1, live_seq: 0,
    detail: "summary", has_more: true, oldest_id: "summary-message",
    events: [event({ type: "model", sid: summarySid, model: "gpt-summary" })],
    turns: [{
      id: "summary-message", prompt: "inspect", done: true,
      blocks: [{ kind: "text", message_id: "summary-final",
        text: "finished", done: true, channel: "final" }],
      detailEventCount: 74, detailLoaded: false,
    }],
  }) });
  assert.equal(summaryState.runtimes[summarySid].turns.length, 1);
  assert.equal(summaryState.runtimes[summarySid].ccSessionId, summarySid,
    "authoritative history must become cacheable before a slow resume snapshot");
  assert.equal(summaryState.runtimes[summarySid].turns[0].blocks[0].kind, "text");
  assert.equal(summaryState.runtimes[summarySid].turns[0].detailEventCount, 74);
  assert.equal(summaryState.runtimes[summarySid].model, "gpt-summary");
  assert.equal(summaryState.runtimes[summarySid].hasMore, true);

  let detailState = reduce(summaryState, {
    type: "turn_detail_requested", sid: summarySid,
    turnId: "summary-message",
  });
  assert.equal(detailState.runtimes[summarySid].turns[0].detailLoading, true);
  detailState = reduce(detailState, { type: "event", event: event({
    type: "turn_detail", session_id: summarySid, turn_id: "summary-message",
    revision: "summary-r1", authoritative: false,
    error: "详细过程暂时不可用，请稍后重试", events: [],
  }) });
  assert.equal(detailState.runtimes[summarySid].turns[0].detailLoading, false);
  assert.equal(
    detailState.runtimes[summarySid].turns[0].detailError,
    "详细过程暂时不可用，请稍后重试",
    "a failed detail read remains visible and retryable inside the open disclosure",
  );
  detailState = reduce(detailState, {
    type: "turn_detail_requested", sid: summarySid,
    turnId: "summary-message",
  });
  assert.equal(detailState.runtimes[summarySid].turns[0].detailLoading, true);
  assert.equal(detailState.runtimes[summarySid].turns[0].detailError, undefined,
    "retry clears the prior detail error without collapsing the process shell");
  detailState = reduce(detailState, { type: "event", event: event({
    type: "turn_detail", session_id: summarySid, turn_id: "summary-message",
    revision: "summary-r1", events: [
      event({ type: "user_msg", sid: summarySid, msg_id: "summary-message",
        prompt: "inspect" }),
      event({ type: "assistant_msg_start", sid: summarySid,
        message_id: "summary-commentary", channel: "commentary" }),
      event({ type: "delta", sid: summarySid,
        message_id: "summary-commentary", channel: "commentary",
        text: "checking" }),
      event({ type: "tool_use", sid: summarySid,
        message_id: "summary-tool-message", tool_use_id: "summary-tool",
        tool: "Read", input: { file_path: "/tmp/example" } }),
      event({ type: "tool_result", sid: summarySid,
        tool_use_id: "summary-tool", content: "ok", is_error: false }),
      event({ type: "assistant_msg_start", sid: summarySid,
        message_id: "summary-final", channel: "final" }),
      event({ type: "delta", sid: summarySid,
        message_id: "summary-final", channel: "final", text: "finished" }),
      event({ type: "assistant_msg_end", sid: summarySid,
        message_id: "summary-final", channel: "final" }),
      event({ type: "turn_end", sid: summarySid, turn_id: "summary-turn",
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.equal(detailState.runtimes[summarySid].turns.length, 1);
  assert.equal(detailState.runtimes[summarySid].turns[0].detailLoaded, true);
  assert.equal(detailState.runtimes[summarySid].turns[0].detailLoading, false);
  assert.equal(detailState.runtimes[summarySid].turns[0].detailError, undefined);
  assert.equal(
    detailState.runtimes[summarySid].turns[0].detailRetryDirection,
    undefined,
    "a successful detail response clears its transient retry target",
  );
  assert.ok(detailState.runtimes[summarySid].turns[0].detailProjection?.blocks.some(
    (block: Block) => block.kind === "tool"));
  assert.deepEqual(detailState.runtimes[summarySid].turns[0].blocks.map(
    (block: Block) => block.kind), ["text"],
  "heavy process blocks stay outside the bounded canonical summary");
  assert.equal(detailState.runtimes[summarySid].turns[0].blocks.filter(
    (block: Block) => block.kind === "text" && block.channel === "final").length, 1,
  "detail replaces the summary projection instead of duplicating its final text");

  // Every bounded detail page is independently decodable, so the wrapper
  // repeats the same user/terminal envelope around its disjoint display groups.
  // Combining pages must combine those groups, not feed an intermediate
  // TurnEnd back through the reducer and silently keep only one page.
  const pagedDetailSid = "multi-page-detail-envelope";
  let pagedDetailState = reduce({
    ...initialState,
    focusedSid: pagedDetailSid,
    runtimes: { [pagedDetailSid]: createRuntime() },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: pagedDetailSid,
      session_id: pagedDetailSid,
      revision: "paged-detail-r1",
      generation: "paged-detail-g1",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      has_more: false,
      newest_id: "paged-detail-turn",
      in_progress: false,
      events: [],
      turns: [{
        id: "paged-detail-turn",
        prompt: "run for eight hours",
        done: true,
        blocks: [{
          kind: "text",
          message_id: "paged-detail-final",
          text: "finished",
          done: true,
          channel: "final",
        }],
        detailEventCount: 12,
      }],
    }),
  });
  const detailEnvelope = () => [
    event({
      type: "user_msg",
      sid: pagedDetailSid,
      msg_id: "paged-detail-turn",
      prompt: "run for eight hours",
    }),
  ];
  const detailTerminal = () => [
    event({
      type: "assistant_msg_start",
      sid: pagedDetailSid,
      message_id: "paged-detail-final",
      channel: "final",
    }),
    event({
      type: "delta",
      sid: pagedDetailSid,
      message_id: "paged-detail-final",
      channel: "final",
      text: "finished",
    }),
    event({
      type: "assistant_msg_end",
      sid: pagedDetailSid,
      message_id: "paged-detail-final",
      channel: "final",
    }),
    event({
      type: "turn_end",
      sid: pagedDetailSid,
      turn_id: "paged-detail-native",
      result: { subtype: "success", duration_ms: 1, is_error: false },
    }),
  ];
  pagedDetailState = reduce(pagedDetailState, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid: pagedDetailSid,
      session_id: pagedDetailSid,
      turn_id: "paged-detail-turn",
      revision: "paged-detail-r1",
      before: null,
      has_more: true,
      oldest_cursor: "2",
      has_newer: false,
      newer_cursor: null,
      events: [
        ...detailEnvelope(),
        event({
          type: "assistant_msg_start",
          sid: pagedDetailSid,
          message_id: "newer-commentary",
          channel: "commentary",
        }),
        event({
          type: "delta",
          sid: pagedDetailSid,
          message_id: "newer-commentary",
          channel: "commentary",
          text: "newer work",
        }),
        event({
          type: "assistant_msg_end",
          sid: pagedDetailSid,
          message_id: "newer-commentary",
          channel: "commentary",
        }),
        event({
          type: "tool_use",
          sid: pagedDetailSid,
          message_id: "newer-tool-message",
          tool_use_id: "newer-tool",
          tool: "Read",
          input: { file_path: "/tmp/newer" },
        }),
        event({
          type: "tool_result",
          sid: pagedDetailSid,
          tool_use_id: "newer-tool",
          content: "newer result",
          is_error: false,
        }),
        event({
          type: "process",
          sid: pagedDetailSid,
          item_id: "newer-compaction",
          kind: "compaction",
          phase: "end",
          status: "succeeded",
          turn_id: "paged-detail-native",
          title: "压缩上下文",
        }),
        ...detailTerminal(),
      ],
    }),
  });
  pagedDetailState = reduce(pagedDetailState, {
    type: "event",
    event: event({
      type: "turn_detail",
      sid: pagedDetailSid,
      session_id: pagedDetailSid,
      turn_id: "paged-detail-turn",
      revision: "paged-detail-r1",
      before: "2",
      has_more: false,
      oldest_cursor: null,
      has_newer: true,
      newer_cursor: "4",
      events: [
        ...detailEnvelope(),
        event({
          type: "assistant_msg_start",
          sid: pagedDetailSid,
          message_id: "older-commentary",
          channel: "commentary",
        }),
        event({
          type: "delta",
          sid: pagedDetailSid,
          message_id: "older-commentary",
          channel: "commentary",
          text: "older work",
        }),
        event({
          type: "assistant_msg_end",
          sid: pagedDetailSid,
          message_id: "older-commentary",
          channel: "commentary",
        }),
        event({
          type: "tool_use",
          sid: pagedDetailSid,
          message_id: "older-tool-message",
          tool_use_id: "older-tool",
          tool: "Read",
          input: { file_path: "/tmp/older" },
        }),
        event({
          type: "tool_result",
          sid: pagedDetailSid,
          tool_use_id: "older-tool",
          content: "older result",
          is_error: false,
        }),
        event({
          type: "process",
          sid: pagedDetailSid,
          item_id: "older-compaction",
          kind: "compaction",
          phase: "end",
          status: "succeeded",
          turn_id: "paged-detail-native",
          title: "压缩上下文",
        }),
        ...detailTerminal(),
      ],
    }),
  });
  assert.deepEqual(
    pagedDetailState.runtimes[pagedDetailSid].turns[0]
      .detailProjection?.blocks.map((block: Block) => block.kind === "text"
        ? block.message_id
        : block.kind === "tool" ? block.tool_use_id : block.item_id),
    [
      "older-commentary",
      "older-tool",
      "older-compaction",
      "newer-commentary",
      "newer-tool",
      "newer-compaction",
    ],
    "disjoint detail pages survive repeated page envelopes in source order",
  );

  const staleDetail = reduce(detailState, { type: "event", event: event({
    type: "turn_detail", session_id: summarySid, turn_id: "summary-message",
    revision: "stale-revision", events: [],
  }) });
  assert.equal(staleDetail, detailState,
    "a detail response from another history revision cannot rewrite the turn");

  const runtimeOlderRequested = reduce(detailState, {
    type: "turn_detail_requested", sid: summarySid,
    turnId: "summary-message", before: "summary-older-cursor",
  });
  const runtimeOlderFailed = reduce(runtimeOlderRequested, {
    type: "event",
    event: event({
      type: "turn_detail", session_id: summarySid,
      turn_id: "summary-message", revision: "summary-r1",
      authoritative: false, before: "summary-older-cursor",
      error: "详细过程暂时不可用，请稍后重试", events: [],
    }),
  });
  assert.equal(
    runtimeOlderFailed.runtimes[summarySid].turns[0].detailLoaded,
    true,
    "an older-page failure keeps the already-loaded newest detail page",
  );
  assert.equal(
    runtimeOlderFailed.runtimes[summarySid].turns[0].detailRetryBefore,
    "summary-older-cursor",
  );
  assert.equal(
    runtimeOlderFailed.runtimes[summarySid].turns[0].detailRetryDirection,
    "older",
  );

  const refreshedSummary = reduce(detailState, { type: "event", event: event({
    type: "history", session_id: summarySid, revision: "summary-r1",
    generation: "summary-g1", build_seq: 2, live_seq: 0,
    detail: "summary", has_more: true, oldest_id: "summary-message",
    events: [], turns: [{
      id: "summary-message", prompt: "inspect", done: true,
      blocks: [{ kind: "text", message_id: "summary-final",
        text: "finished", done: true, channel: "final" }],
      detailEventCount: 74, detailLoaded: false,
    }],
  }) });
  assert.equal(refreshedSummary.runtimes[summarySid].turns[0].detailLoaded, true);
  assert.ok(refreshedSummary.runtimes[summarySid].turns[0]
    .detailProjection?.blocks.some((block: Block) => block.kind === "tool"),
  "a same-revision head refresh must not collapse detail the user opened");

  const refreshSid = "completed-process-refresh";
  const cachedSixStepTurn: Turn = {
    id: "refresh-turn",
    prompt: "1",
    done: true,
    blocks: [
      {
        kind: "text", message_id: "refresh-commentary", text: "2",
        done: true, channel: "commentary",
      },
      {
        kind: "tool", message_id: "refresh-tool-message-3",
        tool_use_id: "refresh-tool-3", tool: "Read",
        input: { file_path: "/tmp/3" }, done: true,
      },
      {
        kind: "tool", message_id: "refresh-tool-message-4",
        tool_use_id: "refresh-tool-4", tool: "Bash",
        input: { command: "echo 4" }, done: true,
      },
      {
        kind: "process", item_id: "refresh-process-5",
        processKind: "command", phase: "snapshot", status: "succeeded",
        title: "5", done: true,
      },
      {
        kind: "text", message_id: "refresh-final", text: "6",
        done: true, channel: "final",
      },
    ],
    detailEventCount: 4,
  };
  const refreshSummary = () => event({
    type: "history", session_id: refreshSid, revision: "refresh-r1",
    generation: "refresh-g1", build_seq: 1, live_seq: 0,
    detail: "summary", has_more: false, oldest_id: "refresh-turn",
    events: [], turns: [{
      id: "refresh-turn", prompt: "1", done: true,
      blocks: [{
        kind: "text", message_id: "refresh-final", text: "6",
        done: true, channel: "final",
      }],
      detailEventCount: 4, detailLoaded: false,
    }],
  });
  const processFingerprint = (turn: Turn): string[] =>
    (turn.detailProjection?.blocks ?? []).map((block) => {
      if (block.kind === "text") return block.text;
      if (block.kind === "tool") return block.tool_use_id;
      return block.title;
    });
  const twoCachedRestores = restoreCachedTurnDetails(
    [
      {
        ...cachedSixStepTurn,
        id: "refresh-turn-older",
        prompt: "older",
        blocks: [cachedSixStepTurn.blocks.at(-1)!],
      },
      {
        ...cachedSixStepTurn,
        blocks: [cachedSixStepTurn.blocks.at(-1)!],
      },
    ],
    [
      {
        ...cachedSixStepTurn,
        id: "refresh-turn-older",
        prompt: "older",
      },
      cachedSixStepTurn,
    ],
    "idle",
  );
  assert.deepEqual(
    twoCachedRestores.map((turn) => !!turn.detailRestorePending),
    [false, false],
    "completed cached process stays lazy until the user requests its detail",
  );
  const repeatedPromptSummaries: Turn[] = ["a", "b"].map((suffix, index) => ({
    id: `repeated-${suffix}`,
    prompt: "继续",
    ts: 1_000 + index * 1_000,
    done: true,
    blocks: [{
      kind: "text",
      message_id: `repeated-${suffix}-final`,
      text: `final-${suffix}`,
      done: true,
      channel: "final",
    }],
    detailEventCount: 1,
  }));
  const repeatedPromptCaches = repeatedPromptSummaries.map((turn, index) => ({
    ...turn,
    blocks: [{
      kind: "process" as const,
      item_id: `repeated-${index}-process`,
      processKind: "command" as const,
      phase: "snapshot" as const,
      status: "succeeded" as const,
      title: `process-${index}`,
      done: true,
    }, ...turn.blocks],
  }));
  const repeatedPromptRestores = restoreCachedTurnDetails(
    repeatedPromptSummaries, repeatedPromptCaches, "idle");
  assert.deepEqual(
    repeatedPromptRestores.map(processFingerprint),
    [["process-0"], ["process-1"]],
    "nearby repeated prompts must restore each turn's own cached process exactly once",
  );
  const aliasSummaries: Turn[] = ["a", "b"].map((suffix, index) => ({
    ...repeatedPromptSummaries[index],
    id: `client-${suffix}`,
    historyTurnId: `native-${suffix}`,
  }));
  const aliasCaches: Turn[] = ["a", "b"].map((suffix, index) => ({
    ...repeatedPromptCaches[index],
    id: `native-${suffix}`,
    // Deliberately make timestamp affinity prefer the wrong repeated prompt.
    ts: index === 0 ? aliasSummaries[1].ts : aliasSummaries[0].ts,
    blocks: [{
      kind: "process",
      item_id: `native-${suffix}-process`,
      processKind: "command",
      phase: "snapshot",
      status: "succeeded",
      title: `native-process-${suffix}`,
      done: true,
    }, ...repeatedPromptSummaries[index].blocks],
  }));
  const aliasRestores = restoreCachedTurnDetails(
    aliasSummaries, aliasCaches, "idle");
  assert.deepEqual(
    aliasRestores.map(processFingerprint),
    [["native-process-a"], ["native-process-b"]],
    "historyTurnId must beat repeated-prompt timestamp affinity during cache restore",
  );

  let cacheFirstRefresh = reduce({
    ...initialState, focusedSid: refreshSid,
    runtimes: { [refreshSid]: createRuntime() },
  }, {
    type: "hydrate_cache", sid: refreshSid, turns: [cachedSixStepTurn],
    revision: "refresh-r1", generation: "refresh-g1",
  });
  cacheFirstRefresh = reduce(cacheFirstRefresh, {
    type: "event", event: refreshSummary(),
  });
  assert.deepEqual(
    processFingerprint(cacheFirstRefresh.runtimes[refreshSid].turns[0]),
    ["2", "refresh-tool-3", "refresh-tool-4", "5"],
    "cache-first refresh must keep the completed process visible while authoritative detail reloads",
  );
  assert.equal(
    (cacheFirstRefresh.runtimes[refreshSid].turns[0] as Turn & {
      detailRestorePending?: boolean;
    }).detailRestorePending,
    false,
    "entering a completed session must not schedule an unsolicited detail read",
  );
  assert.equal(
    (cacheFirstRefresh.runtimes[refreshSid].turns[0] as Turn & {
      detailRestoreOpen?: boolean;
    }).detailRestoreOpen,
    false,
    "completed cached process stays collapsed until the user opens it",
  );

  let historyFirstRefresh = reduce({
    ...initialState, focusedSid: refreshSid,
    runtimes: { [refreshSid]: createRuntime() },
  }, {
    type: "event", event: refreshSummary(),
  });
  historyFirstRefresh = reduce(historyFirstRefresh, {
    type: "hydrate_cache", sid: refreshSid, turns: [cachedSixStepTurn],
    revision: "refresh-r1", generation: "refresh-g1",
  });
  assert.deepEqual(
    processFingerprint(historyFirstRefresh.runtimes[refreshSid].turns[0]),
    ["2", "refresh-tool-3", "refresh-tool-4", "5"],
    "History-first refresh must accept the same validated provisional process instead of racing it away",
  );

  let mismatchedRefresh = reduce({
    ...initialState, focusedSid: refreshSid,
    runtimes: { [refreshSid]: createRuntime() },
  }, {
    type: "event", event: refreshSummary(),
  });
  mismatchedRefresh = reduce(mismatchedRefresh, {
    type: "hydrate_cache", sid: refreshSid, turns: [cachedSixStepTurn],
    revision: "refresh-r1", generation: "stale-generation",
  });
  assert.deepEqual(
    processFingerprint(mismatchedRefresh.runtimes[refreshSid].turns[0]),
    [],
    "a process cache from another wrapper generation must never be restored",
  );
  const mismatchedRevision = reduce(reduce({
    ...initialState, focusedSid: refreshSid,
    runtimes: { [refreshSid]: createRuntime() },
  }, {
    type: "event", event: refreshSummary(),
  }), {
    type: "hydrate_cache", sid: refreshSid, turns: [cachedSixStepTurn],
    revision: "stale-revision", generation: "refresh-g1",
  });
  assert.deepEqual(
    processFingerprint(mismatchedRevision.runtimes[refreshSid].turns[0]),
    [],
    "a process cache from another history revision must never be restored",
  );

  const restoreRequested = reduce(cacheFirstRefresh, {
    type: "turn_detail_requested", sid: refreshSid, turnId: "refresh-turn",
    autoLoad: false,
  } as Parameters<typeof reduce>[1]);
  assert.equal(
    restoreRequested.runtimes[refreshSid].turns[0].detailAutoLoad,
    false,
    "automatic refresh repair fetches one newest detail page instead of paging a giant turn to EOF",
  );
  assert.equal(
    (restoreRequested.runtimes[refreshSid].turns[0] as Turn & {
      detailRestorePending?: boolean;
    }).detailRestorePending,
    false,
    "an accepted restore request is one-shot",
  );
  const restoredDetail = reduce(restoreRequested, {
    type: "event", event: event({
      type: "turn_detail", session_id: refreshSid,
      turn_id: "refresh-turn", revision: "refresh-r1",
      has_more: true, oldest_cursor: "restore-older",
      events: [
        event({ type: "user_msg", sid: refreshSid,
          msg_id: "refresh-turn", prompt: "1" }),
        event({ type: "assistant_msg_start", sid: refreshSid,
          message_id: "restore-commentary", channel: "commentary" }),
        event({ type: "delta", sid: refreshSid,
          message_id: "restore-commentary", channel: "commentary",
          text: "2-server" }),
        event({ type: "assistant_msg_end", sid: refreshSid,
          message_id: "restore-commentary", channel: "commentary" }),
        event({ type: "tool_use", sid: refreshSid,
          message_id: "restore-tool-message", tool_use_id: "restore-tool-5",
          tool: "Read", input: { file_path: "/tmp/5" } }),
        event({ type: "tool_result", sid: refreshSid,
          tool_use_id: "restore-tool-5", content: "ok", is_error: false }),
        event({ type: "assistant_msg_start", sid: refreshSid,
          message_id: "refresh-final", channel: "final" }),
        event({ type: "delta", sid: refreshSid,
          message_id: "refresh-final", channel: "final", text: "6" }),
        event({ type: "assistant_msg_end", sid: refreshSid,
          message_id: "refresh-final", channel: "final" }),
        event({ type: "turn_end", sid: refreshSid, turn_id: "refresh-turn",
          result: { subtype: "success", duration_ms: 1, is_error: false } }),
      ],
    }),
  });
  assert.deepEqual(
    processFingerprint(restoredDetail.runtimes[refreshSid].turns[0]),
    ["2-server", "restore-tool-5"],
    "the authoritative newest detail page replaces rather than merges provisional cache blocks",
  );
  assert.equal(
    restoredDetail.runtimes[refreshSid].turns[0].detailAutoLoad,
    false,
    "refresh repair must not follow has_more into an automatic full-turn download",
  );
  assert.equal(
    restoredDetail.runtimes[refreshSid].turns[0].detailLoaded,
    false,
    "a one-page repair with older detail remaining must keep the explicit full-process affordance",
  );
  const userExpandedRestore = reduce(restoredDetail, {
    type: "turn_detail_requested", sid: refreshSid, turnId: "refresh-turn",
    autoLoad: true,
  });
  assert.equal(
    userExpandedRestore.runtimes[refreshSid].turns[0].detailAutoLoad,
    true,
    "the user's later disclosure still upgrades the repair into full pagination",
  );
  assert.equal(
    userExpandedRestore.runtimes[refreshSid].turns[0]
      .detailRestoreIncomplete,
    false,
  );

  const managedClaudeSid = "managed-claude-session";
  const managedClaudeIdle = reduce({
    ...initialState,
    sessions: [{
      session_id: managedClaudeSid,
      engine: "claude",
      space: "code",
      summary: "managed Claude",
      state: "running",
    }],
    runtimes: {
      [managedClaudeSid]: {
        ...createRuntime(),
        state: "running",
      },
    },
  }, { type: "event", event: event({
    type: "state",
    sid: managedClaudeSid,
    seq: 44,
    state: "idle",
  }), ownership: {
    scopeKey: "machine-a:code:claude",
    machineId: "machine-a",
    engine: "claude",
    space: "code",
    surfaceEpoch: 4,
    connectionGeneration: 7,
  } });
  assert.equal(managedClaudeIdle.runtimes[managedClaudeSid].state, "idle");
  assert.equal(managedClaudeIdle.sessions[0].state, "idle");
  assert.equal(mergeSessionActivityState(
    managedClaudeIdle.sessions[0].state,
    managedClaudeIdle.runtimes[managedClaudeSid].state,
  ), "idle",
  "an authoritative managed-turn terminal clears a stale catalog running badge");
  const wrongSurfaceTerminal = reduce({
    ...managedClaudeIdle,
    sessions: [{
      ...managedClaudeIdle.sessions[0],
      state: "running",
    }],
  }, { type: "event", event: event({
    type: "state", sid: managedClaudeSid, seq: 45, state: "idle",
  }), ownership: {
    scopeKey: "machine-a:code:codex",
    machineId: "machine-a",
    engine: "codex",
    space: "code",
    surfaceEpoch: 5,
    connectionGeneration: 7,
  } });
  assert.equal(wrongSurfaceTerminal.sessions[0].state, "running",
    "a delayed lifecycle frame from another owned surface cannot clear this row");
  const orphanBtwIdle = reduce(managedClaudeIdle, {
    type: "event", event: event({
      type: "state", sid: "btw-orphan", seq: 46, state: "idle",
    }),
  });
  assert.deepEqual(orphanBtwIdle.sessions, managedClaudeIdle.sessions,
    "a fork lifecycle frame cannot create a top-level sidebar row");

  const backgroundRunning = reduce({
    ...initialState,
    sessions: [{
      session_id: "private-app-session",
      engine: "codex",
      space: "code",
      summary: "private app",
      state: "idle",
    }],
  }, { type: "event", event: event({
    type: "session_activity",
    engine: "codex",
    session_id: "private-app-session",
    state: "running",
  }) });
  assert.equal(backgroundRunning.sessions[0].state, "running",
    "a private Codex App turn must light the non-focused sidebar row");
  const backgroundIdle = reduce(backgroundRunning, {
    type: "event", event: event({
      type: "session_activity",
      engine: "codex",
      session_id: "private-app-session",
      state: "idle",
    }),
  });
  assert.equal(backgroundIdle.sessions[0].state, "idle",
    "the sidebar row must clear when the private App turn completes");

  const {
    buildCalendarDays, parseLocalDateTime, placeDateTimePopover, toLocalDateTime,
  } =
    await reducerHarness.ssrLoadModule("/src/date-time.ts");
  const { DateTimePicker } =
    await reducerHarness.ssrLoadModule("/src/components/DateTimePicker.tsx");
  const { CommandSheet: ModelCommandSheet } =
    await reducerHarness.ssrLoadModule("/src/components/CommandSheet.tsx");
  const sampleDate = new Date(2026, 6, 17, 9, 5);
  assert.equal(toLocalDateTime(sampleDate), "2026-07-17T09:05");
  assert.equal(parseLocalDateTime("2026-02-30T09:05"), null,
    "invalid local calendar dates must fail closed");
  const calendarDays = buildCalendarDays(new Date(2026, 6, 1), sampleDate);
  assert.equal(calendarDays.length, 42);
  assert.equal(toLocalDateTime(calendarDays[0].date).slice(0, 10), "2026-06-29",
    "the custom calendar must use a stable Monday-first six-week grid");
  assert.deepEqual(placeDateTimePopover(
    { left: 275, top: 234, bottom: 284 }, { width: 1280, height: 720 }),
  { left: 275, top: 232 },
  "a short viewport must clamp the complete popover inside the visible area");
  assert.deepEqual(placeDateTimePopover(
    { left: 548, top: 600, bottom: 650 }, { width: 1920, height: 1200 }),
  { left: 548, top: 658 },
  "a tall viewport should keep the popover directly below its trigger");
  const datePickerMarkup = renderToStaticMarkup(createElement(DateTimePicker, {
    value: "2026-07-17T09:05", onChange: () => undefined,
  }));
  assert.match(datePickerMarkup, /执行时间/);
  assert.match(datePickerMarkup, /aria-haspopup="dialog"/);
  const claudeModelsMarkup = renderToStaticMarkup(createElement(ModelCommandSheet, {
    open: true, kind: "models", engine: "claude", onClose: () => undefined,
  }));
  assert.match(claudeModelsMarkup, /Opus 5/);
  assert.match(claudeModelsMarkup, /1M 上下文/);
  assert.doesNotMatch(claudeModelsMarkup, /自定义 \/ Provider 模型 ID|custom-claude-model/,
    "Claude's ordinary model sheet must not expose raw provider ids");
  const codexModelsMarkup = renderToStaticMarkup(createElement(ModelCommandSheet, {
    open: true, kind: "models", engine: "codex", onClose: () => undefined,
  }));
  assert.doesNotMatch(codexModelsMarkup, /自定义 \/ Provider 模型 ID/,
    "Codex must keep using its authoritative app-server catalog");

  const snapshotOnly = reduce(initialState, { type: "event", event: event({
    type: "snapshot", sid: "background-code", cc_session_id: "background-code",
    state: "idle", tail_text: "",
  }) });
  assert.equal(snapshotOnly.focusedSid, null,
    "a background snapshot must not focus across Code/Work surfaces");
  const unannounced = createRuntime();
  assert.equal(unannounced.model, "");
  assert.equal(unannounced.effort, "");
  assert.equal(unannounced.perm, "");
  assert.equal(unannounced.fast, null);
  const sid = "race-a";
  const otherSid = "race-b";
  let controlRequestState = {
    ...initialState,
    focusedSid: sid,
    runtimes: { [sid]: createRuntime() },
  };
  controlRequestState = reduce(controlRequestState, {
    type: "begin_context_request", sid, requestId: "context-request",
  });
  controlRequestState = reduce(controlRequestState, { type: "event", event: event({
    type: "error", sid, code: "not_running", message: "会话已离线",
    request_id: "context-request",
  }) });
  assert.equal(controlRequestState.runtimes[sid].contextRequestId, null);
  assert.equal(controlRequestState.runtimes[sid].contextError,
    "当前会话暂时不可用，请重新进入后重试。",
    "a targeted context failure must replace the infinite loading state");
  assert.equal(controlRequestState.banner, undefined,
    "a correlated context failure must stay inside the context control");
  const completedContextSid = "completed-turn-context-failure";
  const completedContextReport = event({
    type: "context_report",
    sid: completedContextSid,
    total_tokens: 120_000,
    max_tokens: 500_000,
    percentage: 24,
    available: true,
    categories: [],
  });
  let completedContextState = {
    ...initialState,
    focusedSid: completedContextSid,
    runtimes: {
      [completedContextSid]: {
        ...createRuntime(),
        state: "idle" as const,
        contextReport: completedContextReport,
        turns: [{
          id: "completed-context-turn",
          prompt: "finish normally",
          done: true,
          doneTs: 20_000,
          blocks: [{
            kind: "text" as const,
            message_id: "completed-context-answer",
            channel: "final" as const,
            text: "done",
            done: true,
          }],
        }],
      },
    },
  };
  completedContextState = reduce(completedContextState, {
    type: "begin_context_request",
    sid: completedContextSid,
    requestId: "post-turn-context",
  });
  completedContextState = reduce(completedContextState, {
    type: "event", event: event({
      type: "error", sid: completedContextSid, code: "internal",
      message: "Control request timeout: get_context_usage",
      request_id: "post-turn-context",
    }),
  });
  assert.equal(completedContextState.banner, undefined);
  assert.equal(
    completedContextState.runtimes[completedContextSid].contextReport,
    completedContextReport,
    "a failed refresh must preserve the last valid context reading",
  );
  assert.equal(
    completedContextState.runtimes[completedContextSid].turns[0].done,
    true,
    "a post-turn metadata failure must not reopen the completed narrative",
  );
  assert.equal(
    completedContextState.runtimes[completedContextSid].turns[0]
      .blocks[0].done,
    true,
    "a post-turn metadata failure must preserve the terminal answer footer",
  );
  controlRequestState = reduce(controlRequestState, {
    type: "begin_status_request", sid, requestId: "status-request",
  });
  controlRequestState = reduce(controlRequestState, { type: "event", event: event({
    type: "error", sid, code: "not_running", message: "状态不可用",
    request_id: "status-request",
  }) });
  assert.equal(controlRequestState.runtimes[sid].statusRequestId, null);
  assert.equal(controlRequestState.runtimes[sid].statusError,
    "当前会话暂时不可用，请重新进入后重试。",
    "a targeted status failure must close its loading state");
  let diffRequestState = reduce(controlRequestState, {
    type: "open_artifact_loading", sid, file: "src/app.ts", requestId: "diff-request",
  });
  diffRequestState = reduce(diffRequestState, { type: "event", event: event({
    type: "diff_report", sid, file: "src/app.ts", diff: "stale",
    request_id: "older-diff-request",
  }) });
  assert.equal(diffRequestState.artifact?.loading, true,
    "a stale diff response must not replace the active request");
  diffRequestState = reduce(diffRequestState, { type: "event", event: event({
    type: "error", sid, code: "not_running", message: "差异不可用",
    request_id: "diff-request",
  }) });
  assert.equal(diffRequestState.artifact?.loading, false);
  assert.equal(diffRequestState.artifact?.error,
    "当前会话暂时不可用，请重新进入后重试。",
    "a correlated diff failure must close its loading state");
  const untouched = {
    id: "b-turn", prompt: "other", done: true, blocks: [], ts: 1000,
  };
  let state = {
    ...initialState,
    connState: "connected",
    wrapperOnline: true,
    focusedSid: sid,
    newChat: { cwd: "~", model: null, effort: null },
    runtimes: {
      [sid]: { ...createRuntime(), state: "running", syncReady: true },
      [otherSid]: { ...createRuntime(), turns: [untouched], syncReady: true },
    },
  };
  const historySid = "background-history-invalidated";
  const staleTurns = [
    { id: "kept-before-rewind", prompt: "保留", blocks: [], done: true },
    { id: "removed-by-rewind", prompt: "应删除", blocks: [], done: true },
  ];
  state = {
    ...state,
    artifact: { file: "changed.ts", kind: "file", sid: historySid },
    runtimes: {
      ...state.runtimes,
      [historySid]: {
        ...createRuntime(),
        turns: staleTurns,
        pendingQuestion: {
          ask_id: "old-question", question: "旧问题", options: [],
        },
      },
    },
  };
  state = reduce(state, { type: "event", event: event({
    type: "history_invalidated", sid: historySid,
    session_id: historySid, revision: "history-rev-2", reason: "rollback",
  }) });
  assert.deepEqual(state.runtimes[historySid].turns, []);
  assert.equal(state.runtimes[historySid].pendingQuestion, null);
  assert.equal(state.runtimes[historySid].historyInvalidated, true);
  assert.equal(state.runtimes[historySid].loading, true);
  assert.equal(state.artifact, null);

  state = {
    ...state,
    artifact: { file: "stale-after-file-rollback.ts", kind: "file", sid: historySid },
  };
  state = reduce(state, { type: "event", event: event({
    type: "artifact_invalidated", sid: historySid,
    session_id: historySid, reason: "rollback",
  }) });
  assert.equal(state.artifact, null,
    "a replayed file rollback marker must close stale previews");

  state = {
    ...state,
    artifact: { file: "stale-files-only-diff.ts", kind: "gitdiff", sid: historySid,
      sections: [] },
  };
  state = reduce(state, { type: "event", event: event({
    type: "rollback_result", sid: historySid, session_id: historySid,
    engine: "codex", restore: "files", conversation: "skipped",
    files: "succeeded", restored_turns: 1, conflicts: [],
  }) });
  assert.equal(state.artifact, null,
    "a files-only rollback result must close stale diff and preview state");

  state = {
    ...state,
    artifact: { file: "other-session.ts", kind: "file", sid: otherSid },
  };
  state = reduce(state, { type: "event", event: event({
    type: "rollback_result", sid: historySid, session_id: historySid,
    engine: "codex", restore: "files", conversation: "skipped",
    files: "succeeded", restored_turns: 1, conflicts: [],
  }) });
  assert.equal(state.artifact?.sid, otherSid,
    "a background file rollback must not close another session's preview");

  state = reduce(state, {
    type: "hydrate_cache", sid: historySid, turns: staleTurns,
    revision: "history-rev-1",
  });
  assert.deepEqual(state.runtimes[historySid].turns, [],
    "a marker must reject a late IndexedDB hydrate");
  assert.equal(state.runtimes[historySid].loading, true);

  state = reduce(state, { type: "event", event: event({
    type: "history", sid: historySid, session_id: historySid,
    revision: "history-rev-1", before: "old-page", has_more: false,
    events: [event({
      type: "user_msg", sid: historySid, msg_id: "removed-by-rewind",
      prompt: "应删除",
    })],
  }) });
  assert.deepEqual(state.runtimes[historySid].turns, [],
    "a late pagination page cannot satisfy a destructive marker");
  assert.equal(state.runtimes[historySid].historyInvalidated, true);

  state = reduce(state, { type: "event", event: event({
    type: "history", sid: historySid, session_id: historySid,
    revision: "history-rev-1", has_more: false,
    events: [event({
      type: "user_msg", sid: historySid, msg_id: "removed-by-rewind",
      prompt: "应删除",
    })],
  }) });
  assert.deepEqual(state.runtimes[historySid].turns, [],
    "a late first page from the old revision cannot satisfy the marker");
  assert.equal(state.runtimes[historySid].pendingHistoryRevision, "history-rev-2");

  state = reduce(state, { type: "event", event: event({
    type: "history", sid: historySid, session_id: historySid,
    revision: "history-rev-2", has_more: false,
    events: [
      event({ type: "user_msg", sid: historySid,
        msg_id: "kept-before-rewind", prompt: "保留" }),
      event({ type: "assistant_msg_start", sid: historySid,
        message_id: "kept-answer" }),
      event({ type: "delta", sid: historySid,
        message_id: "kept-answer", text: "已保留" }),
      event({ type: "assistant_msg_end", sid: historySid,
        message_id: "kept-answer" }),
      event({ type: "turn_end", sid: historySid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.deepEqual(state.runtimes[historySid].turns.map(
    (turn: { id: string }) => turn.id), ["kept-before-rewind"]);
  assert.equal(state.runtimes[historySid].historyInvalidated, false);
  assert.equal(state.runtimes[historySid].loading, false);
  assert.ok(!state.runtimes[historySid].turns.some(
    (turn: { id: string }) => turn.id === "removed-by-rewind"));

  const prunedHistorySid = "pruned-background-invalidated";
  state = reduce(state, { type: "event", event: event({
    type: "history_invalidated", sid: prunedHistorySid,
    session_id: prunedHistorySid, revision: "pruned-rev-2", reason: "rollback",
  }) });
  assert.equal(state.runtimes[prunedHistorySid].historyInvalidated, true,
    "a background marker must survive until that session is focused");
  state = reduce(state, { type: "focus_session", sid: prunedHistorySid });
  state = reduce(state, { type: "event", event: event({
    type: "session_focus", sid: prunedHistorySid,
    session_id: prunedHistorySid, cwd: "/tmp/project",
  }) });
  assert.equal(state.runtimes[prunedHistorySid].loading, true,
    "session_focus must keep loading until authoritative history arrives");

  // A wrapper restart/non-resident resume has no in-memory rollback marker.
  // The History revision must still replace stale IndexedDB-completed turns,
  // while retaining only this connection's unfinished optimistic tail.
  const restartSid = "restart-stale-idb";
  let restartState = reduce({
    ...initialState, focusedSid: restartSid,
  }, {
    type: "hydrate_cache", sid: restartSid, turns: [
      ...staleTurns,
      { id: "stale-half-turn", prompt: "旧的半截输出", blocks: [], done: false },
    ],
    revision: "boot-old:1",
  });
  restartState = reduce(restartState, {
    type: "query_sent", sid: restartSid, prompt: "正在发送",
    msg_id: "optimistic-live", ts: 20_000,
  });
  restartState = reduce(restartState, { type: "event", event: event({
    type: "history", sid: restartSid, session_id: restartSid,
    revision: "boot-new:1", has_more: false,
    events: [
      event({ type: "user_msg", sid: restartSid,
        msg_id: "kept-before-rewind", prompt: "保留" }),
      event({ type: "turn_end", sid: restartSid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.deepEqual(restartState.runtimes[restartSid].turns.map(
    (turn: { id: string }) => turn.id),
  ["kept-before-rewind", "optimistic-live"]);
  assert.equal(restartState.runtimes[restartSid].historyRevision, "boot-new:1");
  assert.ok(!restartState.runtimes[restartSid].turns.some(
    (turn: { id: string }) => turn.id === "removed-by-rewind"));
  assert.ok(!restartState.runtimes[restartSid].turns.some(
    (turn: { id: string }) => turn.id === "stale-half-turn"),
  "an unfinished IndexedDB row is not a genuine current-connection tail");
  assert.equal(restartState.runtimes[restartSid].turns.at(-1)?.done, false,
    "revision replacement must preserve a genuine live optimistic tail");

  const resumedLiveSid = "restart-resumed-live-tail";
  let resumedLiveState = reduce({
    ...initialState, focusedSid: resumedLiveSid,
  }, {
    type: "hydrate_cache", sid: resumedLiveSid, turns: [{
      id: "resumed-live", prompt: "仍在执行", blocks: [], done: false,
    }], revision: "boot-old:2",
  });
  resumedLiveState = reduce(resumedLiveState, { type: "event", event: event({
    type: "delta", sid: resumedLiveSid,
    message_id: "assistant-current", text: "新的实时输出",
  }) });
  resumedLiveState = reduce(resumedLiveState, { type: "event", event: event({
    type: "history", sid: resumedLiveSid, session_id: resumedLiveSid,
    revision: "boot-new:2", has_more: false, in_progress: true, events: [],
  }) });
  assert.deepEqual(resumedLiveState.runtimes[resumedLiveSid].turns.map(
    (turn: { id: string }) => turn.id), ["resumed-live"],
  "a live continuation validates the matching unfinished cached turn");

  // A reconnect may resume at any lifecycle frame, not only a text delta.
  // Every frame that proves the cached turn is live must remove its hydrated
  // marker and advance the live watermark used by History race protection.
  const assertLiveValidation = (
    name: string, blocks: Record<string, unknown>[], liveEvent: ServerEvent,
  ) => {
    const liveSid = `cached-${name}`;
    let liveState = reduce({ ...initialState, focusedSid: liveSid }, {
      type: "hydrate_cache", sid: liveSid, turns: [{
        id: "cached-live", prompt: "继续执行", blocks, done: false,
      }], revision: "live-rev",
    });
    liveState = reduce(liveState, { type: "event", event: liveEvent });
    assert.deepEqual(liveState.runtimes[liveSid].hydratedCacheTurnIds, [],
      `${name} must validate the hydrated turn`);
    assert.equal(liveState.runtimes[liveSid].lastLiveSeq, liveEvent.seq,
      `${name} must advance the live History watermark`);
  };
  assertLiveValidation("tool-delta", [{
    kind: "tool", message_id: "m", tool_use_id: "tool-1",
    tool: "Bash", input: {}, done: false,
  }], event({
    type: "tool_delta", sid: "cached-tool-delta", seq: 21,
    tool_use_id: "tool-1", stream: "output", delta: "running",
  }));
  assertLiveValidation("tool-result", [{
    kind: "tool", message_id: "m", tool_use_id: "tool-1",
    tool: "Bash", input: {}, done: false,
  }], event({
    type: "tool_result", sid: "cached-tool-result", seq: 22,
    tool_use_id: "tool-1", content: "done", is_error: false,
  }));
  assertLiveValidation("assistant-end", [{
    kind: "text", message_id: "assistant-1", text: "done", done: false,
    channel: "final",
  }], event({
    type: "assistant_msg_end", sid: "cached-assistant-end", seq: 23,
    message_id: "assistant-1", channel: "final",
  }));
  assertLiveValidation("turn-end", [], event({
    type: "turn_end", sid: "cached-turn-end", seq: 24,
    result: { subtype: "success", duration_ms: 1, is_error: false },
  }));
  assertLiveValidation("error", [], event({
    type: "error", sid: "cached-error", seq: 25, msg_id: "cached-live",
    code: "internal", message: "failed",
  }));

  // The History read began before assistant completion. Even with the same
  // revision, its empty snapshot must not delete the now-completed live turn.
  const completedRaceSid = "same-revision-completed-live-race";
  let completedRace = reduce({
    ...initialState, focusedSid: completedRaceSid,
    runtimes: { [completedRaceSid]: createRuntime() },
  }, {
    type: "query_sent", sid: completedRaceSid, prompt: "race",
    msg_id: "live-completed", ts: 30_000,
  });
  completedRace = reduce(completedRace, { type: "event", event: event({
    type: "user_msg", sid: completedRaceSid, seq: 31,
    msg_id: "live-completed", prompt: "race",
  }) });
  completedRace = reduce(completedRace, { type: "event", event: event({
    type: "turn_end", sid: completedRaceSid, seq: 32,
    result: { subtype: "success", duration_ms: 1, is_error: false },
  }) });
  completedRace = reduce(completedRace, { type: "event", event: event({
    type: "history", sid: completedRaceSid, session_id: completedRaceSid,
    revision: "race-rev", build_seq: 1, live_seq: 30,
    has_more: false, events: [],
  }) });
  assert.deepEqual(completedRace.runtimes[completedRaceSid].turns.map(
    (turn: { id: string }) => turn.id), ["live-completed"]);
  assert.equal(completedRace.runtimes[completedRaceSid].turns[0].done, true);

  // A thread/settings notification can arrive while a transcript History read
  // is already in flight.  Keep the newer live model/effort even though the
  // older History page remains useful for narrative turns.
  const settingsRaceSid = "history-cannot-revert-live-settings";
  let settingsRace = {
    ...initialState, focusedSid: settingsRaceSid,
    runtimes: { [settingsRaceSid]: createRuntime() },
  };
  settingsRace = reduce(settingsRace, { type: "event", event: event({
    type: "model", sid: settingsRaceSid, seq: 34, model: "gpt-live",
  }) });
  settingsRace = reduce(settingsRace, { type: "event", event: event({
    type: "effort", sid: settingsRaceSid, seq: 35, effort: "high",
  }) });
  settingsRace = reduce(settingsRace, { type: "event", event: event({
    type: "history", sid: settingsRaceSid, session_id: settingsRaceSid,
    revision: "settings-race-rev", build_seq: 1, live_seq: 33,
    has_more: false, events: [
      event({ type: "model", sid: settingsRaceSid, model: "gpt-stale" }),
      event({ type: "effort", sid: settingsRaceSid, effort: "low" }),
    ],
  }) });
  assert.equal(settingsRace.runtimes[settingsRaceSid].model, "gpt-live");
  assert.equal(settingsRace.runtimes[settingsRaceSid].effort, "high");

  // A current authoritative head page can repair a lost terminal event. This is
  // the exact /review interrupt failure mode: the browser saw interrupting and a
  // running process, but missed TurnEnd. Explicit in_progress=false closes the
  // retained live tail and its child activity instead of spinning forever.
  const idleRepairSid = "authoritative-idle-repairs-interrupt";
  let idleRepair = {
    ...initialState, focusedSid: idleRepairSid,
    runtimes: { [idleRepairSid]: createRuntime() },
  };
  idleRepair = reduce(idleRepair, { type: "event", event: event({
    type: "user_msg", sid: idleRepairSid, seq: 41,
    msg_id: "review-turn", prompt: "",
  }) });
  idleRepair = reduce(idleRepair, { type: "event", event: event({
    type: "process", sid: idleRepairSid, seq: 42,
    item_id: "review-hook", kind: "hook", phase: "start",
    status: "running", title: "Hook",
  }) });
  idleRepair = reduce(idleRepair, { type: "event", event: event({
    type: "state", sid: idleRepairSid, state: "interrupting",
  }) });
  const duplicateInterruptError = reduce(idleRepair, {
    type: "event", event: event({
      type: "error", sid: idleRepairSid, code: "not_running",
      message: "该会话没有正在运行的回合",
    }),
  });
  assert.equal(duplicateInterruptError.runtimes[idleRepairSid].state,
    "interrupting", "not_running alone must not guess that an interrupt is terminal");
  assert.equal(duplicateInterruptError.runtimes[idleRepairSid].turns[0].done, false);
  idleRepair = reduce(idleRepair, { type: "event", event: event({
    type: "history", sid: idleRepairSid, session_id: idleRepairSid,
    revision: "idle-repair-rev", generation: "wrapper-one",
    build_seq: 1, live_seq: 42, authoritative: true,
    in_progress: false, has_more: false, events: [],
  }) });
  const repairedRuntime = idleRepair.runtimes[idleRepairSid];
  assert.equal(repairedRuntime.state, "idle");
  assert.equal(repairedRuntime.turns[0].done, true);
  assert.equal(repairedRuntime.turns[0].interrupted, true);
  const repairedProcess = repairedRuntime.turns[0].blocks[0] as {
    done: boolean; status: string;
  };
  assert.equal(repairedProcess.done, true);
  assert.equal(repairedProcess.status, "interrupted");

  // Explicit in_progress=true remains open even when transcript EOF synthesizes
  // TurnEnd. Undefined keeps the older-wrapper fallback to local runtime state.
  const liveHistorySid = "authoritative-running-keeps-tail";
  let liveHistory = {
    ...initialState, focusedSid: liveHistorySid,
    runtimes: { [liveHistorySid]: createRuntime() },
  };
  liveHistory = reduce(liveHistory, { type: "event", event: event({
    type: "user_msg", sid: liveHistorySid, seq: 51,
    msg_id: "live-review", prompt: "",
  }) });
  liveHistory = reduce(liveHistory, { type: "event", event: event({
    type: "state", sid: liveHistorySid, state: "running",
  }) });
  const liveHistoryFrame = {
    type: "history", sid: liveHistorySid, session_id: liveHistorySid,
    revision: "live-history-rev", generation: "wrapper-one",
    build_seq: 1, live_seq: 51, authoritative: true,
    in_progress: true, has_more: false,
    events: [
      event({ type: "user_msg", sid: liveHistorySid,
        msg_id: "live-review", prompt: "" }),
      event({ type: "turn_end", sid: liveHistorySid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  };
  liveHistory = reduce(liveHistory, { type: "event", event: event(liveHistoryFrame) });
  assert.equal(liveHistory.runtimes[liveHistorySid].state, "running");
  assert.equal(liveHistory.runtimes[liveHistorySid].turns[0].done, false);

  // A native/external Codex turn is visually active in the sidebar but does
  // not become a wrapper-owned running state. Otherwise the composer exposes a
  // Stop action which cannot interrupt that external writer.
  const mirroredSid = "external-history-activity";
  let mirroredState = {
    ...initialState, focusedSid: mirroredSid,
    runtimes: { [mirroredSid]: createRuntime() },
  };
  mirroredState = reduce(mirroredState, { type: "event", event: event({
    type: "history", sid: mirroredSid, session_id: mirroredSid,
    revision: "external-activity-rev", build_seq: 1,
    external: true, in_progress: true, has_more: false, events: [],
  }) });
  assert.equal(mirroredState.runtimes[mirroredSid].state, "idle");
  assert.equal(mirroredState.runtimes[mirroredSid].mirroredRunning, true);
  mirroredState = reduce(mirroredState, { type: "event", event: event({
    type: "history", sid: mirroredSid, session_id: mirroredSid,
    revision: "external-activity-rev", build_seq: 2,
    external: false, in_progress: false, has_more: false, events: [],
  }) });
  assert.equal(mirroredState.runtimes[mirroredSid].state, "idle");
  assert.equal(mirroredState.runtimes[mirroredSid].mirroredRunning, false);

  // A native Codex task can accept another user message while still running.
  // The next authoritative head must close the old placeholder as `steered`
  // and leave exactly the latest prompt open instead of stacking "thinking".
  const steeredSid = "external-steering-replaces-thinking";
  let steeredState = {
    ...initialState, focusedSid: steeredSid,
    runtimes: { [steeredSid]: createRuntime() },
  };
  steeredState = reduce(steeredState, { type: "event", event: event({
    type: "history", sid: steeredSid, session_id: steeredSid,
    revision: "steered-rev", build_seq: 1,
    external: true, in_progress: true, has_more: true,
    events: [event({ type: "user_msg", sid: steeredSid,
      msg_id: "first-prompt", prompt: "first" })],
  }) });
  steeredState = reduce(steeredState, { type: "event", event: event({
    type: "history", sid: steeredSid, session_id: steeredSid,
    revision: "steered-rev", build_seq: 2,
    external: true, in_progress: true, has_more: true,
    events: [
      event({ type: "user_msg", sid: steeredSid,
        msg_id: "first-prompt", prompt: "first" }),
      event({ type: "turn_end", sid: steeredSid,
        result: { subtype: "steered", duration_ms: 0, is_error: false } }),
      event({ type: "user_msg", sid: steeredSid,
        msg_id: "second-prompt", prompt: "second" }),
    ],
  }) });
  assert.deepEqual(steeredState.runtimes[steeredSid].turns.map(
    (turn: { prompt: string; done: boolean }) => [turn.prompt, turn.done]), [
      ["first", true], ["second", false],
    ]);

  // Expanding detail before a native steer must not pin the old lifecycle.
  // The next summary is authoritative for done/error/timestamps, while its
  // lightweight commentary/compaction blocks merge with already-loaded detail.
  const detailedSteerSid = "external-steering-after-detail";
  let detailedSteerState = {
    ...initialState, focusedSid: detailedSteerSid,
    runtimes: { [detailedSteerSid]: createRuntime() },
  };
  detailedSteerState = reduce(detailedSteerState, {
    type: "event", event: event({
      type: "history", sid: detailedSteerSid,
      session_id: detailedSteerSid, revision: "detail-steer-rev",
      build_seq: 1, detail: "summary", external: true, in_progress: true,
      has_more: true, events: [], turns: [{
        id: "first-prompt", prompt: "first", done: false,
        detailEventCount: 2, detailLoaded: false, blocks: [],
      }],
    }),
  });
  detailedSteerState = reduce(detailedSteerState, {
    type: "event", event: event({
      type: "turn_detail", sid: detailedSteerSid,
      session_id: detailedSteerSid, revision: "detail-steer-rev",
      turn_id: "first-prompt", authoritative: true, events: [
        event({ type: "user_msg", sid: detailedSteerSid,
          msg_id: "first-prompt", prompt: "first" }),
        event({ type: "assistant_msg_start", sid: detailedSteerSid,
          message_id: "commentary-first", channel: "commentary" }),
        event({ type: "delta", sid: detailedSteerSid,
          message_id: "commentary-first", channel: "commentary",
          text: "first progress" }),
        event({ type: "assistant_msg_end", sid: detailedSteerSid,
          message_id: "commentary-first", channel: "commentary" }),
        event({ type: "tool_use", sid: detailedSteerSid,
          message_id: "tool-message", tool_use_id: "tool-open",
          tool: "exec_command", input: {} }),
      ],
    }),
  });
  detailedSteerState = reduce(detailedSteerState, {
    type: "event", event: event({
      type: "history", sid: detailedSteerSid,
      session_id: detailedSteerSid, revision: "detail-steer-rev",
      build_seq: 2, detail: "summary", external: true, in_progress: true,
      has_more: true, events: [], turns: [
        {
          id: "first-prompt", prompt: "first", done: true,
          doneTs: 2_000,
          detailEventCount: 2, detailLoaded: false, blocks: [{
            kind: "text", message_id: "commentary-first",
            text: "first progress", done: true, channel: "commentary",
          }],
        },
        {
          id: "second-prompt", prompt: "second", done: false,
          detailEventCount: 2, detailLoaded: false, blocks: [
            {
              kind: "text", message_id: "commentary-second",
              text: "second progress", done: true, channel: "commentary",
            },
            {
              kind: "process", item_id: "compact-second",
              processKind: "compaction", phase: "end",
              status: "succeeded", title: "压缩上下文", done: true,
            },
          ],
        },
      ],
    }),
  });
  const detailedSteerTurns = detailedSteerState.runtimes[
    detailedSteerSid
  ].turns;
  assert.deepEqual(detailedSteerTurns.map(
    (turn: { prompt: string; done: boolean }) => [turn.prompt, turn.done]), [
      ["first", true], ["second", false],
    ]);
  assert.equal(detailedSteerTurns[0].durationMs, undefined,
    "a synthetic steered TurnEnd must not render a fake zero-second duration");
  assert.equal(detailedSteerTurns[0].blocks.some(
    (block: { done: boolean }) => !block.done), false);
  assert.equal(detailedSteerTurns[1].blocks.some(
    (block: { kind: string; text?: string }) =>
      block.kind === "text" && block.text === "second progress"), true);
  assert.equal(detailedSteerTurns[1].blocks.some(
    (block: { kind: string; processKind?: string }) =>
      block.kind === "process" && block.processKind === "compaction"), true);

  const testCompactionReducerRaces = () => {
  // A compact marker can reach the live optimistic row before the next
  // authoritative History head binds that row's native user-message id. The
  // reducer must reconcile those two identities instead of painting the same
  // question twice after automatic context compression.
  const compactDuplicateSid = "compact-does-not-duplicate-question";
  const compactDuplicateNativeTurn = "compact-native-turn";
  const compactDuplicateLiveTurn = {
    id: "compact-browser-message",
    clientMsgId: "compact-browser-message",
    prompt: "继续修复问题",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "compact-live-marker",
      processKind: "compaction" as const,
      phase: "end" as const,
      status: "succeeded" as const,
      turn_id: compactDuplicateNativeTurn,
      title: "压缩上下文",
      done: true,
    }],
  };
  let compactDuplicateState = {
    ...initialState,
    focusedSid: compactDuplicateSid,
    runtimes: {
      [compactDuplicateSid]: {
        ...createRuntime(),
        state: "running" as const,
        turns: [compactDuplicateLiveTurn],
      },
    },
  };
  compactDuplicateState = reduce(compactDuplicateState, {
    type: "event", event: event({
      type: "history",
      sid: compactDuplicateSid,
      session_id: compactDuplicateSid,
      revision: "compact-duplicate-revision",
      generation: "compact-duplicate-generation",
      build_seq: 1,
      detail: "summary",
      in_progress: true,
      has_more: false,
      events: [],
      turns: [{
        id: "compact-history-message",
        forkPointId: compactDuplicateNativeTurn,
        prompt: "继续修复问题",
        done: false,
        detailEventCount: 1,
        detailLoaded: false,
        blocks: [],
      }],
    }),
  });
  const compactDuplicateTurns =
    compactDuplicateState.runtimes[compactDuplicateSid].turns;
  assert.equal(compactDuplicateTurns.length, 1,
    "automatic context compression must not add a duplicate user question");
  assert.equal(compactDuplicateTurns[0].prompt, "继续修复问题");
  assert.equal(compactDuplicateTurns[0].historyTurnId,
    "compact-history-message",
    "the merged row keeps the authoritative History lookup identity");
  assert.equal(compactDuplicateTurns[0].blocks.some(
    (block: { kind: string; processKind?: string }) =>
      block.kind === "process" && block.processKind === "compaction"), true);

  const terminalCompactSid = "terminal-compact-keeps-question";
  let terminalCompactState = {
    ...initialState,
    focusedSid: terminalCompactSid,
    sessions: [{ session_id: terminalCompactSid,
      engine: "codex" as const, space: "code" as const }],
    runtimes: {
      [terminalCompactSid]: {
        ...createRuntime(),
        state: "running" as const,
        turns: [{ ...compactDuplicateLiveTurn }],
      },
    },
  };
  terminalCompactState = reduce(terminalCompactState, {
    type: "event", event: event({
      type: "history",
      sid: terminalCompactSid,
      session_id: terminalCompactSid,
      revision: "terminal-compact-revision",
      generation: "terminal-compact-generation",
      build_seq: 1,
      detail: "summary",
      in_progress: false,
      has_more: false,
      events: [],
      turns: [{
        id: "terminal-compact-user",
        forkPointId: compactDuplicateNativeTurn,
        prompt: "继续修复问题",
        done: true,
        detailEventCount: 2,
        detailLoaded: false,
        blocks: [{
          kind: "process" as const,
          item_id: "terminal-compact-marker",
          processKind: "compaction" as const,
          phase: "end" as const,
          status: "succeeded" as const,
          turn_id: compactDuplicateNativeTurn,
          title: "压缩上下文",
          done: true,
        }, {
          kind: "text" as const,
          message_id: "terminal-compact-answer",
          channel: "final" as const,
          text: "已完成",
          done: true,
        }],
      }],
    }),
  });
  const terminalCompactTurns =
    terminalCompactState.runtimes[terminalCompactSid].turns;
  assert.equal(terminalCompactTurns.length, 1,
    "terminal compact History must retain exactly one user question");
  assert.equal(terminalCompactTurns[0].prompt, "继续修复问题");
  assert.equal(terminalCompactTurns[0].done, true);
  assert.equal(terminalCompactTurns[0].blocks.filter(
    (block: { kind: string; processKind?: string }) =>
      block.kind === "process" && block.processKind === "compaction").length, 1);
  assert.equal(terminalCompactTurns[0].blocks.some(
    (block: { kind: string; text?: string }) =>
      block.kind === "text" && block.text === "已完成"), true);

  const cachedCompactionSid = "canonical-history-repairs-cached-compaction";
  const cachedCompactionOwner = {
    ...canonicalCompactionOwner,
    id: "cached-history-owner",
    forkPointId: "cached-history-native",
    blocks: [{ ...orphanCompactionBlock,
      item_id: "cached-history-canonical-compact",
      turn_id: "cached-history-native" }, {
      kind: "text" as const,
      message_id: "cached-history-final",
      channel: "final" as const,
      text: "canonical answer",
      done: true,
    }],
  };
  const cachedCompactionOrphan = {
    ...completedCachedOrphan,
    id: "cached-history-orphan",
    blocks: [{ ...orphanCompactionBlock,
      item_id: "cached-history-stale-compact",
      turn_id: "cached-history-native" }],
  };
  let cachedCompactionState = reduce({
    ...initialState,
    focusedSid: cachedCompactionSid,
    sessions: [{ session_id: cachedCompactionSid,
      engine: "codex" as const, space: "code" as const }],
  }, {
    type: "hydrate_cache",
    sid: cachedCompactionSid,
    turns: [cachedCompactionOwner, cachedCompactionOrphan],
    revision: "cached-history-revision",
    generation: "cached-history-generation",
  });
  cachedCompactionState = reduce(cachedCompactionState, {
    type: "event", event: event({
      type: "history", sid: cachedCompactionSid,
      session_id: cachedCompactionSid,
      revision: "cached-history-revision",
      generation: "cached-history-generation",
      build_seq: 1,
      live_seq: 1,
      detail: "summary",
      in_progress: false,
      has_more: true,
      oldest_id: "cached-history-cursor",
      newest_id: "cached-history-owner",
      events: [],
      turns: [cachedCompactionOwner],
    }),
  });
  assert.deepEqual(cachedCompactionState.runtimes[cachedCompactionSid].turns.map(
    (turn: Turn) => turn.id), ["cached-history-owner", "cached-history-orphan"],
  "the first-page reducer preserves a different bounded-out compaction occurrence");
  assert.equal(cachedCompactionState.runtimes[cachedCompactionSid].hasMore, true,
    "cache self-heal preserves authoritative moving-head pagination");
  assert.equal(cachedCompactionState.runtimes[cachedCompactionSid].oldestId,
    "cached-history-cursor",
    "cache self-heal preserves the authoritative older-page cursor");
  cachedCompactionState = reduce(cachedCompactionState, {
    type: "event", event: event({
      type: "history", sid: cachedCompactionSid,
      session_id: cachedCompactionSid,
      revision: "cached-history-revision",
      generation: "cached-history-generation",
      build_seq: 2,
      live_seq: 1,
      detail: "summary",
      in_progress: false,
      has_more: true,
      oldest_id: "cached-history-cursor",
      newest_id: "cached-history-owner",
      events: [],
      turns: [cachedCompactionOwner],
    }),
  });
  assert.equal(cachedCompactionState.runtimes[cachedCompactionSid].turns.length, 2,
    "repeated first-page History keeps the unmatched occurrence stable");

  const orphanRaceProcess = (sid: string, nativeTurnId: string) => event({
    type: "process", sid, item_id: `${sid}-compaction`,
    kind: "compaction", phase: "end", status: "succeeded",
    turn_id: nativeTurnId, title: "压缩上下文", seq: 10,
  });
  const assertCompactionRaceRepaired = (
    label: string,
    orderedEvents: ServerEvent[],
    sid: string,
    messageId: string,
  ) => {
    let raceState = {
      ...initialState,
      focusedSid: sid,
      runtimes: { [sid]: { ...createRuntime(), state: "running" as const } },
    };
    for (const raceEvent of orderedEvents) {
      raceState = reduce(raceState, { type: "event", event: raceEvent });
    }
    const turns = raceState.runtimes[sid].turns;
    assert.equal(turns.length, 1, `${label} leaves one visible user row`);
    assert.equal(turns[0].id, messageId);
    assert.equal(turns[0].prompt, "继续修复问题");
    assert.deepEqual(turns[0].blocks.flatMap((block: Block) =>
      block.kind === "process" ? [block.processKind] : []),
    ["compaction"], `${label} retains exactly one compact disclosure`);
    assert.equal(turns[0].done, false,
      `${label} keeps the user turn lifecycle rather than the orphan lifecycle`);
    assert.deepEqual(
      raceState.runtimes[sid].liveDetailTurnIds,
      [messageId],
      `${label} transfers observed detail ownership to the surviving row`,
    );
  };

  for (const order of [
    ["process", "user", "binding"],
    ["process", "binding", "user"],
    ["binding", "user", "process"],
  ] as const) {
    const sid = `compact-orphan-${order.join("-")}`;
    const messageId = `${sid}-message`;
    const nativeTurnId = `${sid}-native`;
    const frames: Record<(typeof order)[number], ServerEvent> = {
      process: orphanRaceProcess(sid, nativeTurnId),
      user: event({ type: "user_msg", sid, msg_id: messageId,
        prompt: "继续修复问题", seq: 11 }),
      binding: event({ type: "turn_binding", sid, msg_id: messageId,
        turn_id: nativeTurnId, seq: 9 }),
    };
    assertCompactionRaceRepaired(
      order.join(" → "),
      order.map((name) => frames[name]),
      sid,
      messageId,
    );
  }

  const ownerBeforeOrphanSid = "compact-owner-before-orphan";
  const ownerBeforeOrphanMessage = "compact-owner-before-orphan-message";
  const ownerBeforeOrphanNative = "compact-owner-before-orphan-native";
  let ownerBeforeOrphan = {
    ...initialState,
    focusedSid: ownerBeforeOrphanSid,
    runtimes: {
      [ownerBeforeOrphanSid]: {
        ...createRuntime(),
        state: "running" as const,
        legacyLiveFallbackBlocked: true,
        turns: [{
          id: ownerBeforeOrphanMessage,
          clientMsgId: ownerBeforeOrphanMessage,
          prompt: "继续修复问题",
          blocks: [],
          done: false,
        }],
        liveDetailTurnIds: [],
      },
    },
  };
  const ownerBeforeOrphanRuntime = ownerBeforeOrphan.runtimes[
    ownerBeforeOrphanSid];
  const generatedOrphan = {
    id: ownerBeforeOrphanNative,
    prompt: "",
    done: false,
    blocks: [{ ...orphanCompactionBlock,
      item_id: "owner-before-orphan-compact",
      turn_id: ownerBeforeOrphanNative }],
  };
  ownerBeforeOrphan = {
    ...ownerBeforeOrphan,
    runtimes: {
      ...ownerBeforeOrphan.runtimes,
      [ownerBeforeOrphanSid]: {
        ...ownerBeforeOrphanRuntime,
        turns: [...ownerBeforeOrphanRuntime.turns, generatedOrphan],
        liveDetailTurnIds: [generatedOrphan.id],
      },
    },
  };
  ownerBeforeOrphan = reduce(ownerBeforeOrphan, {
    type: "event", event: event({
      type: "turn_binding", sid: ownerBeforeOrphanSid,
      msg_id: ownerBeforeOrphanMessage,
      turn_id: ownerBeforeOrphanNative, seq: 11,
    }),
  });
  assert.deepEqual(
    ownerBeforeOrphan.runtimes[ownerBeforeOrphanSid].liveDetailTurnIds,
    [ownerBeforeOrphanMessage],
    "detail ownership follows the surviving user row when it precedes the orphan",
  );

  // Cache migrations can leave two legitimate rows with the same presentation
  // id but distinct canonical aliases. Runtime bookkeeping must follow the
  // exact removed object, not infer deletion from that colliding display id.
  const collidingOrphanSid = "compact-orphan-colliding-display-id";
  const collidingDisplayId = "shared-optimistic-display";
  const collidingOwnerMessage = "colliding-owner-message";
  const collidingNative = "colliding-compaction-native";
  let collidingOrphanState = {
    ...initialState,
    focusedSid: collidingOrphanSid,
    runtimes: {
      [collidingOrphanSid]: {
        ...createRuntime(),
        state: "running" as const,
        legacyLiveFallbackBlocked: true,
        turns: [{
          id: collidingDisplayId,
          historyTurnId: "legitimate-canonical-row",
          prompt: "unrelated legitimate row",
          blocks: [],
          done: true,
        }, {
          id: collidingOwnerMessage,
          clientMsgId: collidingOwnerMessage,
          prompt: "continue",
          blocks: [],
          done: false,
        }, {
          id: collidingDisplayId,
          prompt: "",
          blocks: [{ ...orphanCompactionBlock,
            item_id: "colliding-compaction-item",
            turn_id: collidingNative }],
          done: false,
        }],
        hydratedCacheTurnIds: [collidingDisplayId],
        liveDetailTurnIds: [collidingDisplayId],
        liveOwner: { turnId: collidingDisplayId, seq: 29 },
      },
    },
  };
  collidingOrphanState = reduce(collidingOrphanState, {
    type: "event", event: event({
      type: "turn_binding", sid: collidingOrphanSid,
      msg_id: collidingOwnerMessage, turn_id: collidingNative, seq: 30,
    }),
  });
  assert.deepEqual(
    collidingOrphanState.runtimes[collidingOrphanSid].turns.map(
      (turn: Turn) => [turn.id, turn.historyTurnId, turn.prompt]),
    [
      [collidingDisplayId, "legitimate-canonical-row", "unrelated legitimate row"],
      [collidingOwnerMessage, undefined, "continue"],
    ],
    "orphan reconciliation preserves a legitimate row with a colliding display id",
  );
  assert.deepEqual(
    collidingOrphanState.runtimes[collidingOrphanSid].hydratedCacheTurnIds,
    [collidingDisplayId, collidingOwnerMessage],
    "a colliding retained cache id stays valid while orphan provenance transfers",
  );
  assert.deepEqual(
    collidingOrphanState.runtimes[collidingOrphanSid].liveDetailTurnIds,
    [collidingDisplayId, collidingOwnerMessage],
    "a colliding retained id stays observed while exact orphan detail transfers",
  );
  assert.deepEqual(
    collidingOrphanState.runtimes[collidingOrphanSid].liveOwner,
    { turnId: collidingOwnerMessage, seq: 30 },
    "live ownership follows the exact removed orphan despite a display-id collision",
  );

  const completedBeforeBindingSid = "compact-orphan-completed-before-binding";
  const completedBeforeBindingMessage =
    "compact-orphan-completed-before-binding-message";
  const completedBeforeBindingNative =
    "compact-orphan-completed-before-binding-native";
  let completedBeforeBinding = {
    ...initialState,
    focusedSid: completedBeforeBindingSid,
    runtimes: {
      [completedBeforeBindingSid]: {
        ...createRuntime(), state: "running" as const,
      },
    },
  };
  for (const completedEvent of [
    orphanRaceProcess(completedBeforeBindingSid, completedBeforeBindingNative),
    event({ type: "turn_end", sid: completedBeforeBindingSid,
      turn_id: completedBeforeBindingNative, seq: 11,
      result: { subtype: "success", duration_ms: 700, is_error: false } }),
    event({ type: "user_msg", sid: completedBeforeBindingSid,
      msg_id: completedBeforeBindingMessage,
      prompt: "继续修复问题", seq: 12 }),
    event({ type: "turn_binding", sid: completedBeforeBindingSid,
      msg_id: completedBeforeBindingMessage,
      turn_id: completedBeforeBindingNative, seq: 13 }),
  ]) {
    completedBeforeBinding = reduce(completedBeforeBinding, {
      type: "event", event: completedEvent,
    });
  }
  const completedBeforeBindingTurns =
    completedBeforeBinding.runtimes[completedBeforeBindingSid].turns;
  assert.equal(completedBeforeBindingTurns.length, 1,
    "a TurnEnd which wins the race still cannot strand a compact-only row");
  assert.equal(completedBeforeBindingTurns[0].id,
    completedBeforeBindingMessage);
  assert.equal(completedBeforeBindingTurns[0].done, true,
    "the existing exact terminal binding remains authoritative after reconciliation");
  assert.equal(completedBeforeBindingTurns[0].historyTurnId,
    completedBeforeBindingNative,
    "the completed native shell remains the History/detail lookup authority");

  const terminalOrphanSid = "compact-orphan-after-final";
  const terminalOrphanMessage = "compact-orphan-after-final-message";
  const terminalOrphanNative = "compact-orphan-after-final-native";
  let terminalOrphanState = {
    ...initialState,
    focusedSid: terminalOrphanSid,
    runtimes: {
      [terminalOrphanSid]: {
        ...createRuntime(),
        state: "running" as const,
        liveDetailTurnIds: [terminalOrphanNative],
      },
    },
  };
  for (const terminalEvent of [
    orphanRaceProcess(terminalOrphanSid, terminalOrphanNative),
    event({ type: "assistant_msg_start", sid: terminalOrphanSid,
      message_id: "terminal-orphan-final", channel: "final", seq: 11 }),
    event({ type: "delta", sid: terminalOrphanSid,
      message_id: "terminal-orphan-final", channel: "final",
      text: "已经完成", seq: 12 }),
    event({ type: "assistant_msg_end", sid: terminalOrphanSid,
      message_id: "terminal-orphan-final", channel: "final", seq: 13 }),
    event({ type: "turn_end", sid: terminalOrphanSid,
      turn_id: terminalOrphanNative, seq: 14,
      result: { subtype: "success", duration_ms: 800, is_error: false } }),
    event({ type: "user_msg", sid: terminalOrphanSid,
      msg_id: terminalOrphanMessage, prompt: "继续修复问题", seq: 15 }),
    event({ type: "turn_binding", sid: terminalOrphanSid,
      msg_id: terminalOrphanMessage, turn_id: terminalOrphanNative, seq: 16 }),
    event({ type: "turn_binding", sid: terminalOrphanSid,
      msg_id: terminalOrphanMessage, turn_id: terminalOrphanNative, seq: 16 }),
  ]) {
    terminalOrphanState = reduce(terminalOrphanState, {
      type: "event", event: terminalEvent,
    });
  }
  const terminalOrphanTurns =
    terminalOrphanState.runtimes[terminalOrphanSid].turns;
  assert.equal(terminalOrphanTurns.length, 1,
    "the existing exact binding path may join the user and assistant rows once");
  assert.equal(terminalOrphanTurns[0].prompt, "继续修复问题");
  assert.equal(terminalOrphanTurns.filter((turn: Turn) =>
    !turn.prompt && turn.blocks.every((block) => block.kind === "process"
      && block.processKind === "compaction")).length, 0,
  "final plus repeated binding leaves no standalone compaction-only row");
  assert.equal(terminalOrphanTurns.some((turn: Turn) => turn.blocks.some(
    (block) => block.kind === "text" && block.text === "已经完成")), true,
  "ordinary promptless assistant content remains visible");

  const ambiguousRaceSid = "compact-orphan-ambiguous-live";
  const ambiguousRaceNative = "compact-orphan-ambiguous-native";
  let ambiguousRaceState = {
    ...initialState,
    focusedSid: ambiguousRaceSid,
    runtimes: {
      [ambiguousRaceSid]: {
        ...createRuntime(), state: "running" as const,
        turns: [{
          id: "ambiguous-orphan-one", prompt: "", done: false,
          blocks: [{ ...orphanCompactionBlock,
            item_id: "ambiguous-live-one", turn_id: ambiguousRaceNative }],
        }, {
          id: "ambiguous-orphan-two", prompt: "", done: false,
          blocks: [{ ...orphanCompactionBlock,
            item_id: "ambiguous-live-two", turn_id: ambiguousRaceNative }],
        }],
      },
    },
  };
  ambiguousRaceState = reduce(ambiguousRaceState, {
    type: "event", event: event({ type: "user_msg", sid: ambiguousRaceSid,
      msg_id: "ambiguous-live-user", prompt: "continue", seq: 20 }),
  });
  ambiguousRaceState = reduce(ambiguousRaceState, {
    type: "event", event: event({ type: "turn_binding", sid: ambiguousRaceSid,
      msg_id: "ambiguous-live-user", turn_id: ambiguousRaceNative, seq: 21 }),
  });
  assert.equal(ambiguousRaceState.runtimes[ambiguousRaceSid].turns.length, 3,
    "the reducer keeps every row when more than one compact orphan qualifies");
  };
  testCompactionReducerRaces();

  const fallbackHistory = reduce({
    ...liveHistory,
    runtimes: { [liveHistorySid]: {
      ...liveHistory.runtimes[liveHistorySid], state: "interrupting" as const,
    } },
  }, { type: "event", event: event({
    ...liveHistoryFrame, build_seq: 2, in_progress: undefined,
  }) });
  assert.equal(fallbackHistory.runtimes[liveHistorySid].state, "interrupting");
  assert.equal(fallbackHistory.runtimes[liveHistorySid].turns[0].done, false);

  // A History read started before a newer live frame is not current control
  // state, even when it carries in_progress=false.
  const racedIdle = reduce(liveHistory, { type: "event", event: event({
    ...liveHistoryFrame, build_seq: 2, live_seq: 50, in_progress: false,
  }) });
  assert.equal(racedIdle.runtimes[liveHistorySid].state, "running");
  assert.equal(racedIdle.runtimes[liveHistorySid].turns[0].done, false);

  // A reconnect can receive a lightweight live setting frame before the
  // transcript read completes. That makes the History narrative stale, but an
  // explicit wrapper-owned in_progress=true is still a safe positive running
  // signal. Keeping the cache-hydrated idle state here makes the composer send
  // Query instead of interrupt + replacement, which the busy wrapper rejects.
  const racedRunningSid = "history-running-positive-race";
  let racedRunning = {
    ...initialState, focusedSid: racedRunningSid,
    runtimes: { [racedRunningSid]: createRuntime() },
  };
  racedRunning = reduce(racedRunning, { type: "event", event: event({
    type: "model", sid: racedRunningSid, seq: 81, model: "gpt-5",
  }) });
  racedRunning = reduce(racedRunning, { type: "event", event: event({
    type: "history", sid: racedRunningSid, session_id: racedRunningSid,
    revision: "running-positive-rev", build_seq: 1, live_seq: 80,
    external: false, in_progress: true, has_more: false, events: [],
  }) });
  assert.equal(racedRunning.runtimes[racedRunningSid].state, "running",
    "a stale narrative History must still recover positive wrapper activity");
  assert.equal(racedRunning.runtimes[racedRunningSid].mirroredRunning, false);

  let newerIdle = {
    ...initialState, focusedSid: racedRunningSid,
    runtimes: { [racedRunningSid]: createRuntime() },
  };
  newerIdle = reduce(newerIdle, { type: "event", event: event({
    type: "state", sid: racedRunningSid, seq: 82, state: "idle",
  }) });
  newerIdle = reduce(newerIdle, { type: "event", event: event({
    type: "history", sid: racedRunningSid, session_id: racedRunningSid,
    revision: "running-positive-rev", build_seq: 2, live_seq: 81,
    external: false, in_progress: true, has_more: false, events: [],
  }) });
  assert.equal(newerIdle.runtimes[racedRunningSid].state, "idle",
    "older positive History must not overwrite a newer terminal lifecycle");

  // Ownership is control state too. A History build which started before a
  // newer live state frame must never resurrect a stale terminal lock (the
  // /review false-positive), nor clear a newer takeover/ownership decision.
  const ownershipSid = "history-ownership-race";
  let ownershipRace = {
    ...initialState, focusedSid: ownershipSid,
    runtimes: { [ownershipSid]: createRuntime() },
  };
  ownershipRace = reduce(ownershipRace, { type: "event", event: event({
    type: "state", sid: ownershipSid, seq: 80, state: "running",
  }) });
  assert.equal(ownershipRace.runtimes[ownershipSid].lastLiveSeq, 80,
    "non-narrative live state must advance the History race watermark");
  ownershipRace = reduce(ownershipRace, { type: "event", event: event({
    type: "history", sid: ownershipSid, session_id: ownershipSid,
    revision: "ownership-rev", build_seq: 1, live_seq: 79,
    external: true, takeover_pending: true,
    in_progress: true, has_more: false, events: [],
  }) });
  assert.equal(Boolean(ownershipRace.runtimes[ownershipSid].external), false);
  assert.equal(ownershipRace.runtimes[ownershipSid].takeoverPending, false);

  ownershipRace = reduce(ownershipRace, { type: "event", event: event({
    type: "history", sid: ownershipSid, session_id: ownershipSid,
    revision: "ownership-rev", build_seq: 2, live_seq: 80,
    external: true, takeover_pending: true,
    in_progress: true, has_more: true, oldest_id: "cursor", events: [],
  }) });
  assert.equal(ownershipRace.runtimes[ownershipSid].external, true);
  assert.equal(ownershipRace.runtimes[ownershipSid].takeoverPending, true);
  ownershipRace = reduce(ownershipRace, { type: "event", event: event({
    type: "state", sid: ownershipSid, seq: 81, state: "running",
  }) });
  ownershipRace = reduce(ownershipRace, { type: "event", event: event({
    type: "history", sid: ownershipSid, session_id: ownershipSid,
    revision: "ownership-rev", build_seq: 3, live_seq: 80,
    external: false, takeover_pending: false,
    in_progress: false, has_more: true, oldest_id: "cursor", events: [],
  }) });
  assert.equal(ownershipRace.runtimes[ownershipSid].external, true);
  assert.equal(ownershipRace.runtimes[ownershipSid].takeoverPending, true);
  ownershipRace = reduce(ownershipRace, { type: "event", event: event({
    type: "history", sid: ownershipSid, session_id: ownershipSid,
    revision: "ownership-rev", build_seq: 3, before: "cursor",
    external: false, takeover_pending: false,
    has_more: false, events: [],
  }) });
  assert.equal(ownershipRace.runtimes[ownershipSid].external, true,
    "pagination cannot overwrite current ownership state");
  assert.equal(ownershipRace.runtimes[ownershipSid].takeoverPending, true);

  // Newest-page build ordering is independent from pagination. A late older
  // first page cannot replace build 2. Pagination remains revision/cursor based
  // because another client's targeted head build may advance the server counter
  // without ever being routed to this browser.
  const orderedHistorySid = "ordered-history";
  let orderedHistory = reduce({ ...initialState, focusedSid: orderedHistorySid }, {
    type: "event", event: event({
      type: "history", sid: orderedHistorySid, session_id: orderedHistorySid,
      revision: "ordered-rev-new", generation: "wrapper-one",
      build_seq: 2, has_more: true,
      oldest_id: "new", events: [
        event({ type: "user_msg", sid: orderedHistorySid,
          msg_id: "new", prompt: "new" }),
        event({ type: "turn_end", sid: orderedHistorySid,
          result: { subtype: "success", duration_ms: 1, is_error: false } }),
      ],
    }),
  });
  orderedHistory = reduce(orderedHistory, { type: "event", event: event({
    type: "history", sid: orderedHistorySid, session_id: orderedHistorySid,
    revision: "ordered-rev-old", generation: "wrapper-one",
    build_seq: 1, has_more: false,
    events: [event({ type: "user_msg", sid: orderedHistorySid,
      msg_id: "late-old-head", prompt: "old" })],
  }) });
  assert.deepEqual(orderedHistory.runtimes[orderedHistorySid].turns.map(
    (turn: { id: string }) => turn.id), ["new"]);
  const orderedRuntimeBeforePage = structuredClone(
    orderedHistory.runtimes[orderedHistorySid]);
  // Uncorrelated protocol pages have no request-time scope/view authority and
  // therefore cannot touch either runtime or browse state.
  orderedHistory = reduce(orderedHistory, { type: "event", event: event({
    type: "history", sid: orderedHistorySid, session_id: orderedHistorySid,
    revision: "ordered-rev-new", generation: "wrapper-one",
    build_seq: 2, before: "new", has_more: false,
    events: [
      event({ type: "user_msg", sid: orderedHistorySid,
        msg_id: "older-page", prompt: "older" }),
      event({ type: "turn_end", sid: orderedHistorySid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.deepEqual(
    orderedHistory.runtimes[orderedHistorySid],
    orderedRuntimeBeforePage,
    "an uncorrelated before page must leave every live runtime field unchanged");
  assert.equal(orderedHistory.historyBrowse, null);
  orderedHistory = reduce(orderedHistory, {
    type: "begin_history_browse",
    sid: orderedHistorySid,
    scopeKey: "machine-a:code:codex",
    revision: "ordered-rev-new",
    generation: "wrapper-one",
    viewId: "ordered-view",
    basePageKey: "ordered-head",
  });
  assert.ok(orderedHistory.historyBrowse);
  orderedHistory = reduce(orderedHistory, {
    type: "install_history_browse_page",
    sid: orderedHistorySid,
    scopeKey: "machine-a:code:codex",
    revision: "ordered-rev-new",
    generation: "wrapper-one",
    viewId: "ordered-view",
    windowEpoch: orderedHistory.historyBrowse!.windowEpoch,
    before: "new",
    page: {
      pageKey: "ordered-older",
      turns: [{
        id: "older-page", prompt: "older", blocks: [], done: true,
      }],
      hasOlder: false,
      olderCursor: "older-page",
      newerPageKey: "ordered-head",
    },
  });
  assert.deepEqual(orderedHistory.runtimes[orderedHistorySid].turns.map(
    (turn: { id: string }) => turn.id), ["new"],
  "correlated pagination is display-only and never prepends into runtime");
  assert.deepEqual(orderedHistory.historyBrowse?.turns.map(
    (turn: { id: string }) => turn.id), ["older-page", "new"]);

  const testCrossPageCompactionRepair = () => {
  // A polluted completed projection can span the byte boundary between the
  // newest page and one older page. A shared native task does not prove that
  // the compact-only row is the same occurrence, so preserve both rows while
  // keeping page keys and cursors usable.
  const compactBrowseSid = "compact-orphan-across-browse-pages";
  const compactBrowseNative = "compact-browse-native";
  const compactBrowseOwner: Turn = {
    id: "compact-browse-owner",
    forkPointId: compactBrowseNative,
    prompt: "continue long work",
    done: true,
    blocks: [{ ...orphanCompactionBlock,
      item_id: "compact-browse-canonical", turn_id: compactBrowseNative }],
  };
  let compactBrowse = reduce({
    ...initialState,
    focusedSid: compactBrowseSid,
    runtimes: {
      [compactBrowseSid]: {
        ...createRuntime(),
        turns: [compactBrowseOwner],
        historyRevision: "compact-browse-rev",
        historyGeneration: "compact-browse-generation",
        hasMore: true,
        oldestId: "compact-browse-cursor",
      },
    },
  }, {
    type: "begin_history_browse",
    sid: compactBrowseSid,
    scopeKey: "machine-a:code:codex",
    revision: "compact-browse-rev",
    generation: "compact-browse-generation",
    viewId: "compact-browse-view",
    basePageKey: "compact-browse-head",
  });
  compactBrowse = reduce(compactBrowse, {
    type: "install_history_browse_page",
    sid: compactBrowseSid,
    scopeKey: "machine-a:code:codex",
    revision: "compact-browse-rev",
    generation: "compact-browse-generation",
    viewId: "compact-browse-view",
    windowEpoch: compactBrowse.historyBrowse!.windowEpoch,
    before: "compact-browse-cursor",
    page: {
      pageKey: "compact-browse-older",
      turns: [{
        ...completedCachedOrphan,
        id: "compact-browse-orphan",
        blocks: [{ ...orphanCompactionBlock,
          item_id: "compact-browse-stale", turn_id: compactBrowseNative }],
      }],
      hasOlder: false,
      olderCursor: "compact-browse-floor",
      newerPageKey: "compact-browse-head",
    },
  });
  assert.deepEqual(compactBrowse.historyBrowse?.turns.map(
    (turn: Turn) => turn.id), [
      "compact-browse-orphan", "compact-browse-owner",
    ],
  "older-page materialization preserves a distinct compaction occurrence");
  assert.deepEqual(compactBrowse.historyBrowse?.loadedPageKeys,
    ["compact-browse-older", "compact-browse-head"],
  "preserving the row does not erase page-link authority");
  assert.equal(compactBrowse.historyBrowse?.hasOlder, false);
  assert.equal(compactBrowse.historyBrowse?.olderCursor,
    "compact-browse-floor");
  assert.equal(compactBrowse.runtimes[compactBrowseSid].turns.length, 1,
    "browse repair remains display-only and does not mutate the live runtime");
  };
  testCrossPageCompactionRepair();

  orderedHistory = reduce(orderedHistory, { type: "event", event: event({
    type: "history", sid: orderedHistorySid, session_id: orderedHistorySid,
    revision: "ordered-rev-new", generation: "wrapper-one",
    build_seq: 1, before: "older-page",
    has_more: false, events: [
      event({ type: "user_msg", sid: orderedHistorySid,
        msg_id: "stale-page", prompt: "stale" }),
      event({ type: "turn_end", sid: orderedHistorySid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.deepEqual(orderedHistory.runtimes[orderedHistorySid].turns.map(
    (turn: { id: string }) => turn.id), ["new"]);
  assert.deepEqual(orderedHistory.historyBrowse?.turns.map(
    (turn: { id: string }) => turn.id), ["older-page", "new"],
  "a late uncorrelated page cannot extend the active browse epoch");

  const browseBeforeLive = orderedHistory.historyBrowse!;
  const runtimeBeforeLive = orderedHistory.runtimes[orderedHistorySid].turns;
  const browseAfterLive = reduce(orderedHistory, {
    type: "event",
    event: event({
      type: "state", sid: orderedHistorySid, seq: 91, state: "running",
    }),
  });
  assert.equal(browseAfterLive.historyBrowse?.turns, browseBeforeLive.turns,
    "live lifecycle state is owned by runtime and cannot rewrite browse rows");
  assert.deepEqual(browseAfterLive.runtimes[orderedHistorySid].turns,
    runtimeBeforeLive);
  const browseAfterDelta = reduce(browseAfterLive, {
    type: "event",
    event: event({
      type: "delta", sid: orderedHistorySid, seq: 92,
      message_id: "background-live", text: "new live output",
    }),
  });
  assert.equal(browseAfterDelta.historyBrowse?.latestDirty, true);
  assert.equal(browseAfterDelta.historyBrowse?.turns,
    browseAfterLive.historyBrowse?.turns,
    "live narrative marks the browse stale without appending into its window");

  const reconnectingBrowseSource = {
    ...orderedHistory,
    historyBrowse: {
      ...orderedHistory.historyBrowse!,
      hasOlder: true,
      olderCursor: "still-has-older",
    },
  };
  let reconnectingBrowse = reduce(reconnectingBrowseSource, {
    type: "conn", connState: "reconnecting",
  });
  assert.equal(reconnectingBrowse.historyBrowse, null,
    "disconnect revokes the active browse cursor immediately");
  assert.deepEqual(
    reconnectingBrowse.retainedHistoryBrowse?.turns.map(
      (turn: Turn) => turn.id),
    ["older-page", "new"],
    "disconnect keeps the already-painted browse window read-only");
  const reconnectingView = displayHistoryProjection(
    reconnectingBrowse.historyRecovery,
    orderedHistorySid,
    reconnectingBrowse.runtimes[orderedHistorySid],
    reconnectingBrowse.historyBrowse,
    reconnectingBrowse.retainedHistoryBrowse,
  );
  assert.equal(reconnectingView.hasMore, true);
  assert.equal(reconnectingView.pagingReady, false);
  assert.equal(reconnectingView.recovering, true);
  assert.equal(
    reconnectingView.scopeKey,
    orderedHistory.historyBrowse!.scopeKey,
    "read-only reconnect paint must preserve the exact virtualizer scope",
  );
  const retainedBeforeDelayedPage =
    reconnectingBrowse.retainedHistoryBrowse;
  reconnectingBrowse = reduce(reconnectingBrowse, {
    type: "install_history_browse_page",
    sid: orderedHistorySid,
    scopeKey: orderedHistory.historyBrowse!.scopeKey,
    revision: orderedHistory.historyBrowse!.revision,
    generation: orderedHistory.historyBrowse!.generation,
    viewId: orderedHistory.historyBrowse!.viewId,
    windowEpoch: orderedHistory.historyBrowse!.windowEpoch,
    before: "still-has-older",
    page: {
      pageKey: "old-socket-page",
      turns: [{
        id: "must-not-install", prompt: "stale", blocks: [], done: true,
      }],
      hasOlder: false,
      olderCursor: "must-not-install",
    },
  });
  assert.equal(
    reconnectingBrowse.retainedHistoryBrowse,
    retainedBeforeDelayedPage,
    "a delayed old-socket page cannot mutate the retained display snapshot");

  reconnectingBrowse = reduce(reconnectingBrowse, {
    type: "conn", connState: "connected",
  });
  reconnectingBrowse = reduce(reconnectingBrowse, {
    type: "event",
    event: event({
      type: "history", sid: orderedHistorySid,
      session_id: orderedHistorySid,
      revision: "ordered-rev-new", generation: "wrapper-one",
      build_seq: 3, has_more: true, oldest_id: "new",
      events: [event({
        type: "user_msg", sid: orderedHistorySid,
        msg_id: "new", prompt: "new",
      })],
    }),
  });
  assert.equal(reconnectingBrowse.retainedHistoryBrowse, null);
  assert.deepEqual(
    reconnectingBrowse.historyBrowse?.turns.map((turn: Turn) => turn.id),
    ["older-page", "new"],
    "same revision and wrapper generation reactivate the retained browse");
  assert.equal(reconnectingBrowse.historyBrowse?.latestDirty, true,
    "a newer head build marks the reactivated deep window stale");

  let restartedBrowse = reduce(reconnectingBrowseSource, {
    type: "conn", connState: "reconnecting",
  });
  restartedBrowse = reduce(restartedBrowse, {
    type: "conn", connState: "connected",
  });
  restartedBrowse = reduce(restartedBrowse, {
    type: "event",
    event: event({
      type: "history", sid: orderedHistorySid,
      session_id: orderedHistorySid,
      revision: "ordered-rev-after-restart", generation: "wrapper-two",
      build_seq: 1, has_more: true, oldest_id: "restart-head",
      events: [event({
        type: "user_msg", sid: orderedHistorySid,
        msg_id: "restart-head", prompt: "new generation",
      })],
    }),
  });
  assert.equal(restartedBrowse.retainedHistoryBrowse, null);
  assert.equal(restartedBrowse.historyBrowse, null,
    "a different generation atomically replaces rather than reuses old pages");
  assert.deepEqual(
    restartedBrowse.runtimes[orderedHistorySid].turns.map(
      (turn: Turn) => turn.id),
    ["restart-head"],
  );

  const cacheRaceBrowse = {
    ...browseBeforeLive,
    hasNewer: true,
    newerPageKey: "ordered-head",
    latestDirty: false,
  };
  let cacheRaceState = {
    ...orderedHistory,
    historyBrowse: cacheRaceBrowse,
  };
  let resolveCachedLatest!: (page: {
    pageKey: string;
    turns: Turn[];
    isLatest: boolean;
  }) => void;
  const deferredCachedLatest = new Promise<{
    pageKey: string;
    turns: Turn[];
    isLatest: boolean;
  }>((resolvePage) => {
    resolveCachedLatest = resolvePage;
  });
  const frozenCachedLatest = {
    sid: cacheRaceBrowse.sid,
    scopeKey: cacheRaceBrowse.scopeKey,
    revision: cacheRaceBrowse.revision,
    generation: cacheRaceBrowse.generation,
    viewId: cacheRaceBrowse.viewId,
    windowEpoch: cacheRaceBrowse.windowEpoch,
    pageKey: cacheRaceBrowse.newerPageKey!,
  };
  const deferredInstall = (async () => {
    const page = await deferredCachedLatest;
    const current = cacheRaceState.historyBrowse;
    if (!current || !acceptsCachedNewerPage(current, frozenCachedLatest)) {
      return "discarded" as const;
    }
    if (cachedLatestRequiresLiveRuntime(current, page)) {
      return "latest" as const;
    }
    return appendNewerPage(current, page, {
      expectedScopeKey: frozenCachedLatest.scopeKey,
      expectedViewId: frozenCachedLatest.viewId,
      expectedWindowEpoch: frozenCachedLatest.windowEpoch,
      expectedNewerPageKey: frozenCachedLatest.pageKey,
    }).projection;
  })();
  cacheRaceState = reduce(cacheRaceState, {
    type: "event",
    event: event({
      type: "delta", sid: orderedHistorySid, seq: 93,
      message_id: "cache-race-live", text: "arrived during IndexedDB read",
    }),
  });
  resolveCachedLatest({
    pageKey: "ordered-head",
    turns: runtimeBeforeLive,
    isLatest: true,
  });
  const deferredOutcome = await deferredInstall;
  assert.equal(deferredOutcome, "latest",
    "a live delta during the cache read must return to the live runtime");
  cacheRaceState = reduce(cacheRaceState, {
    type: "return_to_latest",
    sid: frozenCachedLatest.sid,
  });
  assert.equal(cacheRaceState.historyBrowse, null,
    "the explicit downward gesture must reveal the authoritative live tail");

  const browseAfterOtherSidSend = reduce({
    ...orderedHistory,
    runtimes: {
      ...orderedHistory.runtimes,
      "background-send": createRuntime(),
    },
  }, {
    type: "query_sent", sid: "background-send",
    prompt: "background", msg_id: "background-message", ts: 99,
  });
  assert.equal(browseAfterOtherSidSend.historyBrowse,
    orderedHistory.historyBrowse,
    "a background or /btw send cannot close the focused parent browse");

  const successfulQueuedBrowse = reduce(orderedHistory, {
    type: "enqueue", sid: orderedHistorySid, query: { prompt: "later" },
  });
  assert.equal(successfulQueuedBrowse.historyBrowse, orderedHistory.historyBrowse,
    "a queued send preserves the current history reading view");
  const successfulPendingBrowse = reduce(orderedHistory, {
    type: "set_pending", sid: orderedHistorySid,
    query: { prompt: "interrupt after drain" },
  });
  assert.equal(successfulPendingBrowse.historyBrowse, orderedHistory.historyBrowse,
    "an interrupt-send preserves the current history reading view");
  assert.equal(
    successfulPendingBrowse.runtimes[orderedHistorySid].pendingSend?.prompt,
    "interrupt after drain");
  const fullQueue = Array.from({ length: 32 }, (_, index) => ({
    prompt: `queued-${index}`,
  }));
  const capacityState = {
    ...orderedHistory,
    runtimes: {
      ...orderedHistory.runtimes,
      [orderedHistorySid]: {
        ...orderedHistory.runtimes[orderedHistorySid],
        queue: fullQueue,
      },
    },
  };
  assert.equal(reduce(capacityState, {
    type: "enqueue", sid: orderedHistorySid,
    query: { prompt: "must fail capacity" },
  }), capacityState,
  "a rejected queue mutation must preserve the user's browse window");

  const directSendBrowse = reduce(orderedHistory, {
    type: "query_sent", sid: orderedHistorySid,
    prompt: "new live question", msg_id: "new-live-question", ts: 100,
  });
  assert.equal(directSendBrowse.historyBrowse, orderedHistory.historyBrowse,
    "sending installs live work without changing the reading viewport");
  assert.equal(
    directSendBrowse.runtimes[orderedHistorySid].turns.at(-1)?.id,
    "new-live-question",
    "the same reducer commit installs the optimistic turn without exiting browse");
  const steeredBrowse = reduce(orderedHistory, {
    type: "steer_sent", sid: orderedHistorySid,
    prompt: "supplement", msg_id: "supplement", ts: 101,
  });
  assert.equal(steeredBrowse.historyBrowse, orderedHistory.historyBrowse);

  const detailRequestedBrowse = reduce(orderedHistory, {
    type: "history_browse_detail_requested",
    sid: orderedHistorySid,
    scopeKey: orderedHistory.historyBrowse!.scopeKey,
    revision: orderedHistory.historyBrowse!.revision,
    viewId: orderedHistory.historyBrowse!.viewId,
    windowEpoch: orderedHistory.historyBrowse!.windowEpoch,
    turnId: "older-page",
  });
  assert.equal(detailRequestedBrowse.historyBrowse?.turns[0].detailLoading, true);
  const cancelledBrowseDetail = reduce(detailRequestedBrowse, {
    type: "history_detail_cancelled",
    context: {
      target: "browse",
      scopeKey: detailRequestedBrowse.historyBrowse!.scopeKey,
      sid: orderedHistorySid,
      revision: detailRequestedBrowse.historyBrowse!.revision,
      turnId: "older-page",
      viewId: detailRequestedBrowse.historyBrowse!.viewId,
      windowEpoch: detailRequestedBrowse.historyBrowse!.windowEpoch,
    },
  });
  assert.equal(cancelledBrowseDetail.historyBrowse?.turns.find(
    (turn: Turn) => turn.id === "older-page")?.detailLoading, false,
  "navigation/reconnect cancellation must make browse detail retryable");
  const detailRequestEpoch = detailRequestedBrowse.historyBrowse!.windowEpoch;
  const detailPagedBrowse = reduce(detailRequestedBrowse, {
    type: "install_history_browse_page",
    sid: orderedHistorySid,
    scopeKey: detailRequestedBrowse.historyBrowse!.scopeKey,
    revision: detailRequestedBrowse.historyBrowse!.revision,
    generation: detailRequestedBrowse.historyBrowse!.generation,
    viewId: detailRequestedBrowse.historyBrowse!.viewId,
    windowEpoch: detailRequestEpoch,
    before: detailRequestedBrowse.historyBrowse!.olderCursor!,
    page: {
      pageKey: "detail-older-page",
      turns: [{
        id: "ancient-detail-page", prompt: "ancient", blocks: [], done: true,
      }],
      hasOlder: false,
      olderCursor: "ancient-detail-page",
      newerPageKey: detailRequestedBrowse.historyBrowse!.oldestPageKey,
    },
  });
  assert.ok(detailPagedBrowse.historyBrowse!.windowEpoch > detailRequestEpoch);
  assert.equal(detailPagedBrowse.historyBrowse?.turns.find(
    (turn: Turn) => turn.id === "older-page")?.detailLoading, true,
  "pagination retains the requested row while advancing the display window");
  const runtimeBeforeBrowseDetail =
    detailPagedBrowse.runtimes[orderedHistorySid];
  const detailedBrowse = reduce(detailPagedBrowse, {
    type: "history_browse_detail",
    sid: orderedHistorySid,
    scopeKey: orderedHistory.historyBrowse!.scopeKey,
    revision: orderedHistory.historyBrowse!.revision,
    viewId: orderedHistory.historyBrowse!.viewId,
    // The response carries the request-time epoch. It remains authoritative
    // inside this same scope/revision/view while the canonical row still exists.
    windowEpoch: detailRequestEpoch,
    turnId: "older-page",
    events: [
      event({
        type: "user_msg", sid: orderedHistorySid,
        msg_id: "older-page", prompt: "older",
      }),
      event({
        type: "assistant_msg_start", sid: orderedHistorySid,
        message_id: "older-answer", channel: "final",
      }),
      event({
        type: "delta", sid: orderedHistorySid,
        message_id: "older-answer", text: "full older answer",
      }),
      event({
        type: "assistant_msg_end", sid: orderedHistorySid,
        message_id: "older-answer",
      }),
      event({
        type: "turn_end", sid: orderedHistorySid,
        result: { subtype: "success", duration_ms: 1, is_error: false },
      }),
    ],
  });
  assert.equal(detailedBrowse.runtimes[orderedHistorySid],
    runtimeBeforeBrowseDetail,
    "browse detail hydration cannot mutate the authoritative live runtime");
  const hydratedOlderTurn = detailedBrowse.historyBrowse?.turns.find(
    (turn: Turn) => turn.id === "older-page");
  assert.equal(hydratedOlderTurn?.detailLoaded, true);
  assert.equal(hydratedOlderTurn?.detailLoading, false);
  assert.equal(hydratedOlderTurn?.blocks[0]?.kind, "text");

  const browseOlderRequested = reduce(detailedBrowse, {
    type: "history_browse_detail_requested",
    sid: orderedHistorySid,
    scopeKey: detailedBrowse.historyBrowse!.scopeKey,
    revision: detailedBrowse.historyBrowse!.revision,
    viewId: detailedBrowse.historyBrowse!.viewId,
    windowEpoch: detailedBrowse.historyBrowse!.windowEpoch,
    turnId: "older-page",
    before: "browse-older-detail-cursor",
  });
  const browseOlderFailed = reduce(browseOlderRequested, {
    type: "history_browse_detail",
    sid: orderedHistorySid,
    scopeKey: detailedBrowse.historyBrowse!.scopeKey,
    revision: detailedBrowse.historyBrowse!.revision,
    viewId: detailedBrowse.historyBrowse!.viewId,
    windowEpoch: detailedBrowse.historyBrowse!.windowEpoch,
    turnId: "older-page",
    before: "browse-older-detail-cursor",
    events: [],
    error: "详细过程暂时不可用，请稍后重试",
  });
  const failedBrowseTurn = browseOlderFailed.historyBrowse?.turns.find(
    (turn: Turn) => turn.id === "older-page");
  assert.equal(failedBrowseTurn?.detailLoaded, true);
  assert.equal(failedBrowseTurn?.detailRetryBefore,
    "browse-older-detail-cursor");
  assert.equal(failedBrowseTurn?.detailRetryDirection, "older");

  const runtimeDetailRequested = reduce(orderedHistory, {
    type: "turn_detail_requested",
    sid: orderedHistorySid,
    turnId: "new",
  });
  assert.equal(
    runtimeDetailRequested.runtimes[orderedHistorySid].turns[0].detailLoading,
    true);
  const runtimeDetailCancelled = reduce(runtimeDetailRequested, {
    type: "history_detail_cancelled",
    context: {
      target: "runtime",
      scopeKey: orderedHistory.historyBrowse!.scopeKey,
      sid: orderedHistorySid,
      revision: "ordered-rev-new",
      turnId: "new",
    },
  });
  assert.equal(
    runtimeDetailCancelled.runtimes[orderedHistorySid].turns[0].detailLoading,
    false,
    "reconnect cancellation must make runtime detail retryable");

  const noNewerCache = reduce(detailedBrowse, {
    type: "history_browse_newer_unavailable",
    sid: orderedHistorySid,
    scopeKey: detailedBrowse.historyBrowse!.scopeKey,
    revision: detailedBrowse.historyBrowse!.revision,
    viewId: detailedBrowse.historyBrowse!.viewId,
    windowEpoch: detailedBrowse.historyBrowse!.windowEpoch,
  });
  assert.equal(noNewerCache.historyBrowse?.hasNewer, false);
  assert.equal(noNewerCache.historyBrowse?.newerPageKey, null);

  const invalidatedBrowse = reduce(orderedHistory, {
    type: "event",
    event: event({
      type: "history_invalidated", sid: orderedHistorySid,
      session_id: orderedHistorySid,
      revision: "ordered-rev-after-rollback", reason: "rollback",
    }),
  });
  assert.equal(invalidatedBrowse.historyBrowse, null);
  const delayedBrowsePage = reduce(invalidatedBrowse, {
    type: "install_history_browse_page",
    sid: orderedHistorySid,
    scopeKey: orderedHistory.historyBrowse!.scopeKey,
    revision: orderedHistory.historyBrowse!.revision,
    generation: "wrapper-one",
    viewId: orderedHistory.historyBrowse!.viewId,
    windowEpoch: orderedHistory.historyBrowse!.windowEpoch,
    before: orderedHistory.historyBrowse!.olderCursor!,
    page: {
      pageKey: "must-not-revive",
      turns: [{ id: "ancient", prompt: "ancient", blocks: [], done: true }],
      hasOlder: false,
      olderCursor: "ancient",
    },
  });
  assert.equal(delayedBrowsePage.historyBrowse, null,
    "a delayed pre-rollback page cannot revive a cleared browse projection");

  let abaBrowse = reduce({
    ...orderedHistory,
    runtimes: {
      ...orderedHistory.runtimes,
      "browse-session-b": createRuntime(),
    },
  }, { type: "focus_session", sid: "browse-session-b" });
  abaBrowse = reduce(abaBrowse, {
    type: "focus_session", sid: orderedHistorySid,
  });
  abaBrowse = reduce(abaBrowse, {
    type: "begin_history_browse",
    sid: orderedHistorySid,
    scopeKey: orderedHistory.historyBrowse!.scopeKey,
    revision: orderedHistory.historyBrowse!.revision,
    generation: orderedHistory.historyBrowse!.generation,
    viewId: "ordered-view-after-aba",
    basePageKey: "ordered-head-after-aba",
  });
  const abaBeforeDelayedPage = structuredClone(abaBrowse.historyBrowse);
  abaBrowse = reduce(abaBrowse, {
    type: "install_history_browse_page",
    sid: orderedHistorySid,
    scopeKey: orderedHistory.historyBrowse!.scopeKey,
    revision: orderedHistory.historyBrowse!.revision,
    generation: orderedHistory.historyBrowse!.generation,
    viewId: orderedHistory.historyBrowse!.viewId,
    windowEpoch: orderedHistory.historyBrowse!.windowEpoch,
    before: orderedHistory.historyBrowse!.olderCursor!,
    page: {
      pageKey: "delayed-old-view-page",
      turns: [{
        id: "must-not-enter-new-view", prompt: "stale", blocks: [], done: true,
      }],
      hasOlder: false,
      olderCursor: "must-not-enter-new-view",
    },
  });
  assert.deepEqual(abaBrowse.historyBrowse, abaBeforeDelayedPage,
    "A to B to A creates a new viewId which rejects A's delayed old page");

  assert.equal(reduce(orderedHistory, {
    type: "conn", connState: "reconnecting",
  }).historyBrowse, null,
  "a new socket generation revokes every frozen browse-page waiter");

  // A targeted newest-page refresh is only the moving head window. Once this
  // browser has explicitly paged backwards, that refresh must retain the older
  // pages and their floor cursor instead of collapsing the conversation back
  // to the handful of newest events.
  const pagedHeadSid = "paged-history-survives-head-refresh";
  let pagedHead = reduce({ ...initialState, focusedSid: pagedHeadSid }, {
    type: "event", event: event({
      type: "history", sid: pagedHeadSid, session_id: pagedHeadSid,
      revision: "paged-rev", generation: "wrapper-one", build_seq: 1,
      has_more: true, oldest_id: "head-cursor", events: [
        event({ type: "user_msg", sid: pagedHeadSid,
          msg_id: "head-one", prompt: "head one", ts: 30 }),
        event({ type: "turn_end", sid: pagedHeadSid,
          result: { subtype: "success", duration_ms: 1, is_error: false } }),
      ],
    }),
  });
  pagedHead = reduce(pagedHead, {
    type: "begin_history_browse",
    sid: pagedHeadSid,
    scopeKey: "machine-a:code:codex",
    revision: "paged-rev",
    generation: "wrapper-one",
    viewId: "paged-view",
    basePageKey: "paged-head",
  });
  pagedHead = reduce(pagedHead, {
    type: "install_history_browse_page",
    sid: pagedHeadSid,
    scopeKey: "machine-a:code:codex",
    revision: "paged-rev",
    generation: "wrapper-one",
    viewId: "paged-view",
    windowEpoch: pagedHead.historyBrowse!.windowEpoch,
    before: "head-cursor",
    page: {
      pageKey: "paged-older",
      turns: [{
        id: "older-one", prompt: "older one", blocks: [], done: true,
        ts: 10_000,
      }],
      hasOlder: false,
      olderCursor: "history-floor",
      newerPageKey: "paged-head",
    },
  });
  pagedHead = reduce(pagedHead, { type: "event", event: event({
    type: "history", sid: pagedHeadSid, session_id: pagedHeadSid,
    revision: "paged-rev", generation: "wrapper-one", build_seq: 2,
    has_more: true, oldest_id: "new-head-cursor", events: [
      event({ type: "user_msg", sid: pagedHeadSid,
        msg_id: "head-two", prompt: "head two", ts: 40 }),
      event({ type: "turn_end", sid: pagedHeadSid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.deepEqual(pagedHead.runtimes[pagedHeadSid].turns.map(
    (turn: { id: string }) => turn.id), ["head-one", "head-two"]);
  assert.deepEqual(pagedHead.historyBrowse?.turns.map(
    (turn: { id: string }) => turn.id), ["older-one", "head-one"],
  "a moving head refresh cannot rewrite the user's display-only reading window");
  assert.equal(pagedHead.historyBrowse?.hasOlder, false);
  assert.equal(pagedHead.historyBrowse?.olderCursor, "history-floor");
  assert.equal(pagedHead.historyBrowse?.latestDirty, true);

  // Learning browser/native message aliases changes the read revision, not
  // transcript contents. A tiny post-send head must retain already-painted
  // history, including the separate older-page reading lifetime.
  const testSendAliasContinuity = () => {
  const aliasSid = "send-alias-continuity";
  const aliasRows = Array.from({ length: 4 }, (_, i) => ({
    id: `alias-history-${i}`, prompt: `old prompt ${i}`, done: true,
    ts: 1000 + i * 1000, doneTs: 1500 + i * 1000,
    durationMs: 500,
    detailLoaded: true,
    blocks: [{ kind: "text", message_id: `answer-${i}`,
      text: `old answer ${i}`, done: true, channel: "final" }],
    fileChanges: { revision: `diff-${i}`, files: [{
      path: "src/main.py", state: "available", additions: 1, deletions: 0,
    }] },
  }));
  let aliasState = reduce({ ...initialState, focusedSid: aliasSid }, {
    type: "event", event: event({
      type: "history", sid: aliasSid, session_id: aliasSid,
      revision: "alias-0", continuity_revision: "alias-0",
      generation: "alias-generation", build_seq: 1, live_seq: 0,
      in_progress: false, detail: "summary", turns: aliasRows, events: [],
      has_more: true, oldest_id: "alias-history-0", newest_id: "alias-history-3",
    }),
  });
  const aliasOriginal = aliasState;
  aliasState = reduce(aliasState, {
    type: "query_sent", sid: aliasSid, msg_id: "new-browser-message", prompt: "new question", ts: 6000,
  });
  const aliasHead = event({
    type: "history", sid: aliasSid, session_id: aliasSid,
    revision: "alias-1", continuity_revision: "alias-0",
    generation: "alias-generation", build_seq: 2, live_seq: 1,
    in_progress: true, detail: "summary", events: [],
    turns: [{ id: "native-new", clientMsgId: "new-browser-message",
      prompt: "new question", done: false, blocks: [], ts: 6000 }],
    has_more: true, oldest_id: "native-new", newest_id: "native-new",
  });
  aliasState = reduce(aliasState, { type: "event", event: aliasHead });
  assert.equal(aliasState.runtimes[aliasSid].turns.length, 5,
    "post-send identity revision cannot replace all old messages with the newest prompt");
  assert.deepEqual(aliasState.runtimes[aliasSid].turns.slice(0, 4).map(
    (turn: Turn) => [turn.prompt, turn.durationMs, turn.fileChanges?.revision]),
  aliasRows.map((turn) => [turn.prompt, 500, turn.fileChanges.revision]));
  assert.equal(aliasState.runtimes[aliasSid].oldestId, "alias-history-0");
  assert.equal(displayHistoryProjection(null, aliasSid,
    aliasState.runtimes[aliasSid]).viewRevision, "alias-0",
  "an alias update keeps the scroll scope while actual read authority advances");
  assert.equal(aliasState.runtimes[aliasSid].historyRevision, "alias-1");

  let aliasBrowse = reduce(aliasOriginal, {
    type: "begin_history_browse", sid: aliasSid,
    scopeKey: "device:code:codex", revision: "alias-0",
    generation: "alias-generation", viewId: "reading-before-send", basePageKey: "head",
  });
  aliasBrowse = reduce(aliasBrowse, {
    type: "install_history_browse_page", sid: aliasSid,
    scopeKey: "device:code:codex", revision: "alias-0",
    generation: "alias-generation", viewId: "reading-before-send",
    windowEpoch: aliasBrowse.historyBrowse!.windowEpoch, before: "alias-history-0",
    page: { pageKey: "older", turns: [{ id: "older", prompt: "older prompt", blocks: [], done: true }],
      hasOlder: true, olderCursor: "older", newerPageKey: "head" },
  });
  const aliasViewBefore = aliasBrowse.historyBrowse!;
  aliasBrowse = reduce(aliasBrowse, { type: "event", event: aliasHead });
  assert.equal(aliasBrowse.historyBrowse?.turns, aliasViewBefore.turns);
  assert.equal(aliasBrowse.historyBrowse?.viewId, aliasViewBefore.viewId);
  assert.equal(aliasBrowse.historyBrowse?.revision, "alias-1");
  assert.equal(aliasBrowse.historyBrowse?.windowEpoch, aliasViewBefore.windowEpoch + 1);
  assert.equal(displayHistoryProjection(null, aliasSid,
    aliasBrowse.runtimes[aliasSid], aliasBrowse.historyBrowse).viewRevision, "alias-0");
  const rebasedAliasView = aliasBrowse.historyBrowse;
  aliasBrowse = reduce(aliasBrowse, {
    type: "install_history_browse_page", sid: aliasSid,
    scopeKey: aliasViewBefore.scopeKey, revision: aliasViewBefore.revision,
    generation: aliasViewBefore.generation, viewId: aliasViewBefore.viewId,
    windowEpoch: aliasViewBefore.windowEpoch, before: "older",
    page: { pageKey: "late-old-page", turns: [], hasOlder: false, olderCursor: null },
  });
  assert.equal(aliasBrowse.historyBrowse, rebasedAliasView,
    "a late response from before the alias update cannot close or rewrite the rebased view");
  aliasBrowse = reduce(aliasBrowse, { type: "event", event: {
    ...aliasHead, revision: "alias-2", build_seq: 3,
  } });
  assert.equal(aliasBrowse.historyBrowse?.turns, aliasViewBefore.turns,
    "continuity survives more than one additive alias revision");

  for (const invalidation of [
    { continuity_revision: "different-source" },
    { continuity_revision: undefined },
    { reset: true },
  ]) {
    const replaced = reduce(aliasState, { type: "event", event: {
      ...aliasHead, revision: "replacement", build_seq: 3, ...invalidation,
    } });
    assert.ok(!replaced.runtimes[aliasSid].turns.some(
      (turn: Turn) => turn.id === "alias-history-0"),
    "destructive/unknown revisions and rollback must still discard old completed rows");
  }
  };
  testSendAliasContinuity();

  // Even before the user explicitly paginates, a same-revision compact/head
  // refresh is only a suffix. It must not erase rows already rendered from the
  // live stream or the first bounded page.
  const compactHeadSid = "compact-head-keeps-painted-history";
  let compactHead = reduce({ ...initialState, focusedSid: compactHeadSid }, {
    type: "event", event: event({
      type: "history", sid: compactHeadSid, session_id: compactHeadSid,
      revision: "compact-rev", generation: "wrapper-one", build_seq: 1,
      has_more: true, oldest_id: "original-cursor", events: [
        event({ type: "user_msg", sid: compactHeadSid,
          msg_id: "long-turn", prompt: "original prompt", ts: 10 }),
        event({ type: "assistant_msg_start", sid: compactHeadSid,
          message_id: "before-compact", channel: "commentary" }),
        event({ type: "delta", sid: compactHeadSid,
          message_id: "before-compact", text: "before compact" }),
      ],
    }),
  });
  compactHead = reduce(compactHead, { type: "event", event: event({
    type: "history", sid: compactHeadSid, session_id: compactHeadSid,
    revision: "compact-rev", generation: "wrapper-one", build_seq: 2,
    has_more: true, oldest_id: "forced-tail-cursor", events: [
      event({ type: "user_msg", sid: compactHeadSid,
        msg_id: "long-turn", prompt: "original prompt", ts: 10 }),
      event({ type: "assistant_msg_start", sid: compactHeadSid,
        message_id: "after-compact", channel: "commentary" }),
      event({ type: "delta", sid: compactHeadSid,
        message_id: "after-compact", text: "after compact" }),
    ],
  }) });
  assert.equal(compactHead.runtimes[compactHeadSid].turns.length, 1);
  assert.equal(compactHead.runtimes[compactHeadSid].turns[0].prompt,
    "original prompt");
  assert.match(JSON.stringify(compactHead.runtimes[compactHeadSid].turns[0]),
    /before compact/);
  assert.match(JSON.stringify(compactHead.runtimes[compactHeadSid].turns[0]),
    /after compact/);
  assert.equal(compactHead.runtimes[compactHeadSid].oldestId,
    "original-cursor");

  // A newest page made entirely of genuine prompt-less background turns is
  // not evidence that the page is a byte-window suffix of the current user
  // turn. Repeated focus/history refreshes must keep those turns independent.
  const assistantPageSid = "assistant-page-does-not-attach-to-current";
  const assistantPageTurns = [
    {
      id: "background-one", prompt: "", done: true,
      blocks: [{
        kind: "text", message_id: "background-one-answer",
        text: "older background answer one", done: true, channel: "final",
      }],
      detailEventCount: 0, detailLoaded: false,
      ts: 4_000, doneTs: 5_000,
    },
    {
      id: "background-two", prompt: "", done: true,
      blocks: [{
        kind: "text", message_id: "background-two-answer",
        text: "older background answer two", done: true, channel: "final",
      }],
      detailEventCount: 0, detailLoaded: false,
      ts: 6_000, doneTs: 7_000,
    },
  ];
  let assistantPageState = reduce({
    ...initialState, focusedSid: assistantPageSid,
  }, { type: "event", event: event({
    type: "history", sid: assistantPageSid, session_id: assistantPageSid,
    revision: "assistant-page-rev", generation: "wrapper-one",
    build_seq: 1, live_seq: 0, has_more: true,
    oldest_id: "background-one", detail: "summary", events: [],
    turns: assistantPageTurns,
  }) });
  assistantPageState = reduce(assistantPageState, {
    type: "event", event: event({
      type: "user_msg", sid: assistantPageSid, seq: 1, ts: 9,
      msg_id: "current-user-turn", prompt: "current question",
    }),
  });
  assistantPageState = reduce(assistantPageState, {
    type: "event", event: event({
      type: "history", sid: assistantPageSid, session_id: assistantPageSid,
      revision: "assistant-page-rev", generation: "wrapper-one",
      build_seq: 2, live_seq: 1, has_more: true, in_progress: true,
      oldest_id: "background-one", detail: "summary", events: [],
      turns: assistantPageTurns,
    }),
  });
  assert.deepEqual(
    assistantPageState.runtimes[assistantPageSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["background-one", "background-two", "current-user-turn"],
  );
  const currentAssistantPageTurn =
    assistantPageState.runtimes[assistantPageSid].turns.at(-1);
  assert.equal(currentAssistantPageTurn?.blocks.length, 0);
  assert.doesNotMatch(
    JSON.stringify(currentAssistantPageTurn),
    /older background answer/,
    "prompt-less history rows must never be stitched into a user turn",
  );

  // A real wrapper restart owns a new generation, so its build counter may
  // legitimately start from one even when the previous generation reached two.
  orderedHistory = reduce(orderedHistory, { type: "event", event: event({
    type: "history", sid: orderedHistorySid, session_id: orderedHistorySid,
    revision: "restart-rev", generation: "wrapper-two",
    build_seq: 1, has_more: false, events: [
      event({ type: "user_msg", sid: orderedHistorySid,
        msg_id: "after-restart", prompt: "restart" }),
      event({ type: "turn_end", sid: orderedHistorySid,
        result: { subtype: "success", duration_ms: 1, is_error: false } }),
    ],
  }) });
  assert.deepEqual(orderedHistory.runtimes[orderedHistorySid].turns.map(
    (turn: { id: string }) => turn.id), ["after-restart"]);

  // Browser retention must not erase the backend's pagination cursor. A
  // pathological newest page can still exceed the canonical data window while
  // the server has older transcript windows available.
  const boundedCursorSid = "bounded-history-keeps-server-cursor";
  const manyHistoryEvents = Array.from(
    { length: MAX_RUNTIME_TURNS + 40 }, (_, index) => [
    event({ type: "user_msg", sid: boundedCursorSid,
      msg_id: `bounded-${index}`, prompt: `question ${index}` }),
    event({ type: "turn_end", sid: boundedCursorSid,
      result: { subtype: "success", duration_ms: 1, is_error: false } }),
  ]).flat();
  const boundedCursorState = reduce({
    ...initialState, focusedSid: boundedCursorSid,
  }, { type: "event", event: event({
    type: "history", sid: boundedCursorSid, session_id: boundedCursorSid,
    revision: "bounded-cursor-rev", generation: "wrapper-one",
    build_seq: 1, has_more: true, oldest_id: "server-byte-cursor",
    events: manyHistoryEvents,
  }) });
  assert.equal(
    boundedCursorState.runtimes[boundedCursorSid].turns.length,
    MAX_RUNTIME_TURNS);
  assert.equal(boundedCursorState.runtimes[boundedCursorSid].hasMore, true,
    "a full newest-biased runtime must keep server pagination available to the browse window");
  assert.equal(boundedCursorState.runtimes[boundedCursorSid].oldestId,
    "bounded-40",
    "local retention must page from the first retained native turn");
  assert.equal(boundedCursorState.runtimes[boundedCursorSid].truncated, true);

  const liveBoundSid = "live-bound-keeps-native-cursor";
  const liveBoundTurns = Array.from(
    { length: MAX_RUNTIME_TURNS + 1 },
    (_, index) => ({
      id: `optimistic-${index}`,
      historyTurnId: `native-${index}`,
      prompt: `question ${index}`,
      blocks: [],
      done: true,
    }),
  );
  const liveBoundState = reduce({
    ...initialState, focusedSid: liveBoundSid,
  }, {
    type: "set_turns", sid: liveBoundSid, turns: liveBoundTurns,
  });
  assert.equal(liveBoundState.runtimes[liveBoundSid].hasMore, true,
    "a locally full runtime must keep older history available to the browse window");
  assert.equal(
    liveBoundState.runtimes[liveBoundSid].oldestId,
    "native-1",
    "a local trim must expose the first retained authoritative history id");

  const failedHistorySid = "non-authoritative-history";
  const preservedTurn = {
    id: "preserved", prompt: "keep", blocks: [], done: true,
  };
  let failedHistory = {
    ...initialState, focusedSid: failedHistorySid,
    runtimes: { [failedHistorySid]: {
      ...createRuntime(), turns: [preservedTurn], loading: true,
      historyRevision: "known-good", historyBuildSeq: 9,
    } },
  };
  failedHistory = reduce(failedHistory, { type: "event", event: event({
    type: "history", sid: failedHistorySid, session_id: failedHistorySid,
    revision: "known-good", build_seq: 10, authoritative: false,
    error: "历史暂时不可用，请稍后重试", has_more: false, events: [],
  }) });
  assert.deepEqual(failedHistory.runtimes[failedHistorySid].turns,
    [preservedTurn]);
  assert.equal(failedHistory.runtimes[failedHistorySid].historyRevision,
    "known-good");
  assert.equal(failedHistory.runtimes[failedHistorySid].historyBuildSeq, 9);
  assert.equal(failedHistory.runtimes[failedHistorySid].loading, false);
  assert.equal(failedHistory.banner, undefined,
    "a recoverable history read must stay silent while preserving the projection");

  const coldPrefixSid = "cold-append-prefix-preview";
  const sampledPrefixTurn = {
    id: "sampled-prefix", prompt: "sampled stale prefix",
    blocks: [], done: true,
  };
  const coldPrefixState = reduce({
    ...initialState,
    focusedSid: coldPrefixSid,
    runtimes: {
      [coldPrefixSid]: { ...createRuntime(), loading: true },
    },
  }, {
    type: "event", event: event({
      type: "history", sid: coldPrefixSid, session_id: coldPrefixSid,
      revision: "prefix-revision", generation: "prefix-generation",
      build_seq: 11, authoritative: false, error: null,
      detail: "summary", turns: [sampledPrefixTurn],
      has_more: true, oldest_id: "sampled-prefix", events: [],
    }),
  });
  assert.deepEqual(coldPrefixState.runtimes[coldPrefixSid].turns, [],
    "a sampled append prefix must never enter the canonical runtime");
  assert.deepEqual(
    displayHistoryProjection(
      coldPrefixState.historyRecovery,
      coldPrefixSid,
      coldPrefixState.runtimes[coldPrefixSid],
    ).turns.map((turn: { id: string }) => turn.id),
    ["sampled-prefix"],
    "an empty cold first screen may use the sampled prefix as display-only preview");
  assert.equal(coldPrefixState.historyRecovery?.candidateBuildSeq, 11,
    "the sampled prefix build must seed confirmation without another exact scan");
  assert.equal(
    isRuntimeHistoryRecoveryPending(coldPrefixState.runtimes[coldPrefixSid]),
    false,
    "a sampled prefix preview is not a replay-gap runtime recovery");
  const coldExactHistory = event({
    type: "history", sid: coldPrefixSid, session_id: coldPrefixSid,
    revision: "exact-revision", generation: "prefix-generation",
    build_seq: 12, authoritative: true,
    detail: "summary", turns: [{
      id: "exact-current", prompt: "exact current history",
      blocks: [], done: true,
    }],
    has_more: false, events: [],
  }) as History;
  assert.equal(
    historyNeedsConfirmationRequest(
      coldPrefixState.runtimes[coldPrefixSid], coldExactHistory),
    false,
    "prefix recovery must not request a second potentially multi-GB exact scan");
  assert.equal(
    historyConfirmsRecovery(coldPrefixState.historyRecovery, coldExactHistory),
    true,
    "the first newer exact build must confirm a sampled prefix");
  const coldExactState = reduce(coldPrefixState, {
    type: "event", event: coldExactHistory,
  });
  assert.equal(coldExactState.historyRecovery?.turns, null);
  assert.deepEqual(
    coldExactState.runtimes[coldPrefixSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["exact-current"]);

  const trustedPrefixSid = "trusted-live-beats-append-prefix";
  const trustedLiveTurn = {
    id: "trusted-live", prompt: "current live question",
    blocks: [], done: false,
  };
  const trustedPrefixState = reduce({
    ...initialState,
    focusedSid: trustedPrefixSid,
    runtimes: {
      [trustedPrefixSid]: {
        ...createRuntime(),
        turns: [trustedLiveTurn],
        state: "running" as const,
        historyRevision: "trusted-revision",
        historyGeneration: "prefix-generation",
        historyBuildSeq: 10,
        lastLiveSeq: 42,
        loading: true,
      },
    },
  }, {
    type: "event", event: event({
      type: "history", sid: trustedPrefixSid, session_id: trustedPrefixSid,
      revision: "prefix-revision", generation: "prefix-generation",
      build_seq: 11, authoritative: false, error: null,
      detail: "summary", turns: [sampledPrefixTurn],
      has_more: true, oldest_id: "sampled-prefix", events: [],
    }),
  });
  assert.deepEqual(
    trustedPrefixState.runtimes[trustedPrefixSid].turns,
    [trustedLiveTurn],
    "a sampled append prefix must not mutate trusted live/current turns");
  assert.equal(
    isHistoryRecoveryPending(
      trustedPrefixState.historyRecovery, trustedPrefixSid),
    false,
    "a trusted base must not enter recovery or lock ordinary interactions");
  assert.equal(trustedPrefixState.runtimes[trustedPrefixSid].loading, false,
    "a stale prefix must not leave a trusted live runtime synchronizing");
  const trustedQueryAfterPrefix = reduce(trustedPrefixState, {
    type: "query_sent", sid: trustedPrefixSid,
    prompt: "continue from trusted head",
    msg_id: "trusted-query-after-prefix", ts: 123,
  });
  assert.deepEqual(
    trustedQueryAfterPrefix.runtimes[trustedPrefixSid]
      .acceptanceHistoryBaseline,
    {
      revision: "trusted-revision",
      generation: "prefix-generation",
      buildSeq: 10,
      liveSeq: 42,
      newestId: null,
    },
    "a sampled prefix must not hide the trusted acceptance watermark");

  // Even when the revision is unchanged, a first page is authoritative for
  // completed rows. This prevents generic stale-cache resurrection, not only
  // the explicit rollback-marker path.
  const sameRevisionSid = "same-revision-stale-idb";
  let sameRevisionState = reduce({
    ...initialState, focusedSid: sameRevisionSid,
  }, {
    type: "hydrate_cache", sid: sameRevisionSid, turns: staleTurns,
    revision: "boot-current:7",
  });
  sameRevisionState = reduce(sameRevisionState, {
    type: "event", event: event({
      type: "history", sid: sameRevisionSid, session_id: sameRevisionSid,
      revision: "boot-current:7", has_more: false,
      events: [
        event({ type: "user_msg", sid: sameRevisionSid,
          msg_id: "kept-before-rewind", prompt: "保留" }),
        event({ type: "turn_end", sid: sameRevisionSid,
          result: { subtype: "success", duration_ms: 1, is_error: false } }),
      ],
    }),
  });
  assert.deepEqual(sameRevisionState.runtimes[sameRevisionSid].turns.map(
    (turn: { id: string }) => turn.id), ["kept-before-rewind"]);

  // IndexedDB intentionally stores only the bounded visible projection, not
  // its pagination cursor.  When that instant paint races a same-authority
  // newest page, keeping the cached rows must not also keep the cache's
  // default `hasMore=false` / null cursor and hide older server history.
  const cachedPagingSid = "same-revision-cache-restores-pagination";
  let cachedPagingState = reduce({
    ...initialState, focusedSid: cachedPagingSid,
  }, {
    type: "hydrate_cache",
    sid: cachedPagingSid,
    revision: "cached-paging-rev",
    generation: "cached-paging-generation",
    turns: [{
      id: "cached-current", prompt: "cached current",
      blocks: [], done: true,
    }],
  });
  assert.equal(!!cachedPagingState.runtimes[cachedPagingSid].hasMore, false);
  assert.equal(cachedPagingState.runtimes[cachedPagingSid].oldestId ?? null, null);
  cachedPagingState = reduce(cachedPagingState, {
    type: "event", event: event({
      type: "history", sid: cachedPagingSid, session_id: cachedPagingSid,
      revision: "cached-paging-rev",
      generation: "cached-paging-generation",
      build_seq: 1,
      detail: "summary",
      has_more: true,
      oldest_id: "server-older-cursor",
      events: [],
      turns: [{
        id: "cached-current", prompt: "cached current",
        blocks: [], done: true,
      }],
    }),
  });
  assert.equal(cachedPagingState.runtimes[cachedPagingSid].hasMore, true,
    "an authoritative newest page must restore pagination after cache paint");
  assert.equal(cachedPagingState.runtimes[cachedPagingSid].oldestId,
    "server-older-cursor",
    "the load-older affordance must use the authoritative server cursor");
  const cachedPagingProjection = displayHistoryProjection(
    cachedPagingState.historyRecovery,
    cachedPagingSid,
    cachedPagingState.runtimes[cachedPagingSid],
  );
  assert.equal(cachedPagingProjection.hasMore, true);
  assert.equal(cachedPagingProjection.pagingReady, true);
  assert.equal(cachedPagingProjection.oldestId, "server-older-cursor");

  // When the tiny invalidation marker itself fell out of the ring, the replay
  // gap is still a conservative invalidation boundary. Never leave a stale
  // preview open or finish loading before authoritative History arrives.
  const gapSid = "replay-gap";
  let gapState = {
    ...initialState,
    focusedSid: gapSid,
    artifact: { file: "stale.ts", kind: "file" as const, sid: gapSid },
    runtimes: {
      [gapSid]: {
        ...createRuntime(), turns: staleTurns,
        historyRevision: "boot-old:9",
        historyGeneration: "gap-generation-new",
        historyBuildSeq: 4,
        hasMore: true,
        oldestId: staleTurns[0].id,
        syncReady: true,
      },
    },
  };
  gapState = reduce(gapState, { type: "event", event: event({
    type: "replay_start", sid: gapSid, from_seq: 20, to_seq: 30,
    truncated: true, rebuild: false, generation: "gap-generation-new",
  }) });
  assert.equal(gapState.artifact, null);
  assert.deepEqual(gapState.runtimes[gapSid].turns, []);
  assert.deepEqual(
    (gapState as typeof gapState & {
      historyRecovery?: { sid: string; turns: unknown[] | null };
    }).historyRecovery?.turns,
    staleTurns,
    "a focused replay gap must retain the last projection for display only");
  assert.equal(isHistoryRecoveryPending(gapState.historyRecovery, gapSid), true);
  assert.deepEqual(
    displayHistoryProjection(
      gapState.historyRecovery, gapSid, gapState.runtimes[gapSid],
    ).turns,
    staleTurns,
  );
  assert.equal(
    displayHistoryProjection(
      gapState.historyRecovery, gapSid, gapState.runtimes[gapSid],
      gapState.historyBrowse, gapState.retainedHistoryBrowse,
    ).hasMore,
    true,
    "known older history stays visible while its old cursor is read-only");
  assert.equal(
    displayHistoryProjection(
      gapState.historyRecovery, gapSid, gapState.runtimes[gapSid],
      gapState.historyBrowse, gapState.retainedHistoryBrowse,
    ).pagingReady,
    false,
    "a retained projection must never paginate with its old generation cursor");
  const queryDuringGapState = reduce(gapState, {
    type: "query_sent", sid: gapSid, prompt: "new live question",
    msg_id: "new-live-question", ts: 123,
  });
  assert.deepEqual(
    queryDuringGapState.runtimes[gapSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["new-live-question"],
    "display-only recovery turns must never enter the active query runtime");
  assert.equal(
    queryDuringGapState.runtimes[gapSid].acceptanceHistoryBaseline,
    null,
    "a query racing recovery must not freeze stale history watermarks");
  assert.deepEqual(
    (queryDuringGapState as typeof queryDuringGapState & {
      historyRecovery?: { turns: unknown[] | null };
    }).historyRecovery?.turns,
    staleTurns,
    "query_sent must not mutate the retained display-only projection");
  assert.equal(gapState.runtimes[gapSid].historyInvalidated, true);
  assert.equal(gapState.runtimes[gapSid].loading, true);
  const staleGenerationGapState = reduce(gapState, {
    type: "event", event: event({
      type: "history", sid: gapSid, session_id: gapSid,
      revision: "boot-stale:9", generation: "gap-generation-old",
      has_more: false, events: [],
    }),
  });
  assert.deepEqual(
    (staleGenerationGapState as typeof staleGenerationGapState & {
      historyRecovery?: { turns: unknown[] | null };
    }).historyRecovery?.turns,
    staleTurns,
    "an older wrapper generation cannot retire the retained display projection");
  assert.equal(
    staleGenerationGapState.runtimes[gapSid].historyInvalidated,
    true,
  );
  const staleBuildGapState = reduce(gapState, {
    type: "event", event: event({
      type: "history", sid: gapSid, session_id: gapSid,
      revision: "boot-old:9", generation: "gap-generation-new",
      build_seq: 3, has_more: false, events: [],
    }),
  });
  assert.equal(
    isHistoryRecoveryPending(staleBuildGapState.historyRecovery, gapSid),
    true,
    "a lower same-generation build cannot retire the recovery projection");
  const failedGapReadState = reduce(gapState, {
    type: "event", event: event({
      type: "history", sid: gapSid, session_id: gapSid,
      revision: "boot-new:9", generation: "gap-generation-new",
      authoritative: false, has_more: false, events: [],
    }),
  });
  assert.deepEqual(
    (failedGapReadState as typeof failedGapReadState & {
      historyRecovery?: { turns: unknown[] | null };
    }).historyRecovery?.turns,
    staleTurns,
    "a failed History read cannot replace the visible projection with an empty page");
  gapState = reduce(gapState, { type: "event", event: event({
    type: "replay_end", sid: gapSid, to_seq: 30, truncated: true,
  }) });
  assert.equal(gapState.runtimes[gapSid].loading, true,
    "ReplayEnd cannot satisfy a transcript gap");
  const recoveryBeforeCandidate = gapState.historyRecovery;
  const runtimeBeforeCandidate = gapState.runtimes[gapSid];
  const firstRecoveryCandidate = event({
    type: "history", sid: gapSid, session_id: gapSid,
    revision: "boot-new:9", generation: "gap-generation-new",
    build_seq: 5, has_more: false, detail: "summary",
    turns: [{
      id: "candidate-only", prompt: "must stay provisional",
      blocks: [], done: true,
    }],
    events: [],
  }) as History;
  assert.equal(
    historyNeedsConfirmationRequest(
      runtimeBeforeCandidate, firstRecoveryCandidate),
    true,
    "the first matching build must explicitly request a second newest page");
  assert.equal(
    historyNeedsConfirmationRequest(runtimeBeforeCandidate, event({
      ...firstRecoveryCandidate, generation: "gap-generation-old",
    }) as History),
    false,
    "an older generation cannot trigger or satisfy confirmation");
  assert.equal(
    historyNeedsConfirmationRequest(runtimeBeforeCandidate, event({
      ...firstRecoveryCandidate, generation: undefined,
    }) as History),
    false,
    "a generation-less response cannot trigger or satisfy confirmation");
  assert.equal(
    historyConfirmsRecovery(recoveryBeforeCandidate, firstRecoveryCandidate),
    false,
    "the first matching build is only a candidate and cannot clear cache");
  assert.equal(
    historyConfirmsRuntimeRecovery(
      runtimeBeforeCandidate, firstRecoveryCandidate),
    false,
    "the first matching build cannot clear the per-runtime gap barrier");
  let delayedCandidateRoundTripState = reduce(gapState, {
    type: "focus_session", sid: "delayed-candidate-other-session",
  });
  assert.equal(delayedCandidateRoundTripState.historyRecovery, null);
  delayedCandidateRoundTripState = reduce(delayedCandidateRoundTripState, {
    type: "event", event: firstRecoveryCandidate,
  });
  assert.deepEqual(
    delayedCandidateRoundTripState.runtimes[gapSid].turns,
    [],
    "a background first candidate must not install canonical narrative");
  assert.equal(
    delayedCandidateRoundTripState.runtimes[gapSid]
      .pendingHistoryCandidateBuildSeq,
    5,
    "the background runtime must retain the lightweight candidate watermark");
  assert.equal(
    isRuntimeHistoryRecoveryPending(
      delayedCandidateRoundTripState.runtimes[gapSid]),
    true,
    "clearing the focused display recovery must not clear the runtime barrier");
  delayedCandidateRoundTripState = reduce(delayedCandidateRoundTripState, {
    type: "focus_session", sid: gapSid,
  });
  assert.deepEqual(
    displayHistoryProjection(
      delayedCandidateRoundTripState.historyRecovery,
      gapSid,
      delayedCandidateRoundTripState.runtimes[gapSid],
    ).turns,
    [],
    "ReplayStart A to focus B to first History A to focus A stays empty/loading");
  assert.equal(
    delayedCandidateRoundTripState.runtimes[gapSid].historyInvalidated,
    true);
  gapState = reduce(gapState, {
    type: "event", event: firstRecoveryCandidate,
  });
  assert.deepEqual(gapState.runtimes[gapSid].turns, [],
    "the first candidate must not enter the canonical runtime");
  assert.equal(gapState.runtimes[gapSid].historyInvalidated, true,
    "the first candidate must not clear the invalidation barrier");
  assert.equal(gapState.runtimes[gapSid].pendingHistoryGeneration,
    "gap-generation-new",
    "the first candidate must keep the generation barrier");
  assert.equal(gapState.runtimes[gapSid].loading, true,
    "the first candidate must keep loading until confirmation");
  assert.equal(
    gapState.historyRecovery?.candidateBuildSeq,
    5,
    "the first matching build must remain pending as a candidate");
  assert.equal(
    (gapState as typeof gapState & {
      historyRecovery?: { turns: unknown[] | null; acceptedRevision?: string | null };
    }).historyRecovery?.turns,
    staleTurns,
    "the candidate must not replace the retained projection");
  let candidateRoundTripState = reduce(gapState, {
    type: "focus_session", sid: "candidate-other-session",
  });
  assert.equal(candidateRoundTripState.historyRecovery, null,
    "switching away releases only the display copy");
  candidateRoundTripState = reduce(candidateRoundTripState, {
    type: "focus_session", sid: gapSid,
  });
  assert.deepEqual(candidateRoundTripState.runtimes[gapSid].turns, [],
    "switching back before confirmation must not expose candidate narrative");
  assert.deepEqual(
    displayHistoryProjection(
      candidateRoundTripState.historyRecovery,
      gapSid,
      candidateRoundTripState.runtimes[gapSid],
    ).turns,
    [],
    "A to B to A must show an empty loading runtime, never gap-era history");
  assert.equal(
    candidateRoundTripState.runtimes[gapSid].historyInvalidated,
    true,
    "A to B to A must remain behind the cache invalidation barrier");
  const recoveryConfirmation = event({
    type: "history", sid: gapSid, session_id: gapSid,
    revision: "boot-new:9", generation: "gap-generation-new",
    build_seq: 6, has_more: false, detail: "summary",
    turns: [{
      id: "confirmed-current", prompt: "confirmed current history",
      blocks: [], done: true,
    }],
    events: [],
  }) as History;
  assert.equal(
    historyConfirmsRuntimeRecovery(
      gapState.runtimes[gapSid], recoveryConfirmation),
    true,
    "only a newer build from the bound generation clears the runtime barrier");
  assert.equal(
    historyConfirmsRecovery(gapState.historyRecovery, recoveryConfirmation),
    true,
    "only that newer build can also clear the focused cache/display barrier");
  gapState = reduce(gapState, {
    type: "event", event: recoveryConfirmation,
  });
  assert.equal(gapState.runtimes[gapSid].historyInvalidated, false);
  assert.equal(gapState.runtimes[gapSid].pendingHistoryGeneration, null);
  assert.equal(gapState.runtimes[gapSid].loading, false);
  assert.deepEqual(
    gapState.runtimes[gapSid].turns.map((turn: { id: string }) => turn.id),
    ["confirmed-current"],
    "only the newer confirmation may install canonical narrative");
  assert.equal(gapState.historyRecovery?.turns, null,
    "the explicit newer confirmation commits the projection atomically");
  assert.equal(
    (gapState as typeof gapState & {
      historyRecovery?: { acceptedRevision?: string | null };
    }).historyRecovery?.acceptedRevision,
    "boot-new:9",
  );
  assert.deepEqual(
    displayHistoryProjection(
      gapState.historyRecovery, gapSid, gapState.runtimes[gapSid],
    ).turns,
    gapState.runtimes[gapSid].turns,
    "after commit the selector must expose only the authoritative runtime");
  const laterRevisionState = reduce(gapState, {
    type: "event", event: event({
      type: "history", sid: gapSid, session_id: gapSid,
      revision: "boot-new:10", generation: "gap-generation-new",
      has_more: false, events: [],
    }),
  });
  assert.equal(laterRevisionState.historyRecovery, null,
    "a later independent revision must release the retained scroll scope");

  const backgroundGapSid = "background-replay-gap";
  let backgroundGapState = {
    ...initialState,
    focusedSid: gapSid,
    runtimes: {
      [gapSid]: { ...createRuntime(), turns: staleTurns },
      [backgroundGapSid]: { ...createRuntime(), turns: staleTurns },
    },
  };
  backgroundGapState = reduce(backgroundGapState, {
    type: "event", event: event({
      type: "replay_start", sid: backgroundGapSid,
      from_seq: 1, to_seq: 2, truncated: true,
      rebuild: false, generation: "background-generation",
    }),
  });
  assert.equal(
    (backgroundGapState as typeof backgroundGapState & {
      historyRecovery?: unknown;
    }).historyRecovery,
    null,
    "background replay gaps must not duplicate a full display projection");
  const staleBackgroundHistory = reduce(backgroundGapState, {
    type: "event", event: event({
      type: "history", sid: backgroundGapSid,
      session_id: backgroundGapSid, revision: "background-stale-revision",
      generation: "older-background-generation",
      has_more: false, events: [],
    }),
  });
  assert.equal(
    staleBackgroundHistory.runtimes[backgroundGapSid].historyInvalidated,
    true,
    "generation matching must also protect rebuilding background runtimes");
  const switchedAwayFromRecovery = reduce(failedGapReadState, {
    type: "focus_session", sid: backgroundGapSid,
  });
  assert.equal(switchedAwayFromRecovery.historyRecovery, null,
    "switching sessions must release the former visible projection");

  let rollbackDuringRecoveryState = {
    ...initialState,
    focusedSid: gapSid,
    runtimes: {
      [gapSid]: {
        ...createRuntime(), turns: staleTurns,
        historyRevision: "before-rollback",
      },
    },
  };
  rollbackDuringRecoveryState = reduce(rollbackDuringRecoveryState, {
    type: "event", event: event({
      type: "replay_start", sid: gapSid, from_seq: 10, to_seq: 20,
      truncated: true, rebuild: false, generation: "rollback-generation",
    }),
  });
  rollbackDuringRecoveryState = reduce(rollbackDuringRecoveryState, {
    type: "event", event: event({
      type: "history_invalidated", sid: gapSid, session_id: gapSid,
      revision: "after-rollback", reason: "rollback",
    }),
  });
  assert.deepEqual(rollbackDuringRecoveryState.runtimes[gapSid].turns, []);
  assert.equal(
    (rollbackDuringRecoveryState as typeof rollbackDuringRecoveryState & {
      historyRecovery?: unknown;
    }).historyRecovery,
    null,
    "rollback must clear both the rebuilding runtime and retained display");

  // A truncated live-tail replay can begin in the middle of an older turn.
  // Without its UserMsg/TurnEnd, that fragment looks like a new unfinished
  // assistant-only turn. The following authoritative History must discard the
  // unmatched fragment instead of sorting it after the real current turn.
  const replayFragmentSid = "replay-fragment-does-not-cross-history";
  let replayFragmentState = reduce({
    ...initialState, focusedSid: replayFragmentSid,
  }, { type: "event", event: event({
    type: "replay_start", sid: replayFragmentSid, from_seq: 70, to_seq: 74,
    truncated: true, rebuild: false, generation: "wrapper-one",
  }) });
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: event({
      type: "assistant_msg_start", sid: replayFragmentSid, seq: 71, ts: 7,
      message_id: "orphaned-old-commentary", channel: "commentary",
    }),
  });
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: event({
      type: "delta", sid: replayFragmentSid, seq: 72, ts: 7.1,
      message_id: "orphaned-old-commentary",
      text: "older reasoning replayed without its user boundary",
      channel: "commentary",
    }),
  });
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: event({
      type: "user_msg", sid: replayFragmentSid, seq: 73, ts: 9,
      msg_id: "current-turn", prompt: "current question",
    }),
  });
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: event({
      type: "assistant_msg_start", sid: replayFragmentSid, seq: 74, ts: 9.1,
      message_id: "current-commentary", channel: "commentary",
    }),
  });
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: event({
      type: "replay_end", sid: replayFragmentSid, to_seq: 74,
      truncated: true,
    }),
  });
  const replayFragmentCandidate = event({
    type: "history", sid: replayFragmentSid,
    session_id: replayFragmentSid, revision: "fragment-rev",
    generation: "wrapper-one", build_seq: 1, live_seq: 74,
    has_more: true, oldest_id: "authoritative-old",
    in_progress: true, detail: "summary", events: [],
    turns: [
      {
        id: "authoritative-old", prompt: "real older question",
        blocks: [], done: true, detailEventCount: 0,
        detailLoaded: false, ts: 5_000, doneTs: 6_000,
      },
      {
        id: "current-turn", prompt: "current question",
        blocks: [{
          kind: "text", message_id: "current-commentary",
          text: "current work", done: false, channel: "commentary",
        }],
        done: false, detailEventCount: 1,
        detailLoaded: false, ts: 9_000,
      },
    ],
  }) as History;
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: replayFragmentCandidate,
  });
  assert.equal(replayFragmentState.historyRecovery?.candidateBuildSeq, 1);
  replayFragmentState = reduce(replayFragmentState, {
    type: "event", event: event({
      ...replayFragmentCandidate, build_seq: 2,
    }) as History,
  });
  assert.deepEqual(
    replayFragmentState.runtimes[replayFragmentSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["authoritative-old", "current-turn"],
    "an unmatched replay prefix must not become the newest visible history",
  );
  assert.doesNotMatch(
    JSON.stringify(replayFragmentState.runtimes[replayFragmentSid].turns),
    /older reasoning replayed without its user boundary/,
  );

  // Command+R can reconnect after the current TurnBinding itself has fallen
  // out of the bounded ring. The wrapper still has its original sequence and
  // therefore seeds that exact owner before a retained suffix whose from_seq
  // is newer. History must merge the suffix into its canonical row rather
  // than rendering a second prompt-less running group.
  const boundSuffixSid = "truncated-current-suffix-keeps-one-owner";
  const boundSuffixMsg = "browser-current-message";
  const boundSuffixNativeTurn = "native-current-turn";
  let boundSuffixState = reduce({
    ...initialState, focusedSid: boundSuffixSid,
  }, { type: "event", event: event({
    type: "replay_start", sid: boundSuffixSid, from_seq: 101, to_seq: 103,
    truncated: true, rebuild: false, generation: "wrapper-one",
  }) });
  for (const replayEvent of [
    event({
      type: "turn_binding", sid: boundSuffixSid,
      msg_id: boundSuffixMsg, turn_id: boundSuffixNativeTurn,
    }),
    event({
      type: "assistant_msg_start", sid: boundSuffixSid, seq: 101, ts: 11,
      message_id: "current-live-commentary", channel: "commentary",
    }),
    event({
      type: "delta", sid: boundSuffixSid, seq: 102, ts: 11.1,
      message_id: "current-live-commentary",
      text: "one current replay suffix", channel: "commentary",
    }),
    event({
      type: "process", sid: boundSuffixSid, seq: 103, ts: 11.2,
      item_id: "current-live-command", kind: "command", phase: "end",
      status: "succeeded", turn_id: boundSuffixNativeTurn,
      title: "checked current tree",
    }),
    event({
      type: "replay_end", sid: boundSuffixSid, to_seq: 103,
      truncated: true,
    }),
  ]) {
    boundSuffixState = reduce(boundSuffixState, {
      type: "event", event: replayEvent,
    });
  }
  assert.equal(
    boundSuffixState.runtimes[boundSuffixSid].state,
    "idle",
    "an owner seed routes replay but does not manufacture lifecycle state",
  );
  assert.deepEqual(
    boundSuffixState.runtimes[boundSuffixSid].turns.map(
      (turn: Turn) => turn.id),
    [boundSuffixMsg],
    "the replay suffix attaches to the client-only owner seed immediately",
  );
  const boundSuffixHistory = event({
    type: "history", sid: boundSuffixSid,
    session_id: boundSuffixSid, revision: "bound-suffix-rev",
    generation: "wrapper-one", build_seq: 1, live_seq: 103,
    has_more: true, oldest_id: "official-current-item",
    newest_id: "official-current-item", in_progress: true,
    detail: "summary", events: [],
    turns: [{
      id: "official-current-item",
      clientMsgId: boundSuffixMsg,
      forkPointId: boundSuffixNativeTurn,
      prompt: "是否可以收敛 commit？",
      blocks: [{
        kind: "text", message_id: "official-current-commentary",
        text: "authoritative current summary", done: false,
        channel: "commentary",
      }],
      done: false, detailEventCount: 2, detailLoaded: false, ts: 10_000,
    }],
  }) as History;
  boundSuffixState = reduce(boundSuffixState, {
    type: "event", event: boundSuffixHistory,
  });
  boundSuffixState = reduce(boundSuffixState, {
    type: "event", event: event({
      ...boundSuffixHistory, build_seq: 2,
    }) as History,
  });
  assert.equal(
    boundSuffixState.runtimes[boundSuffixSid].turns.length,
    1,
    "a proven current replay suffix and authoritative History share one row",
  );
  const boundSuffixTurn = boundSuffixState.runtimes[boundSuffixSid].turns[0];
  assert.equal(boundSuffixTurn.clientMsgId, boundSuffixMsg);
  assert.equal(boundSuffixTurn.prompt, "是否可以收敛 commit？");
  assert.match(JSON.stringify(boundSuffixTurn), /one current replay suffix/);
  assert.match(JSON.stringify(boundSuffixTurn), /checked current tree/);

  // The exception above is deliberately limited to the wrapper's unsequenced
  // client-only seed. A normal sequenced binding replayed while idle can be
  // stale and must not claim later unbound narrative.
  const staleSequencedBindingSid = "sequenced-idle-binding-stays-closed";
  let staleSequencedBindingState = reduce({
    ...initialState, focusedSid: staleSequencedBindingSid,
    runtimes: { [staleSequencedBindingSid]: createRuntime() },
  }, { type: "event", event: event({
    type: "turn_binding", sid: staleSequencedBindingSid, seq: 40,
    msg_id: "completed-browser-message", turn_id: "completed-native-turn",
  }) });
  staleSequencedBindingState = reduce(staleSequencedBindingState, {
    type: "event", event: event({
      type: "assistant_msg_start", sid: staleSequencedBindingSid, seq: 41,
      message_id: "unbound-later-output", channel: "commentary",
    }),
  });
  assert.deepEqual(
    staleSequencedBindingState.runtimes[staleSequencedBindingSid].turns.map(
      (turn: Turn) => turn.id),
    ["unbound-later-output"],
    "a sequenced idle binding remains ineligible for narrative ownership",
  );

  // Starting the next browser turn is a lifecycle fence too. A delayed
  // binding from the completed turn must not become eligible merely because
  // the new turn changes the runtime back to running before its own binding.
  const nextQuerySid = "next-query-clears-idle-binding";
  let nextQueryState = reduce({
    ...initialState, focusedSid: nextQuerySid,
    runtimes: { [nextQuerySid]: {
      ...createRuntime(),
      turns: [{
        id: "completed-browser-message", prompt: "old prompt", blocks: [],
        done: true,
      }],
    } },
  }, { type: "event", event: event({
    type: "turn_binding", sid: nextQuerySid, seq: 40,
    msg_id: "completed-browser-message", turn_id: "completed-native-turn",
  }) });
  nextQueryState = reduce(nextQueryState, {
    type: "query_sent", sid: nextQuerySid, prompt: "new prompt",
    msg_id: "new-browser-message", ts: 10_000,
  });
  assert.equal(
    nextQueryState.runtimes[nextQuerySid].pendingLiveBinding,
    null,
    "a new local query discards a delayed binding from the idle lifecycle",
  );
  for (const nextEvent of [
    event({ type: "state", sid: nextQuerySid, seq: 41, state: "running" }),
    event({
      type: "assistant_msg_start", sid: nextQuerySid, seq: 42,
      message_id: "new-unbound-output", channel: "commentary",
    }),
    event({
      type: "delta", sid: nextQuerySid, seq: 43,
      message_id: "new-unbound-output", text: "new answer",
    }),
  ]) {
    nextQueryState = reduce(nextQueryState, {
      type: "event", event: nextEvent,
    });
  }
  const nextQueryTurns = nextQueryState.runtimes[nextQuerySid].turns;
  assert.deepEqual(
    nextQueryTurns.map((turn: Turn) => turn.id),
    ["completed-browser-message", "new-browser-message"],
  );
  assert.doesNotMatch(JSON.stringify(nextQueryTurns[0]), /new answer/);
  assert.match(JSON.stringify(nextQueryTurns[1]), /new answer/);

  // The inverse race is valid: the newest running UserMsg may have replayed
  // before its transcript row was flushed into History. Keep exactly that
  // newest unfinished tail while still removing an older orphaned prefix.
  const unflushedTailSid = "replay-gap-keeps-current-unflushed-tail";
  let unflushedTailState = reduce({
    ...initialState, focusedSid: unflushedTailSid,
  }, { type: "event", event: event({
    type: "replay_start", sid: unflushedTailSid, from_seq: 80, to_seq: 83,
    truncated: true, rebuild: false, generation: "wrapper-one",
  }) });
  for (const replayEvent of [
    event({
      type: "assistant_msg_start", sid: unflushedTailSid, seq: 81, ts: 7,
      message_id: "unflushed-orphan", channel: "commentary",
    }),
    event({
      type: "user_msg", sid: unflushedTailSid, seq: 82, ts: 10,
      msg_id: "unflushed-current", prompt: "just accepted",
    }),
    event({
      type: "assistant_msg_start", sid: unflushedTailSid, seq: 83, ts: 10.1,
      message_id: "unflushed-current-commentary", channel: "commentary",
    }),
  ]) {
    unflushedTailState = reduce(unflushedTailState, {
      type: "event", event: replayEvent,
    });
  }
  const unflushedCandidate = event({
    type: "history", sid: unflushedTailSid,
    session_id: unflushedTailSid, revision: "unflushed-rev",
    generation: "wrapper-one", build_seq: 1, live_seq: 83,
    has_more: true, in_progress: true, detail: "summary", events: [],
    turns: [{
      id: "last-flushed", prompt: "last flushed question",
      blocks: [], done: true, detailEventCount: 0,
      detailLoaded: false, ts: 5_000, doneTs: 6_000,
    }],
  }) as History;
  unflushedTailState = reduce(unflushedTailState, {
    type: "event", event: unflushedCandidate,
  });
  unflushedTailState = reduce(unflushedTailState, {
    type: "event", event: event({
      ...unflushedCandidate, build_seq: 2,
    }) as History,
  });
  assert.deepEqual(
    unflushedTailState.runtimes[unflushedTailSid].turns.map(
      (turn: { id: string }) => turn.id),
    ["last-flushed", "unflushed-current"],
  );
  assert.doesNotMatch(
    JSON.stringify(unflushedTailState.runtimes[unflushedTailSid].turns),
    /unflushed-orphan/,
  );

  const rebuildSeqSid = "wrapper-generation-rebuild";
  let rebuildSeqState = {
    ...initialState, focusedSid: rebuildSeqSid,
    runtimes: { [rebuildSeqSid]: {
      ...createRuntime(), historyBuildSeq: 8, lastLiveSeq: 200,
      lastLifecycleSeq: 199,
    } },
  };
  rebuildSeqState = reduce(rebuildSeqState, { type: "event", event: event({
    type: "replay_start", sid: rebuildSeqSid, from_seq: 1, to_seq: 2,
    truncated: false, rebuild: true,
  }) });
  assert.equal(rebuildSeqState.runtimes[rebuildSeqSid].historyBuildSeq, 0);
  assert.equal(rebuildSeqState.runtimes[rebuildSeqSid].lastLiveSeq, 0);
  assert.equal(rebuildSeqState.runtimes[rebuildSeqSid].lastLifecycleSeq, 0);

  state = { ...state, focusedSid: sid };
  // Claude has a static presentation catalog, but its explicit defaults come
  // from cwd-aware settings. An empty models array must still update them.
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: "claude-mythos-5-1[1m]", default_effort: "max",
    cwd: "~",
  }) });
  assert.equal(state.catalogDefault.claude, "claude-mythos-5-1");
  assert.equal(state.catalogDefaultEffort.claude, "max");
  const {
    compatibleNewChatEffort,
    newChatCatalogRequest,
    newChatEfforts,
    NewChatView,
    reconcileNewChatSelection,
    resolveNewChatLocalDefaults,
  } = await reducerHarness.ssrLoadModule(
    "/src/components/NewChatView.tsx");
  const { WorkArtifactsSheet } = await reducerHarness.ssrLoadModule(
    "/src/components/WorkArtifactsSheet.tsx");
  const {
    newWorkProfileForSidebarFilter,
    resolveWorkScheduleProfile,
  } = await reducerHarness.ssrLoadModule(
    "/src/work-profile-selection.ts");
  const { SessionsSidebar } = await reducerHarness.ssrLoadModule(
    "/src/components/SessionsSidebar.tsx");
  const { BtwPanel } = await reducerHarness.ssrLoadModule(
    "/src/components/BtwPanel.tsx");
  assert.deepEqual(
    newChatCatalogRequest("claude", "code", "/repo"),
    { engine: "claude", cwd: "/repo" },
    "Claude Code defaults must be resolved against the new session cwd",
  );
  assert.equal(
    newChatCatalogRequest("claude", "work", "/stale-code-cwd"),
    null,
    "Claude Work must not probe or inherit the Code cwd",
  );
  assert.deepEqual(
    newChatCatalogRequest("codex", "work", "/ignored", "stack"),
    { engine: "codex", codexProfileId: "stack" },
    "Codex Work reads the selected account without inventing a cwd scope",
  );
  assert.equal(
    newWorkProfileForSidebarFilter("codex", "work", "stack"),
    "stack",
    "a filtered Work list carries that account into the new-work form",
  );
  assert.equal(
    newWorkProfileForSidebarFilter("codex", "work", "all"),
    undefined,
    "the all-account Work view preserves the existing new-work preference",
  );
  assert.equal(
    newWorkProfileForSidebarFilter("codex", "code", "stack"),
    undefined,
    "the Work shortcut must not change Code's existing new-session behavior",
  );
  assert.deepEqual(resolveNewChatLocalDefaults(
    "claude", "code", "/repo",
    { claude: "claude-sonnet-5" },
    { claude: "high" },
    { claude: "/repo" },
  ), { model: "claude-sonnet-5", effort: "high" });
  assert.deepEqual(resolveNewChatLocalDefaults(
    "claude", "code", "/other",
    { claude: "claude-sonnet-5" },
    { claude: "high" },
    { claude: "/repo" },
  ), { model: null, effort: null },
  "a late Claude default for another cwd must not label this form");
  assert.deepEqual(resolveNewChatLocalDefaults(
    "claude", "work", "/repo",
    { claude: "claude-sonnet-5" },
    { claude: "high" },
    { claude: "/repo" },
  ), { model: null, effort: null },
  "Work ignores even a textually matching Code cwd default");

  const liveNewChatCatalog = {
    codex: [{
      id: "gpt-future",
      display_name: "GPT Future",
      description: "dynamic catalog model",
      efforts: ["low", "high"],
      default_effort: "low",
      is_default: true,
    }],
  };
  assert.equal(compatibleNewChatEffort(
    "codex", "gpt-future", "high", liveNewChatCatalog, null), "high");
  assert.equal(compatibleNewChatEffort(
    "codex", "gpt-future", "ultra", liveNewChatCatalog, null), null,
  "switching models clears an unsupported explicit effort");
  assert.equal(compatibleNewChatEffort(
    "claude", null, "high", {}, "custom-provider-model"), null,
  "an unknown local model cannot inherit a guessed effort capability");
  assert.equal(compatibleNewChatEffort(
    "codex", "gpt-future", null, liveNewChatCatalog, null), null,
  "switching models never auto-selects the highest effort");
  const zeroEffortCatalog = {
    codex: [{
      id: "gpt-no-effort",
      display_name: "GPT No Effort",
      description: "no reasoning override",
      efforts: [],
      default_effort: null,
      is_default: true,
    }],
  };
  assert.deepEqual(newChatEfforts(
    "codex", "gpt-no-effort", zeroEffortCatalog), [],
  "an authoritative empty effort list must not fall back to guessed levels");
  assert.deepEqual(newChatEfforts("codex", null, {}), [],
  "an unknown Codex default cannot expose unvalidated effort overrides");
  assert.deepEqual(reconcileNewChatSelection(
    "codex", "gpt-5.6-sol", "ultra", liveNewChatCatalog, "gpt-future",
  ), { model: null, effort: null },
  "an entitlement-filtered live catalog atomically clears a stale fallback selection");
  assert.deepEqual(reconcileNewChatSelection(
    "codex", "gpt-future", "high", liveNewChatCatalog, "gpt-future",
  ), { model: "gpt-future", effort: "high" },
  "an explicit selection that remains in the live catalog is preserved");

  const newChatMarkup = renderToStaticMarkup(createElement(NewChatView, {
    cwd: "~", engine: "claude",
    controlScopeKey: "machine-a:code:claude",
    model: null, effort: null,
    defaultModel: "claude-mythos-5-1", defaultEffort: "max",
    onPickModel: () => {}, onPickEffort: () => {},
    onPickCwd: () => {},
    onSend: () => true,
  }));
  const codexNewChatMarkup = renderToStaticMarkup(createElement(NewChatView, {
    cwd: "~", engine: "codex", catalog: liveNewChatCatalog,
    controlScopeKey: "machine-a:code:codex",
    model: null, effort: null,
    defaultModel: "gpt-future", defaultEffort: "low",
    onPickModel: () => {}, onPickEffort: () => {},
    onPickCwd: () => {},
    onSend: () => true,
  }));
  const multiProfileNewChatMarkup = renderToStaticMarkup(createElement(
    NewChatView,
    {
      cwd: "~", engine: "codex", catalog: liveNewChatCatalog,
      controlScopeKey: "machine-a:code:codex\0stack",
      model: null, effort: null,
      defaultModel: "gpt-future", defaultEffort: "low",
      codexProfiles: [
        { id: "primary", label: "Main" },
        { id: "stack", label: "Stack" },
      ],
      defaultCodexProfileId: "primary",
      codexProfileId: "stack",
      onPickCodexProfile: () => {},
      onPickModel: () => {}, onPickEffort: () => {},
      onPickCwd: () => {},
      onSend: () => true,
    },
  ));
  const multiProfileWorkMarkup = renderToStaticMarkup(createElement(
    NewChatView,
    {
      cwd: "~", engine: "codex", space: "work",
      catalog: liveNewChatCatalog,
      controlScopeKey: "machine-a:work:codex\0stack",
      model: null, effort: null,
      defaultModel: "gpt-future", defaultEffort: "low",
      codexProfiles: [
        { id: "primary", label: "Main" },
        { id: "stack", label: "Stack", error: "会话列表暂不可用" },
      ],
      defaultCodexProfileId: "primary",
      codexProfileId: "stack",
      onPickCodexProfile: () => {},
      onPickModel: () => {}, onPickEffort: () => {},
      onPickCwd: () => {},
      onSend: () => true,
    },
  ));
  const removedProfileWorkMarkup = renderToStaticMarkup(createElement(
    NewChatView,
    {
      cwd: "~", engine: "codex", space: "work",
      controlScopeKey: "machine-a:work:codex\0removed",
      codexProfiles: [{ id: "primary", label: "Main" }],
      defaultCodexProfileId: "primary",
      codexProfileId: "removed",
      onPickCodexProfile: () => {},
      onPickCwd: () => {},
      onSend: () => true,
    },
  ));
  for (const markup of [newChatMarkup, codexNewChatMarkup]) {
    assert.match(markup, /aria-label="添加照片"/);
    assert.match(markup, /aria-label="添加文件"/);
    assert.match(markup, /accept="image\/\*"/);
    assert.match(markup, /multiple=""/);
    assert.equal(
      (markup.match(/<button[^>]+aria-label="添加照片"/g) ?? []).length, 1);
    assert.equal(
      (markup.match(/<button[^>]+aria-label="添加文件"/g) ?? []).length, 0);
    assert.match(markup, />开始</);
    assert.match(markup, /title="选择模型"/);
    assert.match(markup, /title="选择思考强度"/);
    assert.match(markup, /本机默认/);
    assert.match(markup, /默认/);
  }
  assert.match(newChatMarkup, /本机默认 · Mythos 5\.1/);
  assert.match(newChatMarkup, /默认 · max/);
  assert.match(codexNewChatMarkup, /本机默认 · GPT Future/);
  assert.match(codexNewChatMarkup, /dynamic catalog model/,
    "new-session selectors render the live Codex catalog when available");
  const codexNewChatControls = codexNewChatMarkup.match(
    /<div class="newchat-ctls">([\s\S]*?)<\/div>/,
  )?.[1] ?? "";
  assert.doesNotMatch(codexNewChatControls, /不询问|Plan|标准/,
    "the compact footer must not duplicate controls that live in its sheet");
  assert.match(codexNewChatMarkup, /class="newchat-access"/);
  assert.match(codexNewChatMarkup, /权限与执行环境/);
  assert.match(codexNewChatMarkup, /Full Access/);
  assert.doesNotMatch(newChatMarkup, /class="newchat-access"/);
  for (const markup of [newChatMarkup, codexNewChatMarkup]) {
    assert.doesNotMatch(markup, /class="newchat-context"/,
      "single-account and Claude new-chat preserve the historical cwd DOM");
    assert.match(markup, /<\/div><button class="newchat-cwd"/,
      "the historical cwd button remains a direct new-chat-card child");
  }
  assert.doesNotMatch(codexNewChatMarkup, /aria-label="选择 Codex 账号"/,
    "single-account installs keep the historical new-chat layout");
  assert.match(layoutCss, /\.newchat-cwd\{[^}]*align-self:center/,
    "the historical cwd pill keeps its own centering rule");
  assert.match(multiProfileNewChatMarkup, /class="newchat-context"/);
  assert.match(multiProfileNewChatMarkup, /aria-label="选择 Codex 账号"/);
  assert.match(multiProfileNewChatMarkup, />nyx · Stack</);
  assert.match(multiProfileWorkMarkup, /aria-label="选择 Codex 账号"/);
  assert.match(multiProfileWorkMarkup, />nyx · Stack · 目录暂不可用</);
  assert.match(multiProfileWorkMarkup, /会话列表暂不可用/,
    "a transient catalog error warns without removing the Work account");
  assert.match(removedProfileWorkMarkup, />已移除账号</);
  assert.match(removedProfileWorkMarkup, /所选 Codex 账号已移除，请重新选择/);
  assert.deepEqual(
    resolveWorkScheduleProfile(
      [{ id: "primary", label: "Main" }], null, "removed"),
    { profileId: "removed", missing: true },
    "a removed schedule owner must require an explicit replacement",
  );
  const artifactsMarkup = renderToStaticMarkup(createElement(WorkArtifactsSheet, {
    open: true,
    artifacts: [
      { path: "report.md", size: 1024, modified_at: 1, kind: "document", previewable: true },
      { path: "slides/deck.pptx", size: 4096, modified_at: 2, kind: "presentation", previewable: true },
    ],
    onOpen: () => {},
    onClose: () => {},
  }));
  assert.match(artifactsMarkup, /当前工作产生的 2 个文件/);
  assert.match(artifactsMarkup, /report\.md/);
  assert.match(artifactsMarkup, /slides\/deck\.pptx/);
  assert.doesNotMatch(artifactsMarkup, /暂不可预览|disabled=""/);
  const sidebarProps = {
    open: true,
    engine: "codex" as const,
    space: "code" as const,
    profileScopeKey: "machine-a:codex:code",
    onSpaceChange: () => {},
    sessions: [
      { session_id: "done-main", summary: "Main", state: "idle" },
      { session_id: "done-btw", summary: "BTW", state: "idle" },
      { session_id: "running", summary: "Running", state: "running" },
    ],
    liveStates: { running: "running" as const },
    completionBadges: {
      "done-main": "main" as const,
      "done-btw": "btw" as const,
      running: "main" as const,
    },
    activeSessionId: null,
    onSelect: () => {},
    onNew: () => {},
    onNewInDir: () => {},
    onClose: () => {},
    onRename: () => {},
    onArchive: () => {},
    onPin: () => {},
    onDelete: () => {},
    onForkWorktree: () => {},
  };
  const nativeLocalStorage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  try {
    // Node 25 exposes Web Storage by default, while CI's Node 24 does not.
    // Do not let that host global mask a browser-only initialization dependency.
    Reflect.deleteProperty(globalThis, "localStorage");
    assert.doesNotThrow(() => renderToStaticMarkup(createElement(
      SessionsSidebar, sidebarProps)), "the sidebar renders without Web Storage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() { throw new DOMException("blocked", "SecurityError"); },
    });
    assert.doesNotThrow(() => renderToStaticMarkup(createElement(
      SessionsSidebar, sidebarProps)), "SSR must not read the host's Web Storage");
  } finally {
    if (nativeLocalStorage) Object.defineProperty(globalThis, "localStorage", nativeLocalStorage);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
  const completionSidebarMarkup = renderToStaticMarkup(createElement(
    SessionsSidebar, sidebarProps));
  assert.match(completionSidebarMarkup, />已完成</);
  assert.match(completionSidebarMarkup, />BTW 完成</);
  assert.equal(
    (completionSidebarMarkup.match(/class="pill completed"/g) ?? []).length,
    2,
    "a newly running turn must hide an older completion label",
  );
  ([
    ["idle", "both", "2 项完成"],
    ["running", "btw", "BTW 完成"],
    ["interrupting", "btw", "BTW 完成"],
    ["running", "both", "BTW 完成"],
    ["interrupting", "both", "BTW 完成"],
    ["running", "main", null],
    ["interrupting", "main", null],
  ] as const).forEach(([state, completion, label]) => {
    const markup = renderToStaticMarkup(createElement(SessionsSidebar, {
      ...sidebarProps,
      sessions: [{ session_id: "parent", summary: "Parent", state: "idle" }],
      liveStates: { parent: state },
      completionBadges: { parent: completion },
    }));
    assert.deepEqual(
      [...markup.matchAll(/class="pill completed"><span class="sd"><\/span>([^<]+)</g)]
        .map((match) => match[1]),
      label ? [label] : [],
      `${state} parent must show the appropriate ${completion} completion badge`,
    );
  });
  const profileSidebarMarkup = renderToStaticMarkup(createElement(
    SessionsSidebar,
    {
      ...sidebarProps,
      codexProfiles: [
        { id: "primary", label: "Main" },
        { id: "stack", label: "Stack" },
      ],
      defaultCodexProfileId: "primary",
      sessions: [
        {
          session_id: "primary@native-a", native_session_id: "native-a",
          codex_profile_id: "primary", codex_profile_label: "Main",
          summary: "Primary", state: "idle", engine: "codex", space: "code",
        },
        {
          session_id: "stack@native-b", native_session_id: "native-b",
          codex_profile_id: "stack", codex_profile_label: "Stack",
          summary: "Stack", state: "idle", engine: "codex", space: "code",
        },
      ],
    },
  ));
  assert.match(profileSidebarMarkup,
    /class="scard-profile-ribbon tone-0"[^>]*>default</);
  assert.match(profileSidebarMarkup,
    /class="scard-profile-ribbon tone-1"[^>]*>nyx</);
  assert.match(profileSidebarMarkup, /Codex 账号：nyx · Stack/);
  const workProfileSidebarMarkup = renderToStaticMarkup(createElement(
    SessionsSidebar,
    {
      ...sidebarProps,
      space: "work" as const,
      codexProfiles: [
        { id: "primary", label: "Main" },
        { id: "stack", label: "Stack" },
      ],
      defaultCodexProfileId: "primary",
      sessions: [
        {
          session_id: "primary@work-a", native_session_id: "work-a",
          codex_profile_id: "primary", codex_profile_label: "Main",
          summary: "Primary Work", state: "idle", engine: "codex", space: "work",
        },
        {
          session_id: "stack@work-b", native_session_id: "work-b",
          codex_profile_id: "stack", codex_profile_label: "Stack",
          summary: "Stack Work", state: "idle", engine: "codex", space: "work",
        },
      ],
    },
  ));
  assert.match(workProfileSidebarMarkup, /aria-label="筛选 Codex 账号"/);
  assert.match(workProfileSidebarMarkup,
    /class="scard-profile-ribbon tone-0"[^>]*>default</);
  assert.match(workProfileSidebarMarkup,
    /class="scard-profile-ribbon tone-1"[^>]*>nyx</);
  assert.match(workProfileSidebarMarkup, /Codex 账号：nyx · Stack/);
  const singleProfileSidebarMarkup = renderToStaticMarkup(createElement(
    SessionsSidebar,
    {
      ...sidebarProps,
      codexProfiles: oneCodexProfile,
      defaultCodexProfileId: "primary",
      sessions: [{
        session_id: "native-only", codex_profile_id: "primary",
        codex_profile_label: "Main", summary: "Only", state: "idle",
      }],
    },
  ));
  assert.doesNotMatch(singleProfileSidebarMarkup, /profile-ribbon|profile-filter/,
    "single-account sidebar output remains free of profile-only DOM");
  const singleProfileWorkSidebarMarkup = renderToStaticMarkup(createElement(
    SessionsSidebar,
    {
      ...sidebarProps,
      space: "work" as const,
      codexProfiles: oneCodexProfile,
      defaultCodexProfileId: "primary",
      sessions: [{
        session_id: "work-only", native_session_id: "work-only",
        codex_profile_id: "primary", codex_profile_label: "Main",
        summary: "Only Work", state: "idle", engine: "codex", space: "work",
      }],
    },
  ));
  assert.doesNotMatch(
    singleProfileWorkSidebarMarkup,
    /profile-ribbon|profile-filter/,
    "single-account Work keeps its historical sidebar DOM",
  );
  const btwDraftStore = new ComposerDraftStore();
  const btwPanelMarkup = renderToStaticMarkup(createElement(BtwPanel, {
    sid: "btw-render",
    chats: [{
      sid: "btw-render", engine: "codex", title: "侧聊 1", state: "running",
      needsAnswer: false,
    }],
    rt: {
      ...createRuntime(),
      state: "running",
      syncReady: true,
      model: "gpt-5.6-terra",
      effort: "high",
      queue: [{
        prompt: "queued follow-up",
        queueKind: "queue",
        queueState: "queued",
      }],
      pendingSend: {
        msg_id: "replace-render",
        prompt: "replacement follow-up",
        queueKind: "replace",
        queueState: "queued",
      },
      failedDeferred: [{
        msg_id: "failed-render",
        prompt: "retryable failed follow-up",
        queueKind: "queue",
        queueState: "failed",
        queueError: "submission rejected",
      }],
    },
    engine: "codex",
    opening: false,
    active: "btw",
    hasArtifact: false,
    catalog: {},
    draftKey: "btw-render-draft",
    draftStore: btwDraftStore,
    sendMode: "steer",
    unconfirmedQueued: [],
    unconfirmedReplaceable: [],
    queueCapacity: {},
    replaceQueueCapacity: {},
    onTab: () => {},
    onNew: () => {},
    onSelect: () => {},
    onCloseChat: () => {},
    onSend: () => true,
    onSteer: () => true,
    onInterrupt: () => {},
    onSetSendMode: () => {},
    onEnqueue: () => {},
    onSetPending: () => {},
    onRemoveQueued: () => {},
    onInspectQueued: () => {},
    onSetModel: () => {},
    onSetEffort: () => {},
    onSetAutoCompact: () => true,
    onAnswerQuestion: () => {},
    onOpenFile: () => {},
    onCollapse: () => {},
    onDismissNotice: () => {},
  }));
  assert.match(btwPanelMarkup, /GPT-5\.6 Terra/);
  assert.match(btwPanelMarkup, />high</);
  assert.match(btwPanelMarkup, /queued follow-up/);
  assert.match(btwPanelMarkup, /replacement follow-up/);
  assert.match(btwPanelMarkup, /retryable failed follow-up/);
  assert.match(btwPanelMarkup, />引导</);
  assert.match(btwPanelMarkup, />排队</);
  assert.match(btwPanelMarkup, />替换</);
  assert.match(btwPanelMarkup, />未发送</);
  assert.match(btwPanelMarkup, /aria-label="停止"/);
  assert.match(btwPanelMarkup, /aria-label="调整 BTW 面板宽度"/);
  assert.match(btwPanelMarkup, /data-lock-horizontal-swipe="true"/);
  state = { ...state,
    newChat: { cwd: "/other", model: null, effort: null } };
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: "claude-sonnet-5", default_effort: "high", cwd: "/other",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: "claude-mythos-5", default_effort: "max", cwd: "~",
  }) });
  assert.equal(state.catalogDefault.claude, "claude-sonnet-5");
  assert.equal(state.catalogDefaultEffort.claude, "high");
  assert.equal(state.catalogDefaultCwd.claude, "/other");
  state = reduce(state, { type: "event", event: event({
    type: "models", engine: "claude", models: [],
    default_model: null, default_effort: null, cwd: "/other",
  }) });
  assert.equal(state.catalogDefault.claude, undefined);
  assert.equal(state.catalogDefaultEffort.claude, undefined);
  assert.equal(state.catalogDefaultCwd.claude, "/other");
  // The newest history page restores both authoritative settings. An older
  // pagination page cannot roll either one back, even if a stale server includes
  // control rows that should only appear on the newest page.
  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, revision: "main-rev", has_more: true,
    events: [
      event({ type: "model", sid, model: "claude-mythos-5" }),
      event({ type: "effort", sid, effort: "max" }),
    ],
  }) });
  assert.equal(state.runtimes[sid].model, "claude-mythos-5");
  assert.equal(state.runtimes[sid].effort, "max");
  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, before: "older-turn",
    revision: "main-rev", has_more: false,
    events: [
      event({ type: "model", sid, model: "claude-sonnet-5" }),
      event({ type: "effort", sid, effort: "low" }),
    ],
  }) });
  assert.equal(state.runtimes[sid].model, "claude-mythos-5");
  assert.equal(state.runtimes[sid].effort, "max");
  const approvalBeforePlan = state.runtimes[sid].perm;
  state = reduce(state, { type: "event", event: event({
    type: "collaboration_mode", sid, mode: "plan",
  }) });
  assert.equal(state.runtimes[sid].collaborationMode, "plan");
  assert.equal(state.runtimes[sid].perm, approvalBeforePlan);
  assert.equal(state.runtimes[otherSid].collaborationMode, "default");
  state = reduce(state, { type: "set_collaboration_mode", mode: "default" });
  assert.equal(state.runtimes[sid].collaborationMode, "default");
  assert.equal(state.runtimes[sid].perm, approvalBeforePlan);
  const cachedSid = "cached-v5-codex";
  state = reduce(state, { type: "hydrate_cache", sid: cachedSid, turns: [{
    id: "cached-turn", codexTurnId: "legacy-turn-id", prompt: "旧缓存",
    done: true, blocks: [],
  }], revision: "cached-rev" });
  assert.equal(state.runtimes[cachedSid].turns[0].forkPointId, "legacy-turn-id");
  state = reduce(state, {
    type: "query_sent", sid, prompt: "在？", msg_id: "client-a", ts: 10_000,
  });
  for (const live of [
    event({ type: "user_msg", sid, msg_id: "client-a", prompt: "在？", ts: 10.1 }),
    event({ type: "assistant_msg_start", sid, message_id: "live-answer" }),
  ]) state = reduce(state, { type: "event", event: live });

  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, revision: "main-rev",
    in_progress: true, has_more: false,
    events: [
      event({ type: "user_msg", sid, msg_id: "engine-a",
        client_msg_id: "client-a", prompt: "在？", ts: 10 }),
      event({ type: "turn_end", sid, ts: 11, turn_id: "codex-turn-a",
        result: { subtype: "success", duration_ms: 0, is_error: false } }),
    ],
  }) });
  assert.equal(state.runtimes[sid].turns.length, 1);
  assert.equal(state.runtimes[sid].turns[0].done, false);
  assert.equal(state.runtimes[sid].turns[0].forkPointId, "codex-turn-a");

  for (const live of [
    event({ type: "delta", sid, message_id: "live-answer", text: "only once" }),
    event({ type: "assistant_msg_end", sid, message_id: "live-answer" }),
    event({ type: "turn_end", sid, ts: 12, turn_id: "codex-turn-a",
      result: { subtype: "success", duration_ms: 2000, is_error: false } }),
  ]) state = reduce(state, { type: "event", event: live });

  state = reduce(state, { type: "event", event: event({
    type: "history", sid, session_id: sid, revision: "main-rev",
    in_progress: false, has_more: false,
    events: [
      event({ type: "user_msg", sid, msg_id: "engine-a",
        client_msg_id: "client-a", prompt: "在？", ts: 10 }),
      event({ type: "assistant_msg_start", sid, message_id: "engine-answer" }),
      event({ type: "delta", sid, message_id: "engine-answer", text: "only once" }),
      event({ type: "assistant_msg_end", sid, message_id: "engine-answer" }),
      event({ type: "turn_end", sid, ts: 12, turn_id: "codex-turn-a",
        result: { subtype: "success", duration_ms: 2000, is_error: false } }),
    ],
  }) });
  assert.equal(state.runtimes[sid].turns.length, 1);
  assert.equal(state.runtimes[sid].turns[0].forkPointId, "codex-turn-a");
  assert.deepEqual(state.runtimes[sid].turns[0].blocks.map(
    (block: { kind: string; text?: string }) => block.kind === "text" ? block.text : "tool"),
  ["only once"]);
  assert.deepEqual(state.runtimes[otherSid].turns, [untouched]);

  // App-server 0.147 can report an interrupted summary for the exact native
  // turn which is still appending after context compaction. A newest
  // authoritative History page says which visible row owns that active head;
  // the next assistant item has no turn id and must reopen/append there instead
  // of creating a prompt-less tail after refresh.
  const compactRefreshSid = "compact-refresh-active-head";
  const compactRefreshVisibleId = "compact-refresh-user";
  const compactRefreshNativeId = "compact-refresh-native";
  let compactRefreshState = reduce({
    ...initialState,
    focusedSid: compactRefreshSid,
    runtimes: {
      [compactRefreshSid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
      },
    },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: compactRefreshSid,
      session_id: compactRefreshSid,
      revision: "compact-refresh-r1",
      generation: "compact-refresh-g1",
      build_seq: 1,
      live_seq: 10,
      detail: "summary",
      authoritative: true,
      has_more: true,
      newest_id: compactRefreshVisibleId,
      in_progress: true,
      compaction_continuation_turn_ids: [compactRefreshNativeId],
      events: [],
      turns: [{
        id: compactRefreshVisibleId,
        prompt: "continue the long task",
        forkPointId: compactRefreshNativeId,
        done: true,
        interrupted: true,
        blocks: [{
          kind: "text",
          message_id: "before-compact",
          channel: "commentary",
          text: "before compact",
          done: true,
        }],
        detailEventCount: 4,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    compactRefreshState.runtimes[compactRefreshSid].turns[0].done,
    false,
    "current authoritative History reopens its exact compacted active row",
  );
  assert.equal(
    compactRefreshState.runtimes[compactRefreshSid].turns[0].interrupted,
    undefined,
  );
  const compactIdleState = reduce(compactRefreshState, {
    type: "event",
    event: event({
      type: "state", sid: compactRefreshSid, seq: 11, state: "idle",
    }),
  });
  assert.deepEqual(
    {
      done: compactIdleState.runtimes[compactRefreshSid].turns[0].done,
      interrupted: compactIdleState.runtimes[compactRefreshSid].turns[0]
        .interrupted,
    },
    { done: true, interrupted: true },
    "a later exact idle lifecycle cannot leave a compact History shell running",
  );
  const coldInterruptedState = reduce({
    ...initialState,
    focusedSid: compactRefreshSid,
    runtimes: {
      [compactRefreshSid]: {
        ...createRuntime(), state: "idle" as const, syncReady: true,
      },
    },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: compactRefreshSid,
      session_id: compactRefreshSid,
      revision: "compact-cold-r1",
      generation: "compact-cold-g1",
      build_seq: 1,
      live_seq: 10,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: compactRefreshVisibleId,
      in_progress: true,
      events: [],
      turns: [{
        id: compactRefreshVisibleId,
        prompt: "a real interrupted turn on another device",
        forkPointId: compactRefreshNativeId,
        done: true,
        interrupted: true,
        blocks: [],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    coldInterruptedState.runtimes[compactRefreshSid].turns[0].done,
    true,
    "in_progress plus a native id cannot reopen a cold interrupt without compact evidence",
  );
  for (const liveEvent of [
    event({
      type: "assistant_msg_start",
      sid: compactRefreshSid,
      seq: 11,
      message_id: "after-compact",
      channel: "commentary",
    }),
    event({
      type: "delta",
      sid: compactRefreshSid,
      seq: 12,
      message_id: "after-compact",
      channel: "commentary",
      text: "after compact",
    }),
  ]) {
    compactRefreshState = reduce(compactRefreshState, {
      type: "event", event: liveEvent,
    });
  }
  assert.equal(
    compactRefreshState.runtimes[compactRefreshSid].turns.length,
    1,
    "post-compact assistant items cannot create a second prompt-less turn",
  );
  assert.deepEqual(
    compactRefreshState.runtimes[compactRefreshSid].turns[0].blocks
      .filter((block: Block) => block.kind === "text")
      .map((block: Block) => block.kind === "text" ? block.text : ""),
    ["before compact", "after compact"],
  );

  const falseTerminalSid = "compact-false-terminal-recovery";
  const falseTerminalId = "compact-false-terminal-user";
  const falseTerminalRuntime = (
    terminalSource: "unexpected_interrupt" | "remote_interrupt" | "failed",
    error = "本次回复未完成，请重试。",
  ) => ({
    ...createRuntime(),
    state: "running" as const,
    syncReady: true,
    historyRevision: "compact-false-r1",
    historyGeneration: "compact-false-g1",
    controlGeneration: "compact-false-g1",
    historyBuildSeq: 1,
    historyLiveSeq: 20,
    historyFence: 20,
    historyNewestId: falseTerminalId,
    lastLiveSeq: 20,
    lastLifecycleSeq: 20,
    turns: [{
      id: falseTerminalId,
      prompt: "keep working through compaction",
      forkPointId: "compact-false-native",
      done: true,
      interrupted: true,
      error,
      terminalSource,
      blocks: [],
    }],
  });
  const falseTerminalHistory = (
    overrides: Record<string, unknown> = {},
  ) => event({
    type: "history",
    sid: falseTerminalSid,
    session_id: falseTerminalSid,
    revision: "compact-false-r1",
    generation: "compact-false-g1",
    build_seq: 2,
    live_seq: 20,
    detail: "summary",
    authoritative: true,
    has_more: false,
    newest_id: falseTerminalId,
    in_progress: true,
    compaction_continuation_turn_ids: ["compact-false-native"],
    events: [],
    turns: [{
      id: falseTerminalId,
      prompt: "keep working through compaction",
      forkPointId: "compact-false-native",
      done: true,
      interrupted: true,
      blocks: [],
    }],
    ...overrides,
  });
  const recoverFalseTerminal = (
    terminalSource: "unexpected_interrupt" | "remote_interrupt" | "failed",
    overrides: Record<string, unknown> = {},
    error?: string,
  ) => reduce({
    ...initialState,
    focusedSid: falseTerminalSid,
    runtimes: {
      [falseTerminalSid]: falseTerminalRuntime(terminalSource, error),
    },
  }, {
    type: "event",
    event: falseTerminalHistory(overrides),
  });

  let eventDrivenFalseTerminal = {
    ...initialState,
    focusedSid: falseTerminalSid,
    runtimes: {
      [falseTerminalSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        historyRevision: "compact-false-r1",
        historyGeneration: "compact-false-g1",
        controlGeneration: "compact-false-g1",
        historyBuildSeq: 1,
        historyLiveSeq: 18,
        historyFence: 18,
        historyNewestId: falseTerminalId,
        lastLiveSeq: 18,
        lastLifecycleSeq: 18,
      },
    },
  };
  for (const liveEvent of [
    {
      type: "user_msg", sid: falseTerminalSid, seq: 19,
      msg_id: falseTerminalId, prompt: "keep working through compaction",
    },
    {
      type: "turn_end", sid: falseTerminalSid, seq: 20,
      turn_id: "compact-false-native",
      result: {
        subtype: "error_during_execution",
        duration_ms: 1000,
        is_error: true,
      },
    },
  ]) {
    eventDrivenFalseTerminal = reduce(eventDrivenFalseTerminal, {
      type: "event", event: event(liveEvent),
    });
  }
  assert.equal(
    eventDrivenFalseTerminal.runtimes[falseTerminalSid].turns[0]
      .terminalSource,
    "unexpected_interrupt",
    "the reducer must classify an unrequested interrupted TurnEnd itself",
  );
  let falseTerminalRecovered = reduce(eventDrivenFalseTerminal, {
    type: "event",
    event: falseTerminalHistory(),
  });
  assert.deepEqual(
    {
      done: falseTerminalRecovered.runtimes[falseTerminalSid].turns[0].done,
      interrupted: falseTerminalRecovered.runtimes[falseTerminalSid]
        .turns[0].interrupted,
      error: falseTerminalRecovered.runtimes[falseTerminalSid].turns[0].error,
      terminalSource: falseTerminalRecovered.runtimes[falseTerminalSid]
        .turns[0].terminalSource,
    },
    { done: false, interrupted: undefined, error: undefined,
      terminalSource: "compact_continuation" },
    "a current exact running History head repairs one unexpected false terminal",
  );

  const overlappingLiveTurn = {
    id: falseTerminalId,
    prompt: "keep working through compaction",
    forkPointId: "compact-false-native",
    done: false,
    blocks: [{
      kind: "process" as const,
      item_id: "compact-live-command",
      processKind: "command" as const,
      phase: "start" as const,
      status: "running" as const,
      title: "still running",
      done: false,
    }],
  };
  const overlappingBase = {
    ...initialState,
    focusedSid: "another-session",
    runtimes: {
      [falseTerminalSid]: {
        ...falseTerminalRuntime("unexpected_interrupt"),
        turns: [overlappingLiveTurn],
      },
    },
  };
  const overlappingCompactRecovered = reduce(overlappingBase, {
    type: "event",
    event: falseTerminalHistory(),
  });
  assert.deepEqual({
    done: overlappingCompactRecovered.runtimes[falseTerminalSid].turns[0].done,
    interrupted: overlappingCompactRecovered.runtimes[falseTerminalSid]
      .turns[0].interrupted,
    processDone: overlappingCompactRecovered.runtimes[falseTerminalSid]
      .turns[0].blocks[0].done,
  }, {
    done: false,
    interrupted: undefined,
    processDone: false,
  }, "exact compact authority reopens an overlapping live row even in a background session");

  const realInterruptRemainsTerminal = reduce(overlappingBase, {
    type: "event",
    event: falseTerminalHistory({
      compaction_continuation_turn_ids: [],
    }),
  });
  assert.deepEqual({
    done: realInterruptRemainsTerminal.runtimes[falseTerminalSid].turns[0].done,
    interrupted: realInterruptRemainsTerminal.runtimes[falseTerminalSid]
      .turns[0].interrupted,
    processDone: realInterruptRemainsTerminal.runtimes[falseTerminalSid]
      .turns[0].blocks[0].done,
  }, {
    done: true,
    interrupted: true,
    processDone: true,
  }, "running transport state cannot reopen a real terminal or leave its process open");

  const successfulBackgroundBase = {
    ...initialState,
    focusedSid: "another-session",
    runtimes: {
      [falseTerminalSid]: {
        ...falseTerminalRuntime("unexpected_interrupt"),
        state: "idle" as const,
        turns: [{ ...overlappingLiveTurn, done: true }],
      },
    },
  };
  const successfulBackgroundRemainsVisible = reduce(successfulBackgroundBase, {
    type: "event",
    event: falseTerminalHistory({
      in_progress: false,
      compaction_continuation_turn_ids: [],
      turns: [{
        id: falseTerminalId,
        prompt: "keep working through compaction",
        forkPointId: "compact-false-native",
        done: true,
        blocks: [],
      }],
    }),
  });
  assert.deepEqual({
    done: successfulBackgroundRemainsVisible.runtimes[falseTerminalSid]
      .turns[0].done,
    interrupted: successfulBackgroundRemainsVisible
      .runtimes[falseTerminalSid].turns[0].interrupted,
    processDone: successfulBackgroundRemainsVisible
      .runtimes[falseTerminalSid].turns[0].blocks[0].done,
  }, {
    done: true,
    interrupted: undefined,
    processDone: false,
  }, "a successful answer can retain genuine background agent activity");
  falseTerminalRecovered = reduce(falseTerminalRecovered, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: falseTerminalSid,
      seq: 21,
      message_id: "compact-false-follow-up",
      channel: "commentary",
    }),
  });
  assert.equal(
    falseTerminalRecovered.runtimes[falseTerminalSid].turns.length,
    1,
    "assistant output after false-terminal recovery stays in the original row",
  );
  const compactHistorySettled = reduce(falseTerminalRecovered, {
    type: "event",
    event: falseTerminalHistory({
      build_seq: 3,
      live_seq: 21,
      in_progress: false,
      compaction_continuation_turn_ids: [],
    }),
  });
  assert.deepEqual(
    {
      done: compactHistorySettled.runtimes[falseTerminalSid].turns[0].done,
      terminalSource: compactHistorySettled.runtimes[falseTerminalSid]
        .turns[0].terminalSource,
    },
    { done: true, terminalSource: undefined },
    "an authoritative terminal History settles compact metadata without a State frame",
  );

  const settledAuthoritySid = "settled-codex-lifecycle-authority";
  const settledBrowserId = "settled-codex-browser-message";
  const settledNativeId = "settled-codex-native-turn";
  const settledLiveBlock = {
    kind: "text" as const,
    message_id: "settled-live-final",
    channel: "final" as const,
    text: "the completed answer",
    done: true,
  };
  const settledAuthorityBase = {
    ...initialState,
    focusedSid: settledAuthoritySid,
    sessions: [{
      session_id: settledAuthoritySid,
      engine: "codex" as const,
      space: "code" as const,
    }],
    runtimes: {
      [settledAuthoritySid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
        historyRevision: "settled-codex-r1",
        historyGeneration: "settled-codex-g1",
        historyBuildSeq: 1,
        historyLiveSeq: 40,
        lastLiveSeq: 40,
        turns: [{
          id: settledBrowserId,
          clientMsgId: settledBrowserId,
          forkPointId: settledNativeId,
          prompt: "finish a large compacted task",
          done: true,
          interrupted: true,
          error: "该轮未正常结束",
          terminalSource: "failed" as const,
          blocks: [settledLiveBlock],
        }],
      },
    },
  };
  const settledAuthorityHistory = (
    overrides: Record<string, unknown> = {},
  ) => event({
    type: "history",
    sid: settledAuthoritySid,
    session_id: settledAuthoritySid,
    revision: "settled-codex-r1",
    generation: "settled-codex-g1",
    build_seq: 2,
    live_seq: 40,
    detail: "summary",
    authoritative: true,
    has_more: true,
    newest_id: "settled-codex-history-message",
    in_progress: false,
    events: [],
    turns: [{
      id: "settled-codex-history-message",
      clientMsgId: settledBrowserId,
      prompt: "finish a large compacted task",
      forkPointId: settledNativeId,
      done: true,
      blocks: [],
      detailEventCount: 300,
      detailLoaded: false,
    }],
    ...overrides,
  });
  const settledCodexState = reduce(settledAuthorityBase, {
    type: "event",
    event: settledAuthorityHistory(),
  });
  assert.deepEqual({
    turns: settledCodexState.runtimes[settledAuthoritySid].turns.length,
    done: settledCodexState.runtimes[settledAuthoritySid].turns[0].done,
    interrupted: settledCodexState.runtimes[settledAuthoritySid].turns[0]
      .interrupted,
    error: settledCodexState.runtimes[settledAuthoritySid].turns[0].error,
    terminalSource: settledCodexState.runtimes[settledAuthoritySid].turns[0]
      .terminalSource,
    blocks: settledCodexState.runtimes[settledAuthoritySid].turns[0].blocks,
  }, {
    turns: 1,
    done: true,
    interrupted: undefined,
    error: undefined,
    terminalSource: undefined,
    blocks: [settledLiveBlock],
  }, "settled Codex History repairs a stale live failure without losing its live detail");

  const genuineCodexFailure = reduce(settledAuthorityBase, {
    type: "event",
    event: settledAuthorityHistory({
      turns: [{
        id: "settled-codex-history-message",
        clientMsgId: settledBrowserId,
        prompt: "finish a large compacted task",
        forkPointId: settledNativeId,
        done: true,
        error: "official provider failure",
        blocks: [],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    genuineCodexFailure.runtimes[settledAuthoritySid].turns[0].error,
    "official provider failure",
    "settled Codex History replaces only a provisional terminal, not an official failure",
  );

  const racedCodexHistory = reduce(settledAuthorityBase, {
    type: "event",
    event: settledAuthorityHistory({ live_seq: 39 }),
  });
  assert.equal(
    racedCodexHistory.runtimes[settledAuthoritySid].turns[0].error,
    "该轮未正常结束",
    "History captured before a newer live terminal cannot erase that terminal",
  );

  const sharedTaskOnlyHistory = reduce(settledAuthorityBase, {
    type: "event",
    event: settledAuthorityHistory({
      newest_id: "another-steer-message",
      turns: [{
        id: "another-steer-message",
        prompt: "another steer in the same native task",
        forkPointId: settledNativeId,
        done: true,
        blocks: [],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    sharedTaskOnlyHistory.runtimes[settledAuthoritySid].turns[0].error,
    "该轮未正常结束",
    "a shared native task alone cannot let another steer clear this terminal",
  );

  const settledClaudeState = reduce({
    ...settledAuthorityBase,
    sessions: [{
      ...settledAuthorityBase.sessions[0],
      engine: "claude" as const,
    }],
  }, {
    type: "event",
    event: settledAuthorityHistory(),
  });
  assert.equal(
    settledClaudeState.runtimes[settledAuthoritySid].turns[0].error,
    "该轮未正常结束",
    "Claude transcript History cannot erase its SDK terminal lifecycle",
  );

  for (const [label, recovered] of [
    ["stale live sequence", recoverFalseTerminal(
      "unexpected_interrupt", { live_seq: 19 })],
    ["wrong newest row", recoverFalseTerminal(
      "unexpected_interrupt", { newest_id: "another-row" })],
    ["old wrapper generation", recoverFalseTerminal(
      "unexpected_interrupt", { generation: "compact-false-old" })],
    ["explicit Remote interrupt", recoverFalseTerminal(
      "remote_interrupt", { compaction_continuation_turn_ids: [] })],
    ["failed/auth terminal", recoverFalseTerminal(
      "failed", { compaction_continuation_turn_ids: [] },
      "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。")],
  ] as const) {
    assert.equal(
      recovered.runtimes[falseTerminalSid].turns[0].done,
      true,
      `${label} cannot be washed away by a running History envelope`,
    );
  }

  // A newer authoritative active head supersedes an older explicit task owner.
  // Assistant items have no turn id, so the downstream sequence is the only
  // authoritative fence: an explicit owner accepted before the exact History
  // head cannot steal its first output merely because that old row is still
  // locally unfinished.
  for (const [suffix, oldTaskId, newTaskId] of [
    ["different-task", "stale-task", "current-task"],
    ["same-task", "shared-task", "shared-task"],
  ] as const) {
    const sid = `history-head-after-old-steer-${suffix}`;
    let routedState = {
      ...initialState,
      focusedSid: sid,
      runtimes: {
        [sid]: {
          ...createRuntime(),
          state: "running" as const,
          syncReady: true,
        },
      },
    };
    routedState = reduce(routedState, {
      type: "event",
      event: event({
        type: "turn_steered",
        sid,
        seq: 10,
        msg_id: `old-client-${suffix}`,
        turn_id: oldTaskId,
        prompt: "old guided segment",
      }),
    });
    routedState = reduce(routedState, {
      type: "event",
      event: event({
        type: "history",
        sid,
        session_id: sid,
        revision: `history-head-${suffix}-r1`,
        generation: `history-head-${suffix}-g1`,
        build_seq: 1,
        live_seq: 10,
        detail: "summary",
        authoritative: true,
        has_more: true,
        newest_id: `new-history-${suffix}`,
        in_progress: true,
        compaction_continuation_turn_ids: [newTaskId],
        events: [],
        turns: [{
          id: `new-history-${suffix}`,
          prompt: "current native segment",
          forkPointId: newTaskId,
          done: true,
          interrupted: true,
          blocks: [],
        }],
      }),
    });
    routedState = reduce(routedState, {
      type: "event",
      event: event({
        type: "assistant_msg_start",
        sid,
        seq: 11,
        message_id: `current-output-${suffix}`,
        channel: "commentary",
      }),
    });
    const routedTurns = routedState.runtimes[sid].turns;
    assert.equal(
      routedTurns.find((turn: Turn) => turn.blocks.some((block: Block) =>
        block.kind === "text"
        && block.message_id === `current-output-${suffix}`))?.id,
      `new-history-${suffix}`,
      "an old explicit steer cannot steal output from a newer History head",
    );
  }

  // Conversely, a TurnSteered event accepted after the active History shell is
  // the ordered owner of subsequent id-less output. This is the normal local
  // steer path and must not regress while stale pre-head owners are ignored.
  const postHistorySteerSid = "post-history-local-steer-owner";
  let postHistorySteerState = reduce({
    ...initialState,
    focusedSid: postHistorySteerSid,
    runtimes: {
      [postHistorySteerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
      },
    },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: postHistorySteerSid,
      session_id: postHistorySteerSid,
      revision: "post-history-steer-r1",
      generation: "post-history-steer-g1",
      build_seq: 1,
      live_seq: 20,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: "post-history-shell",
      in_progress: true,
      events: [],
      turns: [{
        id: "post-history-shell",
        prompt: "work before guidance",
        forkPointId: "post-history-native-task",
        done: true,
        interrupted: true,
        blocks: [],
      }],
    }),
  });
  postHistorySteerState = reduce(postHistorySteerState, {
    type: "event",
    event: event({
      type: "turn_steered",
      sid: postHistorySteerSid,
      seq: 21,
      msg_id: "post-history-client-steer",
      turn_id: "post-history-native-task",
      prompt: "guide after the snapshot",
    }),
  });
  postHistorySteerState = reduce(postHistorySteerState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: postHistorySteerSid,
      seq: 22,
      message_id: "post-history-output",
      channel: "commentary",
    }),
  });
  assert.equal(
    postHistorySteerState.runtimes[postHistorySteerSid].turns.find(
      (turn: Turn) => turn.blocks.some((block: Block) =>
        block.kind === "text"
        && block.message_id === "post-history-output"))?.id,
    "post-history-client-steer",
    "a local steer created after the History head owns later output",
  );

  // Re-key can merge a temp runtime into an already-known native runtime. The
  // two runtimes share one downstream generation, so their History/steer
  // sequence evidence is comparable and must remain monotonic. Letting the
  // lower source values overwrite the target makes a stale temp owner win the
  // next id-less assistant item.
  const rekeyFenceOld = "tmp-rekey-owner-fence";
  const rekeyFenceSid = "real-rekey-owner-fence";
  let rekeyFenceState = reduce({
    ...initialState,
    focusedSid: rekeyFenceOld,
    runtimes: {
      [rekeyFenceOld]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        historyRevision: "rekey-owner-r1",
        historyGeneration: "rekey-owner-g1",
        controlGeneration: "rekey-owner-g1",
        historyNewestId: "rekey-owner-head",
        historyFence: 10,
        liveOwner: {
          turnId: "rekey-stale-source-owner",
          seq: 20,
        },
        turns: [{
          id: "rekey-stale-source-owner",
          liveTaskId: "rekey-stale-task",
          prompt: "stale temp owner",
          done: false,
          blocks: [],
        }],
      },
      [rekeyFenceSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        hasRevisionedControl: true,
        historyRevision: "rekey-owner-r1",
        historyGeneration: "rekey-owner-g1",
        controlGeneration: "rekey-owner-g1",
        historyNewestId: "rekey-owner-head",
        historyFence: 50,
        liveOwner: {
          turnId: "rekey-current-target-owner",
          seq: 55,
        },
        turns: [{
          id: "rekey-owner-head",
          forkPointId: "rekey-current-task",
          prompt: "authoritative head",
          done: false,
          blocks: [],
        }, {
          id: "rekey-current-target-owner",
          liveTaskId: "rekey-current-task",
          prompt: "current owner after head",
          done: false,
          blocks: [],
        }],
      },
    },
  }, {
    type: "event",
    event: event({
      type: "session_rekey",
      sid: rekeyFenceOld,
      old_key: rekeyFenceOld,
      session_id: rekeyFenceSid,
      cwd: "/repo",
    }),
  });
  assert.equal(
    rekeyFenceState.runtimes[rekeyFenceSid].historyFence,
    50,
    "same-generation rekey keeps the greatest History owner fence",
  );
  assert.deepEqual(
    rekeyFenceState.runtimes[rekeyFenceSid].liveOwner,
    { turnId: "rekey-current-target-owner", seq: 55 },
    "same-generation rekey keeps the newest explicit owner proof",
  );
  assert.equal(
    rekeyFenceState.runtimes[rekeyFenceSid].hasRevisionedControl,
    true,
    "same-generation rekey preserves revisioned-control authority from either runtime",
  );
  rekeyFenceState = reduce(rekeyFenceState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: rekeyFenceSid,
      seq: 56,
      message_id: "rekey-current-output",
      channel: "commentary",
    }),
  });
  assert.equal(
    rekeyFenceState.runtimes[rekeyFenceSid].turns.find(
      (turn: Turn) => turn.blocks.some((block: Block) =>
        block.kind === "text"
        && block.message_id === "rekey-current-output"))?.id,
    "rekey-current-target-owner",
    "rekey cannot route new output through an older temp owner",
  );

  // A temp runtime belongs to the wrapper generation which captured its real
  // id. If a stale durable runtime survived from an older wrapper, its History
  // revision/head and sequence watermarks are not comparable and must not be
  // inherited during the re-key.
  const crossGenerationOld = "tmp-cross-generation-rekey";
  const crossGenerationSid = "real-cross-generation-rekey";
  const staleCrossGenerationTurn: Turn = {
    id: "rekey-old-projection",
    prompt: "old projected history",
    done: true,
    blocks: [],
  };
  const staleCrossGenerationBrowse = {
    scopeKey: "rekey-old-scope",
    sid: crossGenerationSid,
    revision: "rekey-old-revision",
    generation: "rekey-old-generation",
    viewId: "rekey-old-view",
    windowEpoch: 0,
    turns: [staleCrossGenerationTurn],
    segments: [],
    loadedPageKeys: [],
    oldestPageKey: null,
    newestPageKey: null,
    hasOlder: true,
    olderCursor: "rekey-old-cursor",
    hasNewer: false,
    newerPageKey: null,
    latestDirty: false,
  };
  let crossGenerationState = reduce({
    ...initialState,
    focusedSid: crossGenerationOld,
    historyRecovery: {
      sid: crossGenerationSid,
      turns: [staleCrossGenerationTurn],
      activeOwnerId: null,
      hasMore: true,
      oldestId: "rekey-old-cursor",
      viewRevision: "rekey-old-revision",
      expectedGeneration: "rekey-old-generation",
      candidateBuildSeq: null,
      acceptedRevision: null,
    },
    historyBrowse: staleCrossGenerationBrowse,
    retainedHistoryBrowse: {
      ...staleCrossGenerationBrowse,
      viewId: "rekey-old-retained-view",
    },
    runtimes: {
      [crossGenerationOld]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "rekey-new-generation",
        lastLiveSeq: 3,
        lastLifecycleSeq: 3,
        liveOwner: {
          turnId: "rekey-new-owner",
          seq: 3,
        },
        turns: [{
          id: "rekey-new-owner",
          clientMsgId: "rekey-new-owner",
          liveTaskId: "rekey-new-native-task",
          prompt: "new generation prompt",
          done: false,
          blocks: [],
        }],
      },
      [crossGenerationSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        hasRevisionedControl: true,
        controlGeneration: "rekey-old-generation",
        historyRevision: "rekey-old-revision",
        historyGeneration: "rekey-old-generation",
        historyBuildSeq: 80,
        historyLiveSeq: 81,
        historyFence: 81,
        historyNewestId: "rekey-old-head",
        lastLiveSeq: 82,
        lastLifecycleSeq: 82,
        hydratedCacheTurnIds: ["rekey-old-head"],
        liveDetailTurnIds: ["rekey-old-head"],
        liveOwner: {
          turnId: "rekey-old-head",
          seq: 82,
        },
        turns: [{
          id: "rekey-old-head",
          forkPointId: "rekey-old-native-task",
          prompt: "old generation prompt",
          done: false,
          blocks: [],
        }],
      },
    },
  }, {
    type: "event",
    event: event({
      type: "session_rekey",
      sid: crossGenerationOld,
      old_key: crossGenerationOld,
      session_id: crossGenerationSid,
      cwd: "/repo",
    }),
  });
  const crossGenerationRuntime =
    crossGenerationState.runtimes[crossGenerationSid];
  assert.deepEqual(
    {
      historyRevision: crossGenerationRuntime.historyRevision,
      historyGeneration: crossGenerationRuntime.historyGeneration,
      controlGeneration: crossGenerationRuntime.controlGeneration,
      historyBuildSeq: crossGenerationRuntime.historyBuildSeq,
      historyLiveSeq: crossGenerationRuntime.historyLiveSeq,
      historyFence: crossGenerationRuntime.historyFence,
      historyNewestId: crossGenerationRuntime.historyNewestId,
      lastLiveSeq: crossGenerationRuntime.lastLiveSeq,
      lastLifecycleSeq: crossGenerationRuntime.lastLifecycleSeq,
      hydratedCacheTurnIds: crossGenerationRuntime.hydratedCacheTurnIds,
      liveDetailTurnIds: crossGenerationRuntime.liveDetailTurnIds,
    },
    {
      historyRevision: null,
      historyGeneration: null,
      controlGeneration: "rekey-new-generation",
      historyBuildSeq: 0,
      historyLiveSeq: 0,
      historyFence: null,
      historyNewestId: null,
      lastLiveSeq: 3,
      lastLifecycleSeq: 3,
      hydratedCacheTurnIds: [],
      liveDetailTurnIds: [],
    },
    "cross-generation rekey cannot inherit old History authority or watermarks",
  );
  assert.deepEqual(
    crossGenerationRuntime.turns.map((turn: Turn) => turn.id),
    ["rekey-new-owner"],
    "cross-generation rekey drops the old generation's visible History head",
  );
  assert.deepEqual(
    crossGenerationRuntime.liveOwner,
    { turnId: "rekey-new-owner", seq: 3 },
    "cross-generation rekey preserves only the new generation owner proof",
  );
  assert.equal(
    crossGenerationRuntime.hasRevisionedControl,
    false,
    "cross-generation rekey cannot inherit sticky control authority from the old target runtime",
  );
  let crossGenerationLegacyControlState = reduce(crossGenerationState, {
    type: "event",
    event: event({
      type: "history",
      sid: crossGenerationSid,
      session_id: crossGenerationSid,
      revision: "rekey-new-legacy-ownership",
      generation: "rekey-new-generation",
      build_seq: 1,
      live_seq: 3,
      has_more: false,
      in_progress: true,
      authoritative: true,
      external: true,
      takeover_pending: false,
      events: [],
    }),
  });
  assert.equal(
    crossGenerationLegacyControlState.runtimes[crossGenerationSid].external,
    true,
    "cross-generation rekey restores authoritative History ownership fallback",
  );
  crossGenerationLegacyControlState = reduce(
    crossGenerationLegacyControlState,
    {
      type: "event",
      event: event({
        type: "takeover_state",
        sid: crossGenerationSid,
        pending: true,
        message: "new generation legacy takeover",
      }),
    },
  );
  assert.deepEqual(
    {
      pending: crossGenerationLegacyControlState
        .runtimes[crossGenerationSid].takeoverPending,
      message: crossGenerationLegacyControlState
        .runtimes[crossGenerationSid].takeoverMessage,
    },
    {
      pending: true,
      message: "new generation legacy takeover",
    },
    "cross-generation rekey restores legacy takeover-state updates",
  );
  assert.equal(crossGenerationState.historyRecovery, null,
    "cross-generation rekey drops the durable id's stale recovery window");
  assert.equal(crossGenerationState.historyBrowse, null,
    "cross-generation rekey drops the durable id's stale browse window");
  assert.equal(crossGenerationState.retainedHistoryBrowse, null,
    "cross-generation rekey drops the durable id's stale retained window");
  crossGenerationState = reduce(crossGenerationState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: crossGenerationSid,
      seq: 4,
      message_id: "rekey-new-output",
      channel: "commentary",
    }),
  });
  assert.equal(
    crossGenerationState.runtimes[crossGenerationSid].turns.find(
      (turn: Turn) => turn.blocks.some((block: Block) =>
        block.kind === "text"
        && block.message_id === "rekey-new-output"))?.id,
    "rekey-new-owner",
    "unbound new-generation output cannot attach to an old History head",
  );

  // In one generation mergeInitialHistory deliberately keeps the live/source
  // row id while preserving the target id as historyTurnId. Owner evidence
  // must follow that surviving id instead of becoming a dangling reference.
  const overlapOwnerOld = "tmp-rekey-overlap-owner";
  const overlapOwnerSid = "real-rekey-overlap-owner";
  let overlapOwnerState = reduce({
    ...initialState,
    focusedSid: overlapOwnerOld,
    runtimes: {
      [overlapOwnerOld]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "rekey-overlap-generation",
        historyRevision: "rekey-overlap-revision",
        historyGeneration: "rekey-overlap-generation",
        historyNewestId: "rekey-overlap-head",
        historyFence: 10,
        liveOwner: {
          turnId: "rekey-surviving-owner",
          seq: 19,
        },
        turns: [{
          id: "rekey-surviving-owner",
          historyTurnId: "rekey-retired-owner",
          liveTaskId: "rekey-overlap-native-task",
          prompt: "guided owner",
          done: false,
          blocks: [],
        }],
      },
      [overlapOwnerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "rekey-overlap-generation",
        historyRevision: "rekey-overlap-revision",
        historyGeneration: "rekey-overlap-generation",
        historyNewestId: "rekey-overlap-head",
        historyFence: 10,
        liveOwner: {
          turnId: "rekey-retired-owner",
          seq: 20,
        },
        turns: [{
          id: "rekey-overlap-head",
          forkPointId: "rekey-overlap-head-task",
          prompt: "authoritative head",
          done: false,
          blocks: [],
        }, {
          id: "rekey-retired-owner",
          clientMsgId: "rekey-surviving-owner",
          liveTaskId: "rekey-overlap-native-task",
          prompt: "guided owner",
          done: false,
          blocks: [],
        }],
      },
    },
  }, {
    type: "event",
    event: event({
      type: "session_rekey",
      sid: overlapOwnerOld,
      old_key: overlapOwnerOld,
      session_id: overlapOwnerSid,
      cwd: "/repo",
    }),
  });
  assert.equal(
    overlapOwnerState.runtimes[overlapOwnerSid].turns.some(
      (turn: Turn) => turn.id === "rekey-retired-owner"),
    false,
    "the target owner id is replaced by the overlapping source row",
  );
  assert.deepEqual(
    overlapOwnerState.runtimes[overlapOwnerSid].liveOwner,
    { turnId: "rekey-surviving-owner", seq: 20 },
    "same-generation owner evidence follows the row which survives merging",
  );
  overlapOwnerState = reduce(overlapOwnerState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: overlapOwnerSid,
      seq: 21,
      message_id: "rekey-overlap-output",
      channel: "commentary",
    }),
  });
  assert.equal(
    overlapOwnerState.runtimes[overlapOwnerSid].turns.find(
      (turn: Turn) => turn.blocks.some((block: Block) =>
        block.kind === "text"
        && block.message_id === "rekey-overlap-output"))?.id,
    "rekey-surviving-owner",
    "a remapped post-fence owner keeps unbound output off the History head",
  );

  // Sequence numbers restart with the wrapper generation. Even when no
  // History page established historyGeneration yet, a new Snapshot must clear
  // the old generation's owner/fence rather than comparing unrelated numbers.
  const ownerGenerationSid = "owner-generation-reset-without-history";
  const ownerGenerationState = reduce({
    ...initialState,
    focusedSid: ownerGenerationSid,
    runtimes: {
      [ownerGenerationSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "owner-old-g",
        historyGeneration: null,
        historyFence: 40,
        liveOwner: {
          turnId: "owner-old-turn",
          seq: 41,
        },
        turns: [{
          id: "owner-old-turn",
          liveTaskId: "owner-old-task",
          prompt: "old generation",
          done: false,
          blocks: [],
        }],
      },
    },
  }, {
    type: "event",
    event: event({
      type: "snapshot",
      sid: ownerGenerationSid,
      cc_session_id: ownerGenerationSid,
      generation: "owner-new-g",
      state: "running",
    }),
  });
  assert.equal(
    ownerGenerationState.runtimes[ownerGenerationSid].historyFence,
    null,
    "generation switch clears an unscoped History owner fence",
  );
  assert.equal(
    ownerGenerationState.runtimes[ownerGenerationSid].liveOwner,
    null,
    "generation switch clears an unscoped explicit owner proof",
  );

  // Clearing the explicit owner proof is insufficient if the legacy fallback
  // can still select an unfinished row carrying the previous generation's
  // liveTaskId. The first unbound narrative item in the new generation must
  // open a fresh row, regardless of which item starts the stream.
  for (const [kind, freshTurnId, unboundEvent, blockOwnsEvent] of [
    [
      "assistant",
      "owner-new-assistant",
      event({
        type: "assistant_msg_start",
        sid: ownerGenerationSid,
        seq: 1,
        message_id: "owner-new-assistant",
        channel: "commentary",
      }),
      (block: Block) => block.kind === "text"
        && block.message_id === "owner-new-assistant",
    ],
    [
      "delta",
      "owner-new-delta",
      event({
        type: "delta",
        sid: ownerGenerationSid,
        seq: 1,
        message_id: "owner-new-delta",
        channel: "commentary",
        text: "new generation text",
      }),
      (block: Block) => block.kind === "text"
        && block.message_id === "owner-new-delta",
    ],
    [
      "tool",
      "owner-new-tool-message",
      event({
        type: "tool_use",
        sid: ownerGenerationSid,
        seq: 1,
        message_id: "owner-new-tool-message",
        tool_use_id: "owner-new-tool",
        tool: "Read",
        input: { file_path: "README.md" },
      }),
      (block: Block) => block.kind === "tool"
        && block.tool_use_id === "owner-new-tool",
    ],
  ] as const) {
    const routed = reduce(ownerGenerationState, {
      type: "event",
      event: unboundEvent,
    });
    const owner = routed.runtimes[ownerGenerationSid].turns.find(
      (turn: Turn) => turn.blocks.some(blockOwnsEvent),
    );
    assert.equal(
      owner?.id,
      freshTurnId,
      `the first unbound ${kind} item cannot fall back across generations`,
    );
  }

  let chainedGenerationState = ownerGenerationState;
  for (const chainedEvent of [
    event({
      type: "assistant_msg_start",
      sid: ownerGenerationSid,
      seq: 1,
      message_id: "owner-new-chain-first",
      channel: "commentary",
    }),
    event({
      type: "tool_use",
      sid: ownerGenerationSid,
      seq: 2,
      message_id: "owner-new-chain-tool-message",
      tool_use_id: "owner-new-chain-tool",
      tool: "Read",
      input: { file_path: "README.md" },
    }),
    event({
      type: "assistant_msg_start",
      sid: ownerGenerationSid,
      seq: 3,
      message_id: "owner-new-chain-second",
      channel: "final",
    }),
  ]) {
    chainedGenerationState = reduce(chainedGenerationState, {
      type: "event",
      event: chainedEvent,
    });
  }
  const chainedGenerationTurns =
    chainedGenerationState.runtimes[ownerGenerationSid].turns;
  const chainedOwner = chainedGenerationTurns.find((turn: Turn) =>
    turn.blocks.some((block: Block) => block.kind === "text"
      && block.message_id === "owner-new-chain-first"));
  assert.deepEqual(
    chainedOwner?.blocks.map((block: Block) => block.kind === "text"
      ? block.message_id
      : block.kind === "tool" ? block.tool_use_id : block.item_id),
    [
      "owner-new-chain-first",
      "owner-new-chain-tool",
      "owner-new-chain-second",
    ],
    "every unbound item in the new turn stays on its generation-safe owner",
  );
  assert.equal(
    chainedGenerationTurns.find((turn: Turn) =>
      turn.id === "owner-old-turn")?.blocks.length,
    0,
    "later new-generation message ids cannot reactivate the old liveTaskId owner",
  );

  const sameGenerationLegacyState = reduce({
    ...initialState,
    focusedSid: "same-generation-legacy-owner",
    runtimes: {
      "same-generation-legacy-owner": {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "same-generation-legacy-g",
        turns: [{
          id: "same-generation-legacy-turn",
          liveTaskId: "same-generation-legacy-task",
          prompt: "legacy owner",
          done: false,
          blocks: [],
        }],
      },
    },
  }, {
    type: "event",
    event: event({
      type: "delta",
      sid: "same-generation-legacy-owner",
      seq: 2,
      message_id: "same-generation-legacy-output",
      channel: "commentary",
      text: "same generation",
    }),
  });
  assert.equal(
    sameGenerationLegacyState.runtimes["same-generation-legacy-owner"]
      .turns.find((turn: Turn) => turn.blocks.some((block: Block) =>
        block.kind === "text"
        && block.message_id === "same-generation-legacy-output"))?.id,
    "same-generation-legacy-turn",
    "same-generation legacy liveTaskId fallback remains supported",
  );

  // If TurnEnd is lost, an idle authoritative History owns the lifecycle of a
  // block-overlapping aliasless live row. Its interrupted terminal and timing
  // must not be converted into a successful local idle repair.
  const idleInterruptedSid = "idle-interrupted-aliasless-replay";
  let idleInterruptedState = {
    ...initialState,
    focusedSid: idleInterruptedSid,
    runtimes: {
      [idleInterruptedSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
      },
    },
  };
  for (const liveEvent of [
    event({
      type: "turn_steered",
      sid: idleInterruptedSid,
      seq: 30,
      msg_id: "idle-interrupted-client",
      turn_id: "idle-interrupted-native",
      prompt: "work until interrupted",
    }),
    event({
      type: "assistant_msg_start",
      sid: idleInterruptedSid,
      seq: 31,
      message_id: "idle-interrupted-final",
      channel: "final",
    }),
    event({
      type: "delta",
      sid: idleInterruptedSid,
      seq: 32,
      message_id: "idle-interrupted-final",
      channel: "final",
      text: "partial final before interruption",
    }),
    event({
      type: "assistant_msg_end",
      sid: idleInterruptedSid,
      seq: 33,
      message_id: "idle-interrupted-final",
      channel: "final",
    }),
  ]) {
    idleInterruptedState = reduce(idleInterruptedState, {
      type: "event", event: liveEvent,
    });
  }
  idleInterruptedState = reduce(idleInterruptedState, {
    type: "event",
    event: event({
      type: "history",
      sid: idleInterruptedSid,
      session_id: idleInterruptedSid,
      revision: "idle-interrupted-r1",
      generation: "idle-interrupted-g1",
      build_seq: 1,
      live_seq: 33,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: "idle-interrupted-history",
      in_progress: false,
      ts: 1.2,
      events: [],
      turns: [{
        id: "idle-interrupted-history",
        prompt: "work until interrupted",
        forkPointId: "idle-interrupted-native",
        done: true,
        interrupted: true,
        ts: 100,
        doneTs: 1_200,
        durationMs: 1_100,
        blocks: [{
          kind: "text",
          message_id: "idle-interrupted-final",
          channel: "final",
          text: "partial final before interruption",
          done: true,
        }],
      }],
    }),
  });
  const idleInterruptedTurn =
    idleInterruptedState.runtimes[idleInterruptedSid].turns[0];
  assert.deepEqual(
    {
      done: idleInterruptedTurn.done,
      interrupted: idleInterruptedTurn.interrupted,
      doneTs: idleInterruptedTurn.doneTs,
      durationMs: idleInterruptedTurn.durationMs,
    },
    { done: true, interrupted: true, doneTs: 1_200, durationMs: 1_100 },
    "idle History keeps its authoritative interrupted lifecycle and timing",
  );

  // During Command+R the first authoritative summary can materialize a steered
  // user row before its client/native alias is available. The same native
  // commentary is already present in the live row. Exact block identity plus
  // the envelope's exact active newest_id is sufficient to bind that one row;
  // prompt equality or a shared native task id alone is not.
  const aliaslessActiveSid = "aliasless-active-steer-refresh";
  const aliaslessHistory = (buildSeq: number) => event({
    type: "history",
    sid: aliaslessActiveSid,
    session_id: aliaslessActiveSid,
    revision: "aliasless-active-r1",
    generation: "aliasless-active-g1",
    build_seq: buildSeq,
    live_seq: 30,
    detail: "summary",
    authoritative: true,
    has_more: true,
    newest_id: "history-steer-row",
    in_progress: true,
    events: [],
    turns: [
      {
        id: "native-active-task",
        prompt: "start work",
        forkPointId: "native-active-task",
        done: true,
        blocks: [],
      },
      {
        id: "history-steer-row",
        prompt: "guide the running task",
        forkPointId: "native-active-task",
        done: true,
        blocks: [{
          kind: "text",
          message_id: "shared-active-commentary",
          channel: "commentary",
          text: "one visible update",
          done: true,
        }],
      },
    ],
  });
  let aliaslessActiveState = reduce({
    ...initialState,
    focusedSid: aliaslessActiveSid,
    runtimes: {
      [aliaslessActiveSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        turns: [{
          id: "client-steer-row",
          clientMsgId: "client-steer-row",
          liveTaskId: "native-active-task",
          prompt: "guide the running task",
          done: false,
          blocks: [{
            kind: "text",
            message_id: "shared-active-commentary",
            channel: "commentary",
            text: "one visible update",
            done: true,
            liveOrder: 0,
          }],
          nextLiveBlockOrder: 1,
        }],
      },
    },
  }, {
    type: "event",
    event: aliaslessHistory(1),
  });
  assert.deepEqual(
    aliaslessActiveState.runtimes[aliaslessActiveSid].turns.map(
      (turn: Turn) => ({
        id: turn.id,
        historyTurnId: turn.historyTurnId,
        prompt: turn.prompt,
        done: turn.done,
      }),
    ),
    [
      {
        id: "native-active-task",
        historyTurnId: undefined,
        prompt: "start work",
        done: true,
      },
      {
        id: "client-steer-row",
        historyTurnId: "history-steer-row",
        prompt: "guide the running task",
        done: false,
      },
    ],
    "refresh binds one aliasless active history row to its proven live row",
  );
  aliaslessActiveState = reduce(aliaslessActiveState, {
    type: "event",
    event: aliaslessHistory(2),
  });
  assert.equal(
    aliaslessActiveState.runtimes[aliaslessActiveSid].turns.length,
    2,
    "repeated active History refreshes cannot recreate the discarded duplicate",
  );
  assert.equal(
    aliaslessActiveState.runtimes[aliaslessActiveSid].turns[1].blocks.filter(
      (block: Block) => block.kind === "text"
        && block.message_id === "shared-active-commentary").length,
    1,
    "the bound active commentary renders once",
  );

  // A native TUI/App turn can reach the browser through both the authoritative
  // rollout History and a delayed app-server replay. The stable message id
  // proves these are two projections of one completed item; replaying the full
  // delta after History must not append the same paragraph a second time.
  const delayedReplaySid = "history-before-delayed-live-replay";
  const delayedReplayText = "one completed commentary";
  let delayedReplayState = reduce({
    ...initialState,
    focusedSid: delayedReplaySid,
    runtimes: {
      [delayedReplaySid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
      },
    },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: delayedReplaySid,
      session_id: delayedReplaySid,
      revision: "delayed-replay-r1",
      generation: "delayed-replay-g1",
      build_seq: 1,
      live_seq: 10,
      detail: "summary",
      authoritative: true,
      has_more: true,
      oldest_id: "delayed-replay-user",
      newest_id: "delayed-replay-user",
      in_progress: true,
      events: [],
      turns: [{
        id: "delayed-replay-user",
        prompt: "inspect",
        forkPointId: "delayed-replay-task",
        done: false,
        blocks: [{
          kind: "text",
          message_id: "delayed-replay-commentary",
          channel: "commentary",
          text: delayedReplayText,
          done: true,
        }],
      }],
    }),
  });
  for (const replayed of [
    event({
      type: "assistant_msg_start",
      sid: delayedReplaySid,
      seq: 11,
      message_id: "delayed-replay-commentary",
      channel: "commentary",
    }),
    event({
      type: "delta",
      sid: delayedReplaySid,
      seq: 12,
      message_id: "delayed-replay-commentary",
      channel: "commentary",
      text: "one completed ",
    }),
    event({
      type: "delta",
      sid: delayedReplaySid,
      seq: 13,
      message_id: "delayed-replay-commentary",
      channel: "commentary",
      text: "commentary",
    }),
    event({
      type: "assistant_msg_end",
      sid: delayedReplaySid,
      seq: 14,
      message_id: "delayed-replay-commentary",
      channel: "commentary",
    }),
  ]) {
    delayedReplayState = reduce(delayedReplayState, {
      type: "event",
      event: replayed,
    });
  }
  const delayedReplayBlock = delayedReplayState.runtimes[
    delayedReplaySid
  ].turns[0].blocks[0];
  assert.equal(
    delayedReplayBlock.kind === "text" ? delayedReplayBlock.text : null,
    delayedReplayText,
    "a completed authoritative item absorbs its delayed live replay",
  );

  // Existing installations may already have persisted the polluted projection
  // in IndexedDB. A later authoritative page for the same stable native item
  // must heal the old duplicate instead of preserving the longer cached text.
  const delayedReplayRuntime = delayedReplayState.runtimes[delayedReplaySid];
  delayedReplayState = {
    ...delayedReplayState,
    runtimes: {
      ...delayedReplayState.runtimes,
      [delayedReplaySid]: {
        ...delayedReplayRuntime,
        turns: delayedReplayRuntime.turns.map((turn: Turn) => ({
          ...turn,
          blocks: turn.blocks.map((block: Block) => block.kind === "text"
            && block.message_id === "delayed-replay-commentary"
            ? { ...block, text: delayedReplayText + delayedReplayText }
            : { ...block }),
        })),
      },
    },
  };
  delayedReplayState = reduce(delayedReplayState, {
    type: "event",
    event: event({
      type: "history",
      sid: delayedReplaySid,
      session_id: delayedReplaySid,
      revision: "delayed-replay-r1",
      generation: "delayed-replay-g1",
      build_seq: 2,
      live_seq: 14,
      detail: "summary",
      authoritative: true,
      has_more: true,
      oldest_id: "delayed-replay-user",
      newest_id: "delayed-replay-user",
      in_progress: false,
      events: [],
      turns: [{
        id: "delayed-replay-user",
        prompt: "inspect",
        forkPointId: "delayed-replay-task",
        done: true,
        blocks: [{
          kind: "text",
          message_id: "delayed-replay-commentary",
          channel: "commentary",
          text: delayedReplayText,
          done: true,
        }],
      }],
    }),
  });
  const repairedReplayBlock = delayedReplayState.runtimes[
    delayedReplaySid
  ].turns[0].blocks[0];
  assert.equal(
    repairedReplayBlock.kind === "text" ? repairedReplayBlock.text : null,
    delayedReplayText,
    "authoritative History heals a previously persisted duplicate",
  );

  // A replacement wrapper resumes an already-visible Remote-owned Codex turn
  // under a fresh sequence generation.  Its exact TurnBinding must restore the
  // old logical owner before a recovered process/text tail arrives; sharing the
  // native task id alone remains insufficient because steer segments share it.
  const recoveredOwnerSid = "recovered-wrapper-owner-binding";
  const recoveredOwnerMsg = "recovered-remote-message";
  const recoveredNativeTurn = "recovered-native-turn";
  let recoveredOwnerState = {
    ...initialState,
    focusedSid: recoveredOwnerSid,
    runtimes: {
      [recoveredOwnerSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "recovered-old-generation",
        historyGeneration: "recovered-old-generation",
        turns: [{
          id: recoveredOwnerMsg,
          clientMsgId: recoveredOwnerMsg,
          prompt: "执行收紧后的计划。",
          forkPointId: recoveredNativeTurn,
          blocks: [],
          done: false,
        }],
      },
    },
  };
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "replay_start",
      sid: recoveredOwnerSid,
      from_seq: 1,
      to_seq: 5,
      truncated: false,
      generation: "recovered-new-generation",
    }),
  });
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "turn_binding",
      sid: recoveredOwnerSid,
      seq: 1,
      msg_id: recoveredOwnerMsg,
      turn_id: recoveredNativeTurn,
    }),
  });
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "state",
      sid: recoveredOwnerSid,
      seq: 2,
      state: "running",
      msg_id: recoveredOwnerMsg,
    }),
  });
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "process",
      sid: recoveredOwnerSid,
      seq: 3,
      item_id: "recovered-process",
      kind: "command",
      phase: "end",
      status: "succeeded",
      turn_id: recoveredNativeTurn,
      title: "continued after restart",
    }),
  });
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: recoveredOwnerSid,
      seq: 4,
      message_id: "recovered-final",
      channel: "final",
    }),
  });
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "delta",
      sid: recoveredOwnerSid,
      seq: 5,
      message_id: "recovered-final",
      channel: "final",
      text: "one recovered answer",
    }),
  });
  assert.equal(
    recoveredOwnerState.runtimes[recoveredOwnerSid].turns.length,
    1,
    "new-generation recovered tail stays on the exact old logical owner",
  );
  assert.deepEqual(
    recoveredOwnerState.runtimes[recoveredOwnerSid].turns[0].blocks.map(
      (block: Block) => block.kind === "process"
        ? block.item_id : block.kind === "text" ? block.message_id : null,
    ),
    ["recovered-process", "recovered-final"],
    "process-first and assistant recovery events share one bound segment",
  );
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "turn_end",
      sid: recoveredOwnerSid,
      seq: 6,
      turn_id: recoveredNativeTurn,
      result: { subtype: "success", duration_ms: 1, is_error: false },
    }),
  });
  assert.equal(
    recoveredOwnerState.runtimes[recoveredOwnerSid].turns[0].done,
    true,
  );
  assert.equal(
    recoveredOwnerState.runtimes[recoveredOwnerSid].pendingLiveBinding,
    null,
  );
  recoveredOwnerState = reduce(recoveredOwnerState, {
    type: "event",
    event: event({
      type: "history",
      sid: recoveredOwnerSid,
      session_id: recoveredOwnerSid,
      revision: "recovered-owner-r1",
      generation: "recovered-new-generation",
      build_seq: 1,
      live_seq: 6,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: "recovered-official-user",
      in_progress: false,
      events: [],
      turns: [{
        id: "recovered-official-user",
        clientMsgId: recoveredOwnerMsg,
        prompt: "执行收紧后的计划。",
        forkPointId: recoveredNativeTurn,
        done: true,
        blocks: [{
          kind: "text",
          message_id: "recovered-final",
          channel: "final",
          text: "one recovered answer",
          done: true,
        }],
      }],
    }),
  });
  assert.equal(
    recoveredOwnerState.runtimes[recoveredOwnerSid].turns.length,
    1,
    "terminal canonical History keeps one exact recovered owner",
  );
  assert.equal(
    recoveredOwnerState.runtimes[recoveredOwnerSid].turns[0].clientMsgId,
    recoveredOwnerMsg,
  );
  assert.deepEqual(
    recoveredOwnerState.runtimes[recoveredOwnerSid].turns[0].blocks.map(
      (block: Block) => block.kind === "text" ? block.text : null,
    ),
    ["one recovered answer"],
    "terminal reconciliation does not retain a second recovered layer",
  );

  // A replacement wrapper can prove that its official control turn and the
  // rollout stream task are two exact ids for one still-active turn. History
  // remains keyed by the rollout task, while live notifications are normalized
  // to the control turn. Only the bounded continuation proof may bridge them;
  // otherwise a real interrupted row must remain terminal.
  const splitRecoverySid = "recovered-split-control-stream-binding";
  const splitRecoveryMsg = "recovered-split-client-message";
  const splitRecoveryControl = "recovered-split-control-turn";
  const splitRecoveryStream = "recovered-split-stream-task";
  const splitRecoveryHistory = "recovered-split-native-user";
  const splitRecoveryBase = {
    ...initialState,
    focusedSid: splitRecoverySid,
    runtimes: {
      [splitRecoverySid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "recovered-split-generation",
        historyGeneration: "recovered-split-generation",
        legacyLiveFallbackBlocked: true,
      },
    },
  };
  const splitRecoveryBinding = event({
    type: "turn_binding",
    sid: splitRecoverySid,
    seq: 1,
    msg_id: splitRecoveryMsg,
    turn_id: splitRecoveryControl,
  });
  const splitRecoveryPage = (
    continuationIds: string[],
  ) => event({
    type: "history",
    sid: splitRecoverySid,
    session_id: splitRecoverySid,
    revision: "recovered-split-r1",
    generation: "recovered-split-generation",
    build_seq: 1,
    live_seq: 1,
    detail: "summary",
    authoritative: true,
    has_more: true,
    newest_id: splitRecoveryHistory,
    in_progress: true,
    compaction_continuation_turn_ids: continuationIds,
    events: [],
    turns: [{
      id: splitRecoveryHistory,
      clientMsgId: splitRecoveryMsg,
      prompt: "continue the oversized active task",
      forkPointId: splitRecoveryStream,
      done: true,
      interrupted: true,
      error: "本次回复已打断。",
      blocks: [],
    }],
  });
  let splitRecoveryState = reduce(splitRecoveryBase, {
    type: "event", event: splitRecoveryBinding,
  });
  splitRecoveryState = reduce(splitRecoveryState, {
    type: "event",
    event: splitRecoveryPage([
      splitRecoveryControl,
      splitRecoveryStream,
    ]),
  });
  assert.deepEqual({
    turnCount: splitRecoveryState.runtimes[splitRecoverySid].turns.length,
    done: splitRecoveryState.runtimes[splitRecoverySid].turns[0].done,
    interrupted: splitRecoveryState.runtimes[splitRecoverySid].turns[0]
      .interrupted,
    error: splitRecoveryState.runtimes[splitRecoverySid].turns[0].error,
    forkPointId: splitRecoveryState.runtimes[splitRecoverySid].turns[0]
      .forkPointId,
    liveTaskId: splitRecoveryState.runtimes[splitRecoverySid].turns[0]
      .liveTaskId,
  }, {
    turnCount: 1,
    done: false,
    interrupted: undefined,
    error: undefined,
    forkPointId: splitRecoveryStream,
    liveTaskId: splitRecoveryControl,
  }, "exact control/stream proof reopens one History row without replacing its stream identity");
  for (const recoveredEvent of [
    event({
      type: "process",
      sid: splitRecoverySid,
      seq: 2,
      item_id: "recovered-split-process",
      kind: "command",
      phase: "end",
      status: "succeeded",
      turn_id: splitRecoveryControl,
      title: "continued after wrapper restart",
    }),
    event({
      type: "assistant_msg_start",
      sid: splitRecoverySid,
      seq: 3,
      message_id: "recovered-split-answer",
      channel: "commentary",
    }),
    event({
      type: "delta",
      sid: splitRecoverySid,
      seq: 4,
      message_id: "recovered-split-answer",
      channel: "commentary",
      text: "still working",
    }),
  ]) {
    splitRecoveryState = reduce(splitRecoveryState, {
      type: "event", event: recoveredEvent,
    });
  }
  assert.equal(
    splitRecoveryState.runtimes[splitRecoverySid].turns.length,
    1,
    "normalized control-id output stays on the exact rollout History row",
  );
  assert.deepEqual(
    splitRecoveryState.runtimes[splitRecoverySid].turns[0].blocks.map(
      (block: Block) => block.kind === "process"
        ? block.item_id : block.kind === "text" ? block.message_id : null,
    ),
    ["recovered-split-process", "recovered-split-answer"],
  );

  let unprovenSplitState = reduce(splitRecoveryBase, {
    type: "event", event: splitRecoveryBinding,
  });
  unprovenSplitState = reduce(unprovenSplitState, {
    type: "event",
    event: splitRecoveryPage([]),
  });
  assert.deepEqual({
    done: unprovenSplitState.runtimes[splitRecoverySid].turns[0].done,
    interrupted: unprovenSplitState.runtimes[splitRecoverySid].turns[0]
      .interrupted,
    liveTaskId: unprovenSplitState.runtimes[splitRecoverySid].turns[0]
      .liveTaskId,
  }, {
    done: true,
    interrupted: true,
    liveTaskId: undefined,
  }, "missing continuation evidence cannot reopen or alias a real interrupted turn");

  let streamOnlySplitState = reduce(splitRecoveryBase, {
    type: "event", event: splitRecoveryBinding,
  });
  streamOnlySplitState = reduce(streamOnlySplitState, {
    type: "event",
    event: splitRecoveryPage([splitRecoveryStream]),
  });
  assert.deepEqual({
    done: streamOnlySplitState.runtimes[splitRecoverySid].turns[0].done,
    interrupted: streamOnlySplitState.runtimes[splitRecoverySid].turns[0]
      .interrupted,
    liveTaskId: streamOnlySplitState.runtimes[splitRecoverySid].turns[0]
      .liveTaskId,
  }, {
    done: false,
    interrupted: undefined,
    liveTaskId: undefined,
  }, "a stream-only compaction proof cannot manufacture the control/stream alias");

  // A cold browser can receive the client-only, unsequenced recovery binding
  // before both live content and History. The first real content may open an
  // internal empty-prompt owner, but canonical History must later fill that
  // exact row rather than painting a second transcript copy.
  const coldRecoveredSid = "cold-recovered-owner-binding";
  let coldRecoveredState = {
    ...initialState,
    focusedSid: coldRecoveredSid,
    runtimes: {
      [coldRecoveredSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "cold-recovered-generation",
        historyGeneration: "cold-recovered-generation",
        legacyLiveFallbackBlocked: true,
      },
    },
  };
  for (const recoveredEvent of [
    event({
      type: "turn_binding",
      sid: coldRecoveredSid,
      msg_id: "cold-client-message",
      turn_id: "cold-native-turn",
    }),
    event({
      type: "state",
      sid: coldRecoveredSid,
      seq: 2,
      state: "running",
      msg_id: "cold-client-message",
    }),
    event({
      type: "assistant_msg_start",
      sid: coldRecoveredSid,
      seq: 3,
      message_id: "cold-live-answer",
      channel: "commentary",
    }),
    event({
      type: "delta",
      sid: coldRecoveredSid,
      seq: 4,
      message_id: "cold-live-answer",
      channel: "commentary",
      text: "recovering",
    }),
  ]) {
    coldRecoveredState = reduce(coldRecoveredState, {
      type: "event",
      event: recoveredEvent,
    });
  }
  assert.equal(coldRecoveredState.runtimes[coldRecoveredSid].turns.length, 1);
  assert.equal(coldRecoveredState.runtimes[coldRecoveredSid].turns[0].prompt, "");
  coldRecoveredState = reduce(coldRecoveredState, {
    type: "event",
    event: event({
      type: "history",
      sid: coldRecoveredSid,
      session_id: coldRecoveredSid,
      revision: "cold-recovered-r1",
      generation: "cold-recovered-generation",
      build_seq: 1,
      live_seq: 4,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: "cold-official-message",
      in_progress: true,
      events: [],
      turns: [{
        id: "cold-official-message",
        prompt: "continue the active task",
        forkPointId: "cold-native-turn",
        done: false,
        blocks: [],
      }],
    }),
  });
  assert.equal(
    coldRecoveredState.runtimes[coldRecoveredSid].turns.length,
    1,
    "History arriving after recovery content preserves one projection",
  );
  assert.equal(
    coldRecoveredState.runtimes[coldRecoveredSid].turns[0].prompt,
    "continue the active task",
  );
  assert.equal(
    coldRecoveredState.runtimes[coldRecoveredSid].turns[0].blocks[0].kind
      === "text"
      ? coldRecoveredState.runtimes[coldRecoveredSid].turns[0].blocks[0].text
      : null,
    "recovering",
    "a lagging active History head keeps its newer bound live tail",
  );

  const historyFirstRecoverySid = "history-before-recovery-binding";
  let historyFirstRecoveryState = {
    ...initialState,
    focusedSid: historyFirstRecoverySid,
    runtimes: {
      [historyFirstRecoverySid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "history-first-generation",
        historyGeneration: "history-first-generation",
        legacyLiveFallbackBlocked: true,
      },
    },
  };
  historyFirstRecoveryState = reduce(historyFirstRecoveryState, {
    type: "event",
    event: event({
      type: "history",
      sid: historyFirstRecoverySid,
      session_id: historyFirstRecoverySid,
      revision: "history-first-r1",
      generation: "history-first-generation",
      build_seq: 1,
      live_seq: 0,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: "history-first-official",
      in_progress: true,
      events: [],
      turns: [{
        id: "history-first-official",
        prompt: "keep going",
        forkPointId: "history-first-native",
        done: false,
        blocks: [],
      }],
    }),
  });
  for (const recoveredEvent of [
    event({
      type: "turn_binding",
      sid: historyFirstRecoverySid,
      seq: 1,
      msg_id: "history-first-client",
      turn_id: "history-first-native",
    }),
    event({
      type: "state",
      sid: historyFirstRecoverySid,
      seq: 2,
      state: "running",
      msg_id: "history-first-client",
    }),
    event({
      type: "assistant_msg_start",
      sid: historyFirstRecoverySid,
      seq: 3,
      message_id: "history-first-tail",
      channel: "final",
    }),
  ]) {
    historyFirstRecoveryState = reduce(historyFirstRecoveryState, {
      type: "event",
      event: recoveredEvent,
    });
  }
  assert.equal(
    historyFirstRecoveryState.runtimes[historyFirstRecoverySid].turns.length,
    1,
    "History-before-binding recovery also retains one exact owner",
  );
  assert.equal(
    historyFirstRecoveryState.runtimes[historyFirstRecoverySid].turns[0]
      .blocks[0].kind === "text"
      ? historyFirstRecoveryState.runtimes[historyFirstRecoverySid].turns[0]
        .blocks[0].message_id
      : null,
    "history-first-tail",
  );

  // An older binding for a completed predecessor cannot steal a newer steer
  // owner even though both segments intentionally share one native task id.
  const staleBindingSid = "stale-binding-after-steer";
  let staleBindingState = {
    ...initialState,
    focusedSid: staleBindingSid,
    runtimes: {
      [staleBindingSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "stale-binding-generation",
        historyGeneration: "stale-binding-generation",
        legacyLiveFallbackBlocked: true,
        liveOwner: { turnId: "current-steer", seq: 20 },
        turns: [{
          id: "initial-message",
          clientMsgId: "initial-message",
          prompt: "start",
          forkPointId: "shared-steer-task",
          blocks: [],
          done: true,
        }, {
          id: "current-steer",
          clientMsgId: "current-steer",
          prompt: "continue differently",
          liveTaskId: "shared-steer-task",
          blocks: [],
          done: false,
        }, {
          id: "stale-binding-compaction-orphan",
          prompt: "",
          blocks: [{ ...orphanCompactionBlock,
            item_id: "stale-binding-compaction",
            turn_id: "shared-steer-task" }],
          done: false,
        }],
      },
    },
  };
  staleBindingState = reduce(staleBindingState, {
    type: "event",
    event: event({
      type: "turn_binding",
      sid: staleBindingSid,
      seq: 10,
      msg_id: "initial-message",
      turn_id: "shared-steer-task",
    }),
  });
  assert.equal(
    staleBindingState.runtimes[staleBindingSid].turns.some((turn: Turn) =>
      turn.id === "stale-binding-compaction-orphan"),
    true,
    "a stale predecessor binding cannot claim or delete an ambiguous compaction",
  );
  staleBindingState = reduce(staleBindingState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: staleBindingSid,
      seq: 21,
      message_id: "current-steer-answer",
      channel: "final",
    }),
  });
  assert.equal(staleBindingState.runtimes[staleBindingSid].turns.length, 3);
  assert.equal(staleBindingState.runtimes[staleBindingSid].turns.find(
    (turn: Turn) => turn.id === "initial-message")?.done, true);
  const currentSteer = staleBindingState.runtimes[staleBindingSid].turns.find(
    (turn: Turn) => turn.id === "current-steer");
  assert.equal(
    currentSteer?.blocks[0].kind
      === "text"
      ? currentSteer.blocks[0].message_id
      : null,
    "current-steer-answer",
    "an older binding cannot reopen the completed predecessor",
  );

  const completeDetailText = "complete detail payload after the summary prefix";
  const refreshedLoadedDetail = mergeAuthoritativeTurnDetail({
    id: "loaded-detail-refresh",
    prompt: "inspect a long answer",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "loaded-detail-final",
      channel: "final",
      text: "complete detail…（完整内容请展开本轮过程）",
      done: true,
    }],
  }, {
    id: "loaded-detail-refresh",
    prompt: "inspect a long answer",
    done: true,
    detailLoaded: true,
    blocks: [{
      kind: "text",
      message_id: "loaded-detail-final",
      channel: "final",
      text: completeDetailText,
      done: true,
    }],
  });
  assert.equal(
    refreshedLoadedDetail.blocks[0]?.kind === "text"
      ? refreshedLoadedDetail.blocks[0].text : null,
    completeDetailText,
    "a later summary refresh must preserve an already-loaded complete answer",
  );

  const repeatedCanonicalText = "echoecho";
  const repeatedCanonical = mergeInitialHistory([{
    id: "repeated-prose-turn",
    prompt: "repeat naturally",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "repeated-prose-message",
      channel: "commentary",
      text: repeatedCanonicalText,
      done: true,
    }],
  }], [{
    id: "repeated-prose-turn",
    prompt: "repeat naturally",
    done: true,
    blocks: [{
      kind: "text",
      message_id: "repeated-prose-message",
      channel: "commentary",
      text: repeatedCanonicalText + repeatedCanonicalText,
      done: true,
    }],
  }]);
  assert.equal(
    (repeatedCanonical[0].blocks[0] as Block & { kind: "text" }).text,
    repeatedCanonicalText,
    "completed exact ids use History authority without inspecting prose shape",
  );

  const unfinishedProjection = mergeInitialHistory([{
    id: "unfinished-replay-turn",
    prompt: "keep streaming",
    done: false,
    blocks: [{
      kind: "text",
      message_id: "unfinished-replay-message",
      channel: "commentary",
      text: "first ",
      done: false,
    }],
  }], [{
    id: "unfinished-replay-turn",
    prompt: "keep streaming",
    done: false,
    blocks: [{
      kind: "text",
      message_id: "unfinished-replay-message",
      channel: "commentary",
      text: "second",
      done: false,
    }, {
      kind: "text",
      message_id: "different-native-message",
      channel: "commentary",
      text: "separate",
      done: false,
    }],
  }]);
  assert.deepEqual(
    unfinishedProjection[0].blocks
      .filter((block): block is Block & { kind: "text" } =>
        block.kind === "text")
      .map((block) => [block.message_id, block.text]),
    [
      ["unfinished-replay-message", "first second"],
      ["different-native-message", "separate"],
    ],
    "unfinished exact ids still append while different ids remain distinct",
  );

  // Exact production reconnect race: IndexedDB paints an unfinished row whose
  // stable assistant item is already present, then current-generation replay
  // recreates the user/binding row after that old user boundary fell outside
  // the ring. The assistant replay refreshes the cached row, while History
  // paints the same canonical item onto the newly bound row. Both rows are
  // live, but only the bound row is the current owner. Authoritative History
  // must absorb the older projection even though it is not the physical tail.
  const cachedReplaySid = "cached-replay-before-bound-owner";
  const cachedReplayMessage =
    "msg_0a40ae38f7d2caa1016a7d977a67408191b30c594107a3495c";
  const cachedReplayUser = "msg_019ffa87-85cf-7f62-81f0-8a04cb4e782a";
  const cachedReplayNative = "019ffa87-85a7-7093-8a2a-ce906d7b1a59";
  let cachedReplayState = reduce({
    ...initialState,
    focusedSid: cachedReplaySid,
    sessions: [{
      session_id: cachedReplaySid,
      engine: "codex" as const,
      space: "code" as const,
    }],
  }, {
    type: "hydrate_cache",
    sid: cachedReplaySid,
    revision: "cached-replay-r1",
    generation: "cached-replay-g1",
    turns: [{
      id: "stale-indexeddb-projection",
      prompt: "修复问题。",
      done: false,
      blocks: [{
        kind: "text",
        message_id: cachedReplayMessage,
        channel: "commentary",
        text: "",
        done: false,
      }],
    }],
  });
  for (const reconnectEvent of [
    event({
      type: "snapshot",
      sid: cachedReplaySid,
      cc_session_id: cachedReplaySid,
      generation: "cached-replay-g1",
      state: "running",
    }),
    event({
      type: "replay_start",
      sid: cachedReplaySid,
      generation: "cached-replay-g1",
      from_seq: 19,
      to_seq: 23,
      truncated: false,
    }),
    event({
      type: "user_msg",
      sid: cachedReplaySid,
      seq: 20,
      msg_id: cachedReplayUser,
      client_msg_id: "cached-replay-browser-message",
      prompt: "修复问题。",
    }),
    event({
      type: "turn_binding",
      sid: cachedReplaySid,
      seq: 21,
      msg_id: cachedReplayUser,
      turn_id: cachedReplayNative,
    }),
    event({
      type: "assistant_msg_start",
      sid: cachedReplaySid,
      seq: 22,
      message_id: cachedReplayMessage,
      channel: "commentary",
    }),
    event({
      type: "delta",
      sid: cachedReplaySid,
      seq: 23,
      message_id: cachedReplayMessage,
      channel: "commentary",
      text: "浏览器全量测试仍在后台正常运行",
    }),
    event({
      type: "assistant_msg_end",
      sid: cachedReplaySid,
      seq: 24,
      message_id: cachedReplayMessage,
      channel: "commentary",
    }),
    event({
      type: "replay_end",
      sid: cachedReplaySid,
      to_seq: 24,
      truncated: false,
    }),
  ]) {
    cachedReplayState = reduce(cachedReplayState, {
      type: "event",
      event: reconnectEvent,
    });
  }
  assert.deepEqual(
    cachedReplayState.runtimes[cachedReplaySid].turns.map(
      (turn: Turn) => turn.id),
    ["stale-indexeddb-projection", cachedReplayUser],
    "replay first exposes the cached item row and the separately bound user row",
  );
  cachedReplayState = reduce(cachedReplayState, {
    type: "event",
    event: event({
      type: "history",
      sid: cachedReplaySid,
      session_id: cachedReplaySid,
      revision: "cached-replay-r1",
      generation: "cached-replay-g1",
      build_seq: 2,
      live_seq: 24,
      detail: "summary",
      authoritative: true,
      has_more: true,
      oldest_id: cachedReplayUser,
      newest_id: cachedReplayUser,
      in_progress: true,
      events: [],
      turns: [{
        id: cachedReplayUser,
        prompt: "修复问题。",
        forkPointId: cachedReplayNative,
        done: true,
        blocks: [{
          kind: "text",
          message_id: cachedReplayMessage,
          channel: "commentary",
          text: "浏览器全量测试仍在后台正常运行",
          done: true,
        }],
      }],
    }),
  });
  const cachedReplayTurns =
    cachedReplayState.runtimes[cachedReplaySid].turns;
  assert.deepEqual(
    cachedReplayTurns.map((turn: Turn) => ({
      id: turn.id,
      done: turn.done,
      messages: turn.blocks.flatMap((block: Block) =>
        block.kind === "text" ? [block.message_id] : []),
    })),
    [{
      id: cachedReplayUser,
      done: false,
      messages: [cachedReplayMessage],
    }],
    "running History collapses the non-tail cached projection into its bound owner",
  );
  assert.deepEqual(
    cachedReplayState.runtimes[cachedReplaySid].liveOwner,
    { turnId: cachedReplayUser, seq: 21 },
    "the surviving row remains the exact ordered live owner",
  );
  assert.deepEqual(
    cachedReplayState.runtimes[cachedReplaySid].liveDetailTurnIds,
    [cachedReplayUser],
    "live detail provenance follows the row which survives cache self-healing",
  );
  cachedReplayState = reduce(cachedReplayState, {
    type: "event",
    event: event({
      type: "history",
      sid: cachedReplaySid,
      session_id: cachedReplaySid,
      revision: "cached-replay-r1",
      generation: "cached-replay-g1",
      build_seq: 3,
      live_seq: 24,
      detail: "summary",
      authoritative: true,
      has_more: true,
      oldest_id: cachedReplayUser,
      newest_id: cachedReplayUser,
      in_progress: true,
      events: [],
      turns: [{
        id: cachedReplayUser,
        prompt: "修复问题。",
        forkPointId: cachedReplayNative,
        done: true,
        blocks: [{
          kind: "text",
          message_id: cachedReplayMessage,
          channel: "commentary",
          text: "浏览器全量测试仍在后台正常运行",
          done: true,
        }],
      }],
    }),
  });
  assert.equal(
    cachedReplayState.runtimes[cachedReplaySid].turns.length,
    1,
    "a second authoritative refresh cannot recreate the repaired projection",
  );
  cachedReplayState = reduce(cachedReplayState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: cachedReplaySid,
      seq: 25,
      message_id: "cached-replay-later-message",
      channel: "commentary",
    }),
  });
  assert.deepEqual(
    cachedReplayState.runtimes[cachedReplaySid].turns[0].blocks.flatMap(
      (block: Block) => block.kind === "text" ? [block.message_id] : []),
    [cachedReplayMessage, "cached-replay-later-message"],
    "later native items keep targeting the surviving row exactly once",
  );

  // A polluted cache can also leave an unrelated unfinished row after the
  // current owner. The explicit liveOwner binding, not physical array order,
  // decides which row may inherit History's running lifecycle.
  const currentOwnerBeforeTail = mergeInitialHistory(
    [{
      id: "bound-history-owner",
      prompt: "current request",
      forkPointId: "bound-native-task",
      done: true,
      blocks: [{
        kind: "text",
        message_id: "bound-current-item",
        channel: "commentary",
        text: "current output",
        done: true,
      }],
    }],
    [{
      id: "bound-current-owner",
      historyTurnId: "bound-history-owner",
      liveTaskId: "bound-native-task",
      prompt: "current request",
      done: false,
      blocks: [{
        kind: "text",
        message_id: "bound-current-item",
        channel: "commentary",
        text: "current output",
        done: true,
      }],
    }, {
      id: "unrelated-open-tail",
      prompt: "different request",
      done: false,
      blocks: [{
        kind: "text",
        message_id: "unrelated-open-item",
        channel: "commentary",
        text: "different output",
        done: false,
      }],
    }],
    {
      preserveLiveTailOpen: true,
      newestHistoryId: "bound-history-owner",
      activeOwnerId: "bound-current-owner",
      reconcileReplayOrphans: true,
    },
  );
  assert.deepEqual(
    currentOwnerBeforeTail.map((turn) => [turn.id, turn.done]),
    [["bound-current-owner", false], ["unrelated-open-tail", false]],
    "an explicit non-tail owner stays active without absorbing another row",
  );

  const duplicatePrimaryActiveProjection = mergeInitialHistory(
    [{
      id: "duplicate-primary-history",
      prompt: "keep both duplicate blocks visible",
      done: true,
      blocks: [{
        kind: "text",
        message_id: "duplicate-primary-message",
        channel: "commentary",
        text: "one canonical occurrence",
        done: true,
      }],
    }],
    [{
      id: "duplicate-primary-cache",
      prompt: "keep both duplicate blocks visible",
      done: false,
      blocks: [0, 1].map(() => ({
        kind: "text" as const,
        message_id: "duplicate-primary-message",
        channel: "commentary" as const,
        text: "one canonical occurrence",
        done: true,
      })),
    }, {
      id: "duplicate-primary-owner",
      historyTurnId: "duplicate-primary-history",
      prompt: "keep both duplicate blocks visible",
      done: false,
      blocks: [{
        kind: "text",
        message_id: "duplicate-primary-message",
        channel: "commentary",
        text: "one canonical occurrence",
        done: true,
      }],
    }],
    {
      preserveLiveTailOpen: true,
      newestHistoryId: "duplicate-primary-history",
      activeOwnerId: "duplicate-primary-owner",
      reconcileReplayOrphans: true,
    },
  );
  assert.equal(
    duplicatePrimaryActiveProjection.some((turn) =>
      turn.id === "duplicate-primary-cache"),
    true,
    "duplicate stable ids inside one projection remain unsafe to self-heal",
  );

  const distinctStableItemsStayDistinct = mergeInitialHistory(
    [{
      id: "distinct-items-history",
      prompt: "two legitimate updates",
      done: true,
      blocks: [{
        kind: "text",
        message_id: "distinct-item-a",
        channel: "commentary",
        text: "same visible words",
        done: true,
      }, {
        kind: "text",
        message_id: "distinct-item-b",
        channel: "commentary",
        text: "same visible words",
        done: true,
      }],
    }],
    [{
      id: "distinct-items-cache-a",
      prompt: "two legitimate updates",
      done: false,
      blocks: [{
        kind: "text",
        message_id: "distinct-item-a",
        channel: "commentary",
        text: "same visible words",
        done: true,
      }],
    }, {
      id: "distinct-items-owner",
      historyTurnId: "distinct-items-history",
      prompt: "two legitimate updates",
      done: false,
      blocks: [{
        kind: "text",
        message_id: "distinct-item-b",
        channel: "commentary",
        text: "same visible words",
        done: true,
      }],
    }],
    {
      preserveLiveTailOpen: true,
      newestHistoryId: "distinct-items-history",
      activeOwnerId: "distinct-items-owner",
      reconcileReplayOrphans: true,
    },
  );
  assert.deepEqual(
    distinctStableItemsStayDistinct[0].blocks.flatMap((block) =>
      block.kind === "text" ? [block.message_id] : []),
    ["distinct-item-a", "distinct-item-b"],
    "self-healing uses stable ids and preserves equal-text native occurrences",
  );

  // The first running summary can race ahead of every visible item. With no
  // block identity yet it is correct to keep the authoritative row and the
  // accepted local steer separate. Once the local row receives native items
  // and the completed summary returns those same exact item ids, however, the
  // terminal merge must collapse the stale empty shell instead of leaving an
  // extra completed process row at the physical bottom of the conversation.
  const emptyActiveSid = "empty-active-steer-terminal-binding";
  const emptyActiveNative = "empty-active-native-task";
  let emptyActiveState = reduce({
    ...initialState,
    focusedSid: emptyActiveSid,
    runtimes: {
      [emptyActiveSid]: {
        ...createRuntime(),
        state: "running" as const,
        syncReady: true,
        controlGeneration: "empty-active-g1",
        historyFence: 10,
        liveOwner: {
          turnId: "empty-active-client-steer",
          seq: 11,
        },
        turns: [{
          id: "empty-active-client-steer",
          clientMsgId: "empty-active-client-steer",
          liveTaskId: emptyActiveNative,
          prompt: "guide before the first item",
          done: false,
          ts: 20_001,
          blocks: [],
        }],
      },
    },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: emptyActiveSid,
      session_id: emptyActiveSid,
      revision: "empty-active-r1",
      generation: "empty-active-g1",
      build_seq: 1,
      live_seq: 10,
      detail: "summary",
      authoritative: true,
      has_more: true,
      oldest_id: "empty-active-history-steer",
      newest_id: "empty-active-history-steer",
      in_progress: true,
      events: [],
      turns: [{
        id: "empty-active-history-steer",
        prompt: "guide before the first item",
        forkPointId: emptyActiveNative,
        done: true,
        ts: 20_000,
        blocks: [],
        detailEventCount: 1,
        detailLoaded: false,
      }],
    }),
  });
  assert.equal(
    emptyActiveState.runtimes[emptyActiveSid].turns.length,
    2,
    "an item-free running snapshot remains ambiguous until native blocks arrive",
  );
  assert.equal(
    emptyActiveState.runtimes[emptyActiveSid].turns.filter(
      (turn: Turn) => !turn.done).length,
    1,
    "the provisional History shell cannot render a second running indicator",
  );
  for (const liveEvent of [
    event({
      type: "process",
      sid: emptyActiveSid,
      seq: 11,
      item_id: "empty-active-shared-process",
      kind: "command",
      phase: "end",
      status: "succeeded",
      turn_id: emptyActiveNative,
      title: "ran the guided command",
    }),
    event({
      type: "assistant_msg_start",
      sid: emptyActiveSid,
      seq: 12,
      message_id: "empty-active-shared-final",
      channel: "final",
    }),
    event({
      type: "delta",
      sid: emptyActiveSid,
      seq: 13,
      message_id: "empty-active-shared-final",
      channel: "final",
      text: "finished the guided work",
    }),
    event({
      type: "assistant_msg_end",
      sid: emptyActiveSid,
      seq: 14,
      message_id: "empty-active-shared-final",
      channel: "final",
    }),
    event({
      type: "turn_end",
      sid: emptyActiveSid,
      seq: 15,
      turn_id: emptyActiveNative,
      result: { subtype: "success", duration_ms: 5, is_error: false },
    }),
  ]) {
    emptyActiveState = reduce(emptyActiveState, {
      type: "event",
      event: liveEvent,
    });
  }
  const preTerminalRows =
    emptyActiveState.runtimes[emptyActiveSid].turns;
  assert.equal(
    preTerminalRows[0].blocks.some((block: Block) => block.kind === "text"
      && block.message_id === "empty-active-shared-final"),
    false,
    "an aliasless History shell cannot steal the post-steer final answer",
  );
  assert.deepEqual(
    preTerminalRows[1].blocks.map((block: Block) => block.kind === "text"
      ? block.message_id
      : block.kind === "tool" ? block.tool_use_id : block.item_id),
    ["empty-active-shared-process", "empty-active-shared-final"],
    "post-steer process and final output remain on one live UI row",
  );
  emptyActiveState = reduce(emptyActiveState, {
    type: "event",
    event: event({
      type: "history",
      sid: emptyActiveSid,
      session_id: emptyActiveSid,
      revision: "empty-active-r1",
      generation: "empty-active-g1",
      build_seq: 2,
      live_seq: 15,
      detail: "summary",
      authoritative: true,
      has_more: true,
      oldest_id: "empty-active-history-steer",
      newest_id: "empty-active-history-steer",
      in_progress: false,
      events: [],
      turns: [{
        id: "empty-active-history-steer",
        prompt: "guide before the first item",
        forkPointId: emptyActiveNative,
        done: true,
        ts: 20_000,
        doneTs: 25_000,
        durationMs: 5,
        blocks: [{
          kind: "process",
          item_id: "empty-active-shared-process",
          processKind: "command",
          phase: "end",
          status: "succeeded",
          turn_id: emptyActiveNative,
          title: "ran the guided command",
          done: true,
        }, {
          kind: "text",
          message_id: "empty-active-shared-final",
          channel: "final",
          text: "finished the guided work",
          done: true,
        }],
        detailEventCount: 2,
        detailLoaded: false,
      }],
    }),
  });
  assert.deepEqual(
    emptyActiveState.runtimes[emptyActiveSid].turns.map((turn: Turn) => ({
      id: turn.id,
      historyTurnId: turn.historyTurnId,
      done: turn.done,
      prompt: turn.prompt,
      blockIds: turn.blocks.map((block: Block) => block.kind === "text"
        ? block.message_id
        : block.kind === "tool" ? block.tool_use_id : block.item_id),
    })),
    [{
      id: "empty-active-client-steer",
      historyTurnId: "empty-active-history-steer",
      done: true,
      prompt: "guide before the first item",
      blockIds: [
        "empty-active-shared-process",
        "empty-active-shared-final",
      ],
    }],
    "terminal History binds the now-proven live row and removes its stale active shell",
  );

  const ambiguousActiveHeadSid = "ambiguous-active-history-head";
  let ambiguousActiveHeadState = reduce({
    ...initialState,
    focusedSid: ambiguousActiveHeadSid,
    runtimes: {
      [ambiguousActiveHeadSid]: {
        ...createRuntime(),
        state: "idle" as const,
        syncReady: true,
      },
    },
  }, {
    type: "event",
    event: event({
      type: "history",
      sid: ambiguousActiveHeadSid,
      session_id: ambiguousActiveHeadSid,
      revision: "ambiguous-active-r1",
      generation: "ambiguous-active-g1",
      build_seq: 1,
      live_seq: 20,
      detail: "summary",
      authoritative: true,
      has_more: false,
      newest_id: "missing-active-visible-id",
      in_progress: true,
      events: [],
      turns: [
        {
          id: "candidate-a",
          prompt: "first",
          forkPointId: "shared-active-native",
          done: true,
          blocks: [],
        },
        {
          id: "candidate-b",
          prompt: "second",
          forkPointId: "shared-active-native",
          done: true,
          blocks: [],
        },
      ],
    }),
  });
  assert.ok(
    ambiguousActiveHeadState.runtimes[ambiguousActiveHeadSid].turns.every(
      (turn: Turn) => turn.done),
    "an unmatched active cursor cannot guess between visible steer segments",
  );
  ambiguousActiveHeadState = reduce(ambiguousActiveHeadState, {
    type: "event",
    event: event({
      type: "assistant_msg_start",
      sid: ambiguousActiveHeadSid,
      seq: 21,
      message_id: "unbound-after-refresh",
      channel: "commentary",
    }),
  });
  assert.equal(
    ambiguousActiveHeadState.runtimes[ambiguousActiveHeadSid].turns.length,
    3,
    "missing authoritative ownership fails closed instead of merging by recency",
  );

  const richSid = "rich-process";
  state = {
    ...state,
    focusedSid: richSid,
    runtimes: {
      ...state.runtimes,
      [richSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: richSid, prompt: "实现功能",
    msg_id: "rich-turn", ts: 30_000 });
  const richEvents = [
    event({ type: "assistant_msg_start", sid: richSid, ts: 30, message_id: "comment-1", channel: "commentary" }),
    event({ type: "delta", sid: richSid, ts: 30, message_id: "comment-1", channel: "commentary", text: "先检查代码。" }),
    event({ type: "assistant_msg_end", sid: richSid, ts: 30, message_id: "comment-1", channel: "commentary" }),
    event({ type: "tool_use", sid: richSid, ts: 30, message_id: "comment-1", tool_use_id: "cmd-1",
      tool: "shell", category: "command", input: { command: "npm test" } }),
    event({ type: "tool_delta", sid: richSid, ts: 31, tool_use_id: "cmd-1", stream: "output", delta: "ok\n" }),
    event({ type: "tool_result", sid: richSid, ts: 31, tool_use_id: "cmd-1", content: "ok\n",
      is_error: false, status: "succeeded", exit_code: 0, duration_ms: 1250 }),
    event({ type: "turn_plan", sid: richSid, ts: 31, item_id: "plan-1", turn_id: "turn-rich",
      explanation: "执行计划", plan: [{ step: "检查", status: "completed" }] }),
    event({ type: "process", sid: richSid, ts: 31, item_id: "hook-1", kind: "hook", phase: "end",
      status: "succeeded", turn_id: "turn-rich", title: "Hook 完成", duration_ms: 20 }),
    event({ type: "turn_diff", sid: richSid, ts: 32, item_id: "diff-1", turn_id: "turn-rich",
      diff: "diff --git a/a b/a" }),
    event({ type: "assistant_msg_start", sid: richSid, message_id: "final-1", channel: "final" }),
    event({ type: "delta", sid: richSid, message_id: "final-1", channel: "final", text: "已经完成。" }),
    event({ type: "assistant_msg_end", sid: richSid, message_id: "final-1", channel: "final" }),
    event({ type: "turn_end", sid: richSid, ts: 33, turn_id: "turn-rich",
      result: { subtype: "success", duration_ms: 3000, is_error: false } }),
  ];
  for (const richEvent of richEvents) {
    state = reduce(state, { type: "event", event: richEvent });
  }
  const richTurn = state.runtimes[richSid].turns[0];
  assert.equal(richTurn.durationMs, 3000);
  assert.deepEqual(richTurn.blocks.filter((block: { kind: string }) => block.kind === "text")
    .map((block: { channel?: string }) => block.channel), ["commentary", "final"]);
  const commandBlock = richTurn.blocks.find((block: { kind: string }) => block.kind === "tool") as {
    output?: string; result?: { exit_code?: number; duration_ms?: number };
  };
  assert.equal(commandBlock.output, "ok\n");
  assert.equal(commandBlock.result?.exit_code, 0);
  assert.equal(commandBlock.result?.duration_ms, 1250);
  assert.equal(richTurn.blocks.filter((block: { kind: string }) => block.kind === "process").length, 3);

  const clearedDiffSid = "cleared-file-diff";
  let clearedDiffState = reduce({
    ...initialState,
    focusedSid: clearedDiffSid,
    runtimes: { [clearedDiffSid]: createRuntime() },
  }, {
    type: "query_sent", sid: clearedDiffSid,
    prompt: "edit", msg_id: "diff-turn", ts: 31_000,
  });
  for (const diffEvent of [
    event({
      type: "tool_use", sid: clearedDiffSid, message_id: "diff-message",
      tool_use_id: "diff-tool", tool: "apply_patch", category: "file",
      input: { file_paths: ["/repo/a.py"] },
    }),
    event({
      type: "tool_delta", sid: clearedDiffSid, tool_use_id: "diff-tool",
      stream: "diff", delta: "@@ -1 +1 @@\n-old\n+first",
    }),
    event({
      type: "tool_result", sid: clearedDiffSid, tool_use_id: "diff-tool",
      content: "", is_error: false, status: "succeeded", diff: null,
    }),
  ]) {
    clearedDiffState = reduce(clearedDiffState, {
      type: "event", event: diffEvent,
    });
  }
  const clearedDiffBlock = clearedDiffState.runtimes[clearedDiffSid]
    .turns[0].blocks.find((block: Block) => block.kind === "tool") as {
      diff?: string;
    };
  assert.equal(clearedDiffBlock.diff, undefined,
    "an authoritative empty terminal snapshot clears the stale live diff");

  // Claude can reveal only at Result that a stop_reason=null text block was the
  // final answer. A repeated End for the same id must reclassify in place, while
  // an End for an unknown id must never manufacture an empty text block/turn.
  const correctedSid = "claude-final-correction";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [correctedSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: correctedSid, prompt: "回答",
    msg_id: "corrected-turn", ts: 30_500 });
  for (const correctionEvent of [
    event({ type: "assistant_msg_start", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "unknown" }),
    event({ type: "delta", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "unknown", text: "真实正文" }),
    event({ type: "assistant_msg_end", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "commentary" }),
    event({ type: "assistant_msg_end", sid: correctedSid,
      message_id: "ambiguous-answer", channel: "final" }),
    event({ type: "assistant_msg_end", sid: correctedSid,
      message_id: "missing-answer", channel: "final" }),
  ]) state = reduce(state, { type: "event", event: correctionEvent });
  const correctedTurns = state.runtimes[correctedSid].turns;
  assert.equal(correctedTurns.length, 1);
  assert.equal(correctedTurns[0].blocks.length, 1);
  const { liveOrder: correctedLiveOrder, ...correctedBlock } =
    correctedTurns[0].blocks[0];
  assert.equal(correctedLiveOrder, 0);
  assert.deepEqual(correctedBlock, {
    kind: "text", message_id: "ambiguous-answer", text: "真实正文",
    done: true, channel: "final",
  });

  // A compromised/noisy upstream must not grow an active turn without bound
  // before the normal completed-turn runtime eviction can run.
  const noisySid = "bounded-rich-delta";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [noisySid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: noisySid, prompt: "stream",
    msg_id: "noisy-turn", ts: 31_000 });
  state = reduce(state, { type: "event", event: event({
    type: "tool_use", sid: noisySid, message_id: "noisy-msg",
    tool_use_id: "noisy-tool", tool: "shell", category: "command", input: {},
  }) });
  const noisyChunk = "x".repeat(512 * 1024);
  for (let index = 0; index < 6; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "tool_delta", sid: noisySid, tool_use_id: "noisy-tool",
      stream: "output", delta: noisyChunk,
    }) });
  }
  const noisyTool = state.runtimes[noisySid].turns[0].blocks.find(
    (block: { kind: string }) => block.kind === "tool") as { output?: string };
  assert.equal(noisyTool.output?.length, 2 * 1024 * 1024);

  const { finalTextBlocks, hasActiveProcess } = await reducerHarness.ssrLoadModule(
    "/src/process-blocks.ts");
  state = reduce(state, { type: "event", event: event({
    type: "error", sid: noisySid, msg_id: "noisy-turn",
    code: "engine_failed", message: "stream stopped",
  }) });
  const failedNoisyTurn = state.runtimes[noisySid].turns[0];
  assert.equal(failedNoisyTurn.done, true);
  assert.equal(hasActiveProcess(failedNoisyTurn.blocks), false);
  const failedNoisyTool = failedNoisyTurn.blocks.find(
    (block: { kind: string }) => block.kind === "tool") as {
      done: boolean; result?: { is_error: boolean; status?: string | null };
    };
  assert.equal(failedNoisyTool.done, true);
  assert.equal(failedNoisyTool.result?.is_error, true);
  assert.equal(failedNoisyTool.result?.status, "failed");

  // A goal/automatic continuation can produce thousands of distinct items in
  // one still-running turn. Bound the item count (not only each item's bytes),
  // retain known final answers and newest live activity, and keep terminal
  // updates working for retained blocks.
  const boundedBlocksSid = "bounded-turn-blocks";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [boundedBlocksSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: boundedBlocksSid,
    prompt: "long goal", msg_id: "bounded-block-turn", ts: 31_500 });
  for (const finalEvent of [
    event({ type: "assistant_msg_start", sid: boundedBlocksSid,
      message_id: "early-final", channel: "final" }),
    event({ type: "delta", sid: boundedBlocksSid,
      message_id: "early-final", channel: "final", text: "必须保留的最终答案" }),
    event({ type: "assistant_msg_end", sid: boundedBlocksSid,
      message_id: "early-final", channel: "final" }),
  ]) state = reduce(state, { type: "event", event: finalEvent });

  const generatedBlocks = MAX_TURN_BLOCKS + 50;
  for (let index = 0; index < generatedBlocks; index += 1) {
    const suffix = String(index);
    const events = index % 5 === 0 ? [
      event({ type: "assistant_msg_start", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, channel: "commentary" }),
      event({ type: "delta", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, channel: "commentary", text: suffix }),
      event({ type: "assistant_msg_end", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, channel: "commentary" }),
    ] : index % 5 === 1 ? [
      event({ type: "tool_use", sid: boundedBlocksSid,
        message_id: `comment-${suffix}`, tool_use_id: `tool-${suffix}`,
        tool: "shell", category: "command", input: { command: "true" } }),
      event({ type: "tool_result", sid: boundedBlocksSid,
        tool_use_id: `tool-${suffix}`, content: "ok", is_error: false,
        status: "succeeded" }),
    ] : index % 5 === 2 ? [
      event({ type: "process", sid: boundedBlocksSid,
        item_id: `process-${suffix}`, kind: "hook", phase: "end",
        status: "succeeded", title: `Hook ${suffix}` }),
    ] : index % 5 === 3 ? [
      event({ type: "turn_plan", sid: boundedBlocksSid,
        item_id: `plan-${suffix}`, explanation: suffix,
        plan: [{ step: suffix, status: "completed" }] }),
    ] : [
      event({ type: "turn_diff", sid: boundedBlocksSid,
        item_id: `diff-${suffix}`, diff: `+${suffix}` }),
    ];
    for (const generatedEvent of events) {
      state = reduce(state, { type: "event", event: generatedEvent });
    }
  }
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: boundedBlocksSid, item_id: "latest-active",
    kind: "agent", phase: "start", status: "running", title: "最新活动",
  }) });
  let boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  assert.equal(boundedBlockTurn.blocks.length, MAX_TURN_BLOCKS);
  assert.equal(boundedBlockTurn.blocks.filter((block: { kind: string; item_id?: string }) =>
    block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID).length, 0,
  "the routine live memory window must not paint an omission card");
  assert.ok((boundedBlockTurn.detailEventCount ?? 0) >= generatedBlocks);
  assert.equal(boundedBlockTurn.detailRestorePending, true,
    "the first live spill must request an authoritative bounded detail page");
  assert.ok(boundedBlockTurn.blocks.some(
    (block: { kind: string; message_id?: string; channel?: string }) =>
      block.kind === "text" && block.message_id === "early-final"
      && block.channel === "final"));
  assert.ok(boundedBlockTurn.blocks.some(
    (block: { kind: string; item_id?: string; done: boolean }) =>
      block.kind === "process" && block.item_id === "latest-active" && !block.done));
  for (const expectedId of ["comment-305", "tool-301", "process-302", "plan-303", "diff-304"]) {
    assert.ok(boundedBlockTurn.blocks.some((block: {
      kind: string; message_id?: string; tool_use_id?: string; item_id?: string;
    }) => block.message_id === expectedId || block.tool_use_id === expectedId
      || block.item_id === expectedId));
  }

  state = reduce(state, { type: "event", event: event({
    type: "process", sid: boundedBlocksSid, item_id: "latest-active",
    kind: "agent", phase: "end", status: "succeeded", title: "最新活动完成",
  }) });
  boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  const closedLatest = boundedBlockTurn.blocks.find((block: {
    kind: string; item_id?: string;
  }) => block.kind === "process" && block.item_id === "latest-active") as {
    done: boolean; status: string;
  };
  assert.equal(closedLatest.done, true);
  assert.equal(closedLatest.status, "succeeded");

  for (const finalEvent of [
    event({ type: "assistant_msg_start", sid: boundedBlocksSid,
      message_id: "late-final", channel: "final" }),
    event({ type: "delta", sid: boundedBlocksSid,
      message_id: "late-final", channel: "final", text: "最新最终答案" }),
    event({ type: "assistant_msg_end", sid: boundedBlocksSid,
      message_id: "late-final", channel: "final" }),
  ]) state = reduce(state, { type: "event", event: finalEvent });
  boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  assert.equal(boundedBlockTurn.blocks.length, MAX_TURN_BLOCKS);
  assert.ok(boundedBlockTurn.blocks.some((block: {
    kind: string; message_id?: string; channel?: string;
  }) => block.kind === "text" && block.message_id === "late-final"
    && block.channel === "final"));
  state = reduce(state, {
    type: "turn_detail_requested",
    sid: boundedBlocksSid,
    turnId: "bounded-block-turn",
    autoLoad: false,
  });
  state = reduce(state, { type: "event", event: event({
    type: "turn_end", sid: boundedBlocksSid, turn_id: "bounded-block-turn",
    result: { subtype: "success", duration_ms: 1, is_error: false },
  }) });
  boundedBlockTurn = state.runtimes[boundedBlocksSid].turns[0];
  assert.equal(boundedBlockTurn.detailRestorePending, true,
    "terminal reconciliation survives an already in-flight running snapshot");
  assert.equal(boundedBlockTurn.detailLoading, true);
  const mergedLiveDetail = mergeDetailWithLiveTail(
    [{
      kind: "process", item_id: "source-old", processKind: "command",
      phase: "end", status: "succeeded", title: "较早命令", done: true,
    }],
    [
      {
        kind: "process", item_id: OMITTED_PROCESS_ITEM_ID,
        processKind: "compaction", phase: "snapshot", status: "succeeded",
        title: "较早过程已省略", done: true,
      },
      {
        kind: "process", item_id: "live-new", processKind: "agent",
        phase: "start", status: "running", title: "最新活动", done: false,
      },
    ],
  );
  assert.deepEqual(mergedLiveDetail.map((block: {
    kind: string; item_id?: string;
  }) => block.kind === "process" ? block.item_id : ""), [
    "source-old", "live-new",
  ], "a loaded detail page and later live tail must render once in source order");
  const repeatedDistinctCommentary = mergeDetailWithLiveTail(
    [{
      kind: "text", message_id: "history-repeat", text: "same update",
      done: true, channel: "commentary",
    }],
    [{
      kind: "text", message_id: "live-repeat", text: "same update",
      done: true, channel: "commentary",
    }],
  );
  assert.deepEqual(
    repeatedDistinctCommentary.flatMap((block) =>
      block.kind === "text" ? [block.message_id] : []),
    ["history-repeat", "live-repeat"],
    "equal detail/live text with distinct native ids is two real occurrences",
  );
  const interleavedObservedProcess = mergeDetailWithLiveTail(
    [
      {
        kind: "text", message_id: "commentary-before", text: "before",
        done: true, channel: "commentary", liveOrder: 0,
      },
      {
        kind: "text", message_id: "commentary-after", text: "after",
        done: true, channel: "commentary", liveOrder: 1,
      },
    ],
    [
      {
        kind: "text", message_id: "commentary-before", text: "before",
        done: true, channel: "commentary", liveOrder: 0,
      },
      {
        kind: "tool", message_id: "tool-message-1", tool_use_id: "tool-1",
        tool: "exec", input: {}, done: true, liveOrder: 1,
      },
      {
        kind: "text", message_id: "commentary-after", text: "after",
        done: true, channel: "commentary", liveOrder: 2,
      },
      {
        kind: "tool", message_id: "tool-message-2", tool_use_id: "tool-2",
        tool: "exec", input: {}, done: true, liveOrder: 3,
      },
    ],
  );
  assert.deepEqual(
    interleavedObservedProcess.map((block) => block.kind === "text"
      ? block.message_id : block.kind === "tool"
        ? block.tool_use_id : block.item_id),
    ["commentary-before", "tool-1", "commentary-after", "tool-2"],
    "an official detail subset must not move observed tools below later commentary",
  );
  const unorderedLiveSubset = mergeDetailWithLiveTail(
    [
      {
        kind: "text", message_id: "source-first", text: "first",
        done: true, channel: "commentary",
      },
      {
        kind: "text", message_id: "source-second", text: "second",
        done: true, channel: "commentary",
      },
    ],
    [
      {
        kind: "text", message_id: "source-second", text: "second",
        done: true, channel: "commentary",
      },
      {
        kind: "tool", message_id: "unknown-order-tool", tool_use_id: "unknown-order-tool",
        tool: "exec", input: {}, done: true,
      },
      {
        kind: "text", message_id: "source-first", text: "first",
        done: true, channel: "commentary",
      },
    ],
  );
  assert.deepEqual(
    unorderedLiveSubset.map((block) => block.kind === "text"
      ? block.message_id : block.kind === "tool"
        ? block.tool_use_id : block.item_id),
    ["source-first", "source-second", "unknown-order-tool"],
    "Set containment without reducer liveOrder is not chronology evidence",
  );
  const authoritativePayload = mergeDetailWithLiveTail(
    [{
      kind: "process", item_id: "source-complete", processKind: "command",
      phase: "end", status: "succeeded", title: "完整命令",
      output: "FULL-AUTHORITATIVE-OUTPUT", done: true,
    }],
    [{
      kind: "process", item_id: "source-complete", processKind: "command",
      phase: "end", status: "succeeded", title: "完整命令",
      output: "TRUNCATED-SPILL", done: true,
    }],
    true,
  );
  assert.equal(
    authoritativePayload[0]?.kind === "process"
      ? authoritativePayload[0].output : undefined,
    "FULL-AUTHORITATIVE-OUTPUT",
    "a bounded spill cannot overwrite a completed authoritative detail payload",
  );
  const authoritativeThroughLiveTail = mergeDetailWithLiveTail(
    authoritativePayload,
    [{
      kind: "process", item_id: "source-complete", processKind: "command",
      phase: "end", status: "succeeded", title: "完整命令",
      output: "TRUNCATED-LIVE-TAIL", done: true,
    }],
    true,
  );
  assert.equal(
    authoritativeThroughLiveTail[0]?.kind === "process"
      ? authoritativeThroughLiveTail[0].output : undefined,
    "FULL-AUTHORITATIVE-OUTPUT",
    "the live Turn.blocks merge cannot overwrite detail after the spill merge",
  );
  const restoredObservedPayload = restoreObservedLiveTurnDetails(
    [{
      id: "observed-authoritative", prompt: "inspect", done: true,
      blocks: [{
        kind: "text", message_id: "observed-final",
        text: "done", channel: "final", done: true,
      }],
    }],
    [{
      id: "observed-authoritative", prompt: "inspect", done: true,
      blocks: [{
        kind: "process", item_id: "observed-process",
        processKind: "command", phase: "end", status: "succeeded",
        title: "完整命令", output: "TRUNCATED-LIVE-TAIL", done: true,
      }],
      liveSpillBlocks: [{
        kind: "process", item_id: "observed-process",
        processKind: "command", phase: "end", status: "succeeded",
        title: "完整命令", output: "TRUNCATED-SPILL", done: true,
      }],
      detailProjection: {
        segments: [],
        blocks: [{
          kind: "process", item_id: "observed-process",
          processKind: "command", phase: "end", status: "succeeded",
          title: "完整命令", output: "FULL-AUTHORITATIVE-OUTPUT", done: true,
        }],
        capped: false,
        hasMore: false,
        oldestCursor: null,
        hasNewer: false,
        newerCursor: null,
      },
    }],
  );
  assert.equal(
    restoredObservedPayload[0].detailProjection?.blocks[0]?.kind === "process"
      ? restoredObservedPayload[0].detailProjection.blocks[0].output
      : undefined,
    "FULL-AUTHORITATIVE-OUTPUT",
    "summary restoration keeps detail authoritative through spill and live",
  );

  const sharedNativeSteerId = "native-task-with-steers";
  const preSteerTools = Array.from({ length: 12 }, (_, index) => ({
    kind: "process" as const,
    item_id: `pre-steer-tool-${index}`,
    processKind: "command" as const,
    phase: "end" as const,
    status: "succeeded" as const,
    title: `tool ${index}`,
    done: true,
  }));
  const restoredSteerSegments = restoreObservedLiveTurnDetails(
    [
      {
        id: "native-user-before-steer", prompt: "initial request",
        forkPointId: sharedNativeSteerId, blocks: [], done: true,
      },
      {
        id: "native-user-first-steer", prompt: "first steer",
        forkPointId: sharedNativeSteerId, blocks: [], done: true,
      },
      {
        id: "native-user-second-steer", prompt: "second steer",
        forkPointId: sharedNativeSteerId, blocks: [], done: false,
      },
    ],
    [
      {
        id: "native-user-before-steer", prompt: "initial request",
        forkPointId: sharedNativeSteerId, blocks: preSteerTools, done: true,
      },
      {
        id: "native-user-first-steer", prompt: "first steer",
        forkPointId: sharedNativeSteerId,
        blocks: [{
          kind: "process", item_id: "first-steer-tool",
          processKind: "command", phase: "end", status: "succeeded",
          title: "first steer tool", done: true,
        }],
        done: true,
      },
      {
        id: "native-user-second-steer", prompt: "second steer",
        forkPointId: sharedNativeSteerId,
        blocks: [{
          kind: "process", item_id: "second-steer-tool",
          processKind: "command", phase: "start", status: "running",
          title: "second steer tool", done: false,
        }],
        done: false,
      },
    ],
  );
  assert.deepEqual(
    restoredSteerSegments[0].detailProjection?.blocks.flatMap((block) =>
      block.kind === "process" ? [block.item_id] : []),
    preSteerTools.map((block) => block.item_id),
    "a History refresh must not move or erase pre-steer tools when every "
      + "visible segment shares one native task id",
  );
  assert.deepEqual(
    restoredSteerSegments[1].detailProjection?.blocks.flatMap((block) =>
      block.kind === "process" ? [block.item_id] : []),
    ["first-steer-tool"],
    "each completed steer segment restores only its own observed process",
  );
  const restoredCachedSteerSegments = restoreCachedTurnDetails(
    [
      {
        id: "native-user-before-steer", prompt: "initial request",
        forkPointId: sharedNativeSteerId, blocks: [], done: true,
        detailEventCount: 12,
      },
      {
        id: "native-user-first-steer", prompt: "first steer",
        forkPointId: sharedNativeSteerId, blocks: [], done: true,
        detailEventCount: 1,
      },
    ],
    restoredSteerSegments.slice(0, 2),
    "idle",
  );
  assert.deepEqual(
    restoredCachedSteerSegments[0].detailProjection?.blocks.flatMap((block) =>
      block.kind === "process" ? [block.item_id] : []),
    preSteerTools.map((block) => block.item_id),
    "switching away and back must not let a shared native task id move the "
      + "pre-steer tools into a later cached segment",
  );
  assert.deepEqual(
    restoredCachedSteerSegments[1].detailProjection?.blocks.flatMap((block) =>
      block.kind === "process" ? [block.item_id] : []),
    ["first-steer-tool"],
    "cached steer segments remain one-to-one after a session switch",
  );

  const rollingSpillSid = "rolling-live-spill";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [rollingSpillSid]: {
        ...createRuntime(), state: "running", syncReady: true,
      },
    },
  };
  state = reduce(state, { type: "query_sent", sid: rollingSpillSid,
    prompt: "keep the whole running process", msg_id: "rolling-turn", ts: 31_600 });
  const spillBatch = MAX_TURN_BLOCKS + 20;
  const emitSpillBatch = (prefix: string) => {
    for (let index = 0; index < spillBatch; index += 1) {
      state = reduce(state, { type: "event", event: event({
        type: "process", sid: rollingSpillSid,
        item_id: `${prefix}-${index}`, kind: "command", phase: "end",
        status: "succeeded", title: `${prefix} ${index}`,
      }) });
    }
  };
  emitSpillBatch("first");
  let rollingTurn = state.runtimes[rollingSpillSid].turns[0];
  const firstSpillCount = rollingTurn.liveSpilledBlockCount ?? 0;
  const firstSnapshotBlocks = Array.from({ length: spillBatch }, (_, index) => ({
    kind: "process" as const,
    item_id: `first-${index}`,
    processKind: "command" as const,
    phase: "end" as const,
    status: "succeeded" as const,
    title: `first ${index}`,
    done: true,
  }));
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [rollingSpillSid]: {
        ...state.runtimes[rollingSpillSid],
        turns: [{
          ...rollingTurn,
          detailRestorePending: false,
          detailLoading: false,
          detailLoaded: true,
          liveSpillRefreshCount: firstSpillCount,
          detailProjection: {
            segments: [],
            blocks: firstSnapshotBlocks,
            capped: false,
            hasMore: false,
            oldestCursor: null,
            hasNewer: false,
            newerCursor: null,
          },
        }],
      },
    },
  };
  emitSpillBatch("second");
  rollingTurn = state.runtimes[rollingSpillSid].turns[0];
  const spillArchive = (rollingTurn as typeof rollingTurn & {
    liveSpillBlocks?: typeof firstSnapshotBlocks;
  }).liveSpillBlocks ?? [];
  const completeRollingProcess = mergeDetailWithLiveTail(
    rollingTurn.detailProjection?.blocks ?? [],
    [...spillArchive, ...rollingTurn.blocks],
  );
  assert.equal(new Set(completeRollingProcess.flatMap((block) =>
    block.kind === "process" ? [block.item_id] : [])).size, spillBatch * 2,
  "a second live spill must remain continuous with the earlier detail snapshot");
  assert.equal(rollingTurn.detailRestorePending, true,
    "continued spill growth must schedule a coalesced authoritative refresh");
  const reconciledRollingTurn = mergeInitialHistory(
    [{
      id: rollingTurn.id,
      prompt: rollingTurn.prompt,
      blocks: [],
      done: false,
    }],
    [rollingTurn],
    { preserveLiveTailOpen: true },
  )[0];
  assert.equal(
    reconciledRollingTurn.liveSpillBlocks?.length,
    rollingTurn.liveSpillBlocks?.length,
    "an in-progress summary refresh cannot discard the provisional spill archive",
  );

  const interleavedSpillSid = "interleaved-live-spill";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [interleavedSpillSid]: {
        ...createRuntime(), state: "running", syncReady: true,
      },
    },
  };
  state = reduce(state, { type: "query_sent", sid: interleavedSpillSid,
    prompt: "preserve interleaved process order", msg_id: "interleaved-turn",
    ts: 31_700 });
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: interleavedSpillSid, item_id: "process-a",
    kind: "command", phase: "start", status: "running", title: "A",
  }) });
  for (let index = 0; index < MAX_TURN_BLOCKS + 8; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "process", sid: interleavedSpillSid,
      item_id: `process-b-${index}`, kind: "command", phase: "start",
      status: "running", title: `B ${index}`,
    }) });
  }
  let interleavedTurn = state.runtimes[interleavedSpillSid].turns[0];
  assert.ok(interleavedTurn.liveSpillBlocks?.some((block: Block) =>
    block.kind === "process" && block.item_id === "process-a"),
  "the oldest still-open process is represented in the spill archive");
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: interleavedSpillSid, item_id: "process-a",
    kind: "command", phase: "end", status: "succeeded", title: "A",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: interleavedSpillSid, item_id: "process-c",
    kind: "command", phase: "start", status: "running", title: "C",
  }) });
  interleavedTurn = state.runtimes[interleavedSpillSid].turns[0];
  const interleavedBlocks = [
    ...(interleavedTurn.liveSpillBlocks ?? []),
    ...interleavedTurn.blocks,
  ].sort((left, right) => (left.liveOrder ?? 0) - (right.liveOrder ?? 0));
  const interleavedIds = interleavedBlocks.flatMap((block) =>
    block.kind === "process" ? [block.item_id] : []);
  assert.equal(interleavedIds[0], "process-a");
  assert.equal(interleavedIds.at(-1), "process-c");
  assert.equal(interleavedIds.filter((id) => id === "process-a").length, 1,
    "closing a spilled process updates its original block instead of appending a duplicate");
  const processA = interleavedBlocks.find((block) =>
    block.kind === "process" && block.item_id === "process-a");
  assert.equal(processA?.done, true);

  const boundedPayloadSid = "bounded-turn-payload";
  state = {
    ...state,
    runtimes: {
      ...state.runtimes,
      [boundedPayloadSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, { type: "query_sent", sid: boundedPayloadSid,
    prompt: "large diffs", msg_id: "bounded-payload-turn", ts: 31_700 });
  const maximumDiff = "d".repeat(2 * 1024 * 1024);
  for (let index = 0; index < 10; index += 1) {
    state = reduce(state, { type: "event", event: event({
      type: "turn_diff", sid: boundedPayloadSid, item_id: `large-diff-${index}`,
      diff: maximumDiff,
    }) });
  }
  const payloadBlocks = state.runtimes[boundedPayloadSid].turns[0].blocks;
  const retainedDiffChars = payloadBlocks.reduce((total: number, block: {
    kind: string; diff?: string | null;
  }) => total + (block.kind === "process" ? (block.diff?.length ?? 0) : 0), 0);
  assert.ok(retainedDiffChars <= MAX_TURN_BLOCK_CHARS);
  assert.ok(payloadBlocks.some((block: { kind: string; item_id?: string }) =>
    block.kind === "process" && block.item_id === "large-diff-9"));
  assert.equal(payloadBlocks.filter((block: { kind: string; item_id?: string }) =>
    block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID).length, 0);

  // Authoritative History is reduced through the same bounded window and must
  // not recreate an oversized turn after a refresh.
  const boundedHistorySid = "bounded-history-blocks";
  const boundedHistoryEvents: ServerEvent[] = [
    event({ type: "user_msg", sid: boundedHistorySid,
      msg_id: "bounded-history-turn", prompt: "history" }),
    event({ type: "assistant_msg_start", sid: boundedHistorySid,
      message_id: "history-final", channel: "final" }),
    event({ type: "delta", sid: boundedHistorySid,
      message_id: "history-final", channel: "final", text: "历史最终答案" }),
    event({ type: "assistant_msg_end", sid: boundedHistorySid,
      message_id: "history-final", channel: "final" }),
  ];
  for (let index = 0; index < MAX_TURN_BLOCKS + 30; index += 1) {
    boundedHistoryEvents.push(event({
      type: "process", sid: boundedHistorySid, item_id: `history-process-${index}`,
      kind: "hook", phase: "end", status: "succeeded", title: `History ${index}`,
    }));
  }
  boundedHistoryEvents.push(event({
    type: "turn_end", sid: boundedHistorySid,
    result: { subtype: "success", duration_ms: 1, is_error: false },
  }));
  state = reduce(state, { type: "event", event: event({
    type: "history", sid: boundedHistorySid, session_id: boundedHistorySid,
    revision: "bounded-rev", in_progress: false, has_more: false,
    events: boundedHistoryEvents,
  }) });
  const boundedHistoryTurn = state.runtimes[boundedHistorySid].turns[0];
  assert.equal(boundedHistoryTurn.blocks.length, MAX_TURN_BLOCKS);
  assert.equal(boundedHistoryTurn.blocks.filter((block: {
    kind: string; item_id?: string;
  }) => block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID).length, 0);
  assert.ok((boundedHistoryTurn.detailEventCount ?? 0) > MAX_TURN_BLOCKS);
  assert.ok(boundedHistoryTurn.blocks.some((block: {
    kind: string; message_id?: string;
  }) => block.kind === "text" && block.message_id === "history-final"));

  assert.deepEqual(finalTextBlocks(richTurn.blocks).map((block: { text: string }) => block.text),
    ["已经完成。"]);
  const { presentTool, isToolFailure } = await reducerHarness.ssrLoadModule(
    "/src/tool-presentation.ts");
  assert.deepEqual(presentTool({
    kind: "tool", message_id: "m", tool_use_id: "mcp", tool: "lookup",
    input: {}, category: "mcp", title: "docs · lookup", done: false,
  }), { icon: "term", title: "docs · lookup", subtitle: "", group: "MCP" });
  assert.equal(presentTool({
    kind: "tool", message_id: "w", tool_use_id: "web", tool: "webSearch",
    input: { query: "sdk" }, category: "web_search", title: "搜索 sdk", done: false,
  }).icon, "research");
  assert.equal(presentTool({
    kind: "tool", message_id: "r", tool_use_id: "read", tool: "Read",
    input: { file_path: "/tmp/report.md" }, category: "file",
    title: "读取 report.md", done: true,
  }).icon, "read",
  "an engine title must not turn a read-only file tool into a pen icon");
  for (const tool of ["Glob", "Grep"]) {
    const presentation = presentTool({
      kind: "tool", message_id: tool, tool_use_id: tool, tool,
      input: { pattern: "*.tsx", path: "web/src" }, category: "file",
      title: tool === "Glob" ? "查找文件 · *.tsx" : "搜索 · *.tsx", done: true,
    });
    assert.equal(presentation.icon, "research",
      `${tool} must remain a read-only search activity`);
    assert.equal(presentation.group, "搜索");
  }
  assert.equal(presentTool({
    kind: "tool", message_id: "image", tool_use_id: "image", tool: "view_image",
    input: { path: "/tmp/chart.png" }, category: "file",
    title: "查看图片", done: true,
  }).icon, "read",
  "view_image must remain a read-only file activity");
  assert.equal(presentTool({
    kind: "tool", message_id: "a", tool_use_id: "add", tool: "fileChange",
    input: { changes: [{ path: "new.ts", kind: "add" }] },
    category: "file", title: "修改", done: true,
  }).icon, "file-plus");
  assert.equal(presentTool({
    kind: "tool", message_id: "x", tool_use_id: "delete", tool: "fileChange",
    input: { changes: [{ path: "old.ts", kind: "delete" }] },
    category: "file", title: "修改", done: true,
  }).icon, "trash");
  assert.equal(presentTool({
    kind: "tool", message_id: "m", tool_use_id: "move", tool: "fileChange",
    input: { changes: [{
      path: "old.ts", move_path: "new.ts", kind: "move",
    }] },
    category: "file", title: "修改", done: true,
  }).icon, "branch");
  assert.equal(presentTool({
    kind: "tool", message_id: "u", tool_use_id: "update", tool: "apply_patch",
    input: { changes: [{ path: "app.ts", kind: "update" }] },
    category: "file", title: "修改", done: true,
  }).icon, "code");
  assert.equal(isToolFailure({
    kind: "tool", message_id: "d", tool_use_id: "declined", tool: "shell",
    input: {}, done: true,
    result: { content: "", is_error: false, status: "declined" },
  }), true);

  const { ChatView } = await reducerHarness.ssrLoadModule(
    "/src/components/ChatView.tsx");
  const { TurnChangesPanel } = await reducerHarness.ssrLoadModule(
    "/src/components/TurnChangesPanel.tsx");
  await import("./turn-changes.test.ts");
  const boundedInitialMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "long-session",
    turns: Array.from({ length: 30 }, (_, index) => ({
      id: `long-${index}`, prompt: `prompt-${index}`, done: true, blocks: [],
    })),
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.doesNotMatch(boundedInitialMarkup, /prompt-0</,
    "switching to a long session must not synchronously render its full DOM");
  assert.match(boundedInitialMarkup, /prompt-29</);
  assert.match(boundedInitialMarkup, /virtual-thread-in/,
    "the first paint keeps only a small latest fallback before DOM measurement");
  const recoveringHistoryMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "recovering-history",
    turns: [{
      id: "recovering-turn", prompt: "keep visible", done: true, blocks: [],
    }],
    hasMore: true,
    historyPagingReady: false,
    engine: "codex",
    onEdit: () => {},
    onGetDiff: () => {},
  }));
  assert.match(recoveringHistoryMarkup, /正在恢复历史…/,
    "known older history keeps a truthful disabled affordance during reconnect");
  assert.doesNotMatch(recoveringHistoryMarkup, /class="load-more-btn"/,
    "a retained old-generation cursor must never stay clickable");
  const summaryMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "summary-session",
    turns: [{
      id: "summary-turn", prompt: "inspect", done: true,
      blocks: [], processDetailState: "present", detailReasons: ["process"],
      detailEventCount: 12, detailLoaded: false,
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(summaryMarkup, /已处理/,
    "summary history must reuse the existing collapsed process affordance");
  assert.match(summaryMarkup, /12 项/,
    "the collapsed process affordance keeps the deferred detail count");
  assert.doesNotMatch(summaryMarkup, /展开完整过程/,
    "deferred detail must not add a second standalone UI control");
  const loadedDetailMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "summary-session",
    turns: [{
      id: "summary-turn", prompt: "inspect", done: true,
      blocks: [], processDetailState: "present", detailReasons: ["process"],
      detailEventCount: 12, detailLoaded: true,
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.doesNotMatch(loadedDetailMarkup, /展开完整过程/);
  assert.match(loadedDetailMarkup, /已处理/,
    "a known process stays visible while its current detail page is replaced");
  assert.match(loadedDetailMarkup, /加载失败/,
    "a completed empty detail response must expose a retryable contradiction");
  const repairedDeferredDetail = mergeInitialHistory([{
    id: "native-summary-turn",
    clientMsgId: "browser-turn",
    prompt: "修复问题。",
    done: true,
    processDetailState: "present",
    detailReasons: ["process"],
    detailEventCount: 93,
    detailLoaded: false,
    blocks: [{
      kind: "text", message_id: "native-final", channel: "final",
      text: "已经修复。", done: true,
    }],
  }], [{
    id: "browser-turn",
    historyTurnId: "native-summary-turn",
    prompt: "修复问题。",
    done: true,
    // A stale cache flag without its former projection is the production
    // failure shape: it must not suppress the server-advertised process shell.
    detailLoaded: true,
    blocks: [{
      kind: "text", message_id: "native-final", channel: "final",
      text: "已经修复。", done: true,
    }],
  }])[0];
  assert.equal(repairedDeferredDetail.detailEventCount, 93);
  assert.equal(repairedDeferredDetail.detailLoaded, false,
    "a stale loaded bit without payload cannot erase deferred process history");
  const repairedDeferredMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "repaired-summary-session",
    turns: [repairedDeferredDetail],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
    onLoadDetail: () => {},
  }));
  assert.match(repairedDeferredMarkup, /已处理/);
  assert.match(repairedDeferredMarkup, /93 项/);
  const failedInterruptedMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "failed-interrupted-session",
    turns: [{
      id: "failed-interrupted-turn",
      prompt: "work",
      done: true,
      interrupted: true,
      error: "网络异常，连接失败，请重新尝试。",
      blocks: [],
    }],
    engine: "codex", onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(failedInterruptedMarkup, /网络异常，连接失败，请重新尝试。/);
  assert.doesNotMatch(failedInterruptedMarkup, /— 已打断 —/,
    "one terminal failure must not render a second interrupted note");
  const richMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [richTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(richMarkup, /已处理 2s/,
    "Codex timing stops at the final visible process snapshot");
  assert.match(richMarkup, /已经完成/);
  assert.doesNotMatch(richMarkup, /复制回复/,
    "reply copy keeps the original compact icon instead of adding a text action");
  assert.match(richMarkup, /class="ubub-meta ai-meta"[\s\S]*aria-label="复制"/,
    "the original reply copy icon remains in the completed-turn metadata row");
  assert.match(richMarkup,
    /class="ubub-meta ai-meta"[\s\S]*?<button[^>]*aria-label="复制"[^>]*><svg[^>]*>[\s\S]*?<rect x="8" y="8" width="14" height="14" rx="2"\/><path d="M4 16a2 2 0 01-2-2V4a2 2 0 012-2h10a2 2 0 012 2"/,
    "reply copy uses the overlapping-squares icon instead of a checkmark");
  assert.doesNotMatch(richMarkup, /先检查代码/);
  assert.doesNotMatch(richMarkup, /class="turn-working"/);

  const cachedClaudeDurationMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "cached-claude-duration", engine: "claude",
    turns: [{
      id: "cached-claude-turn", prompt: "处理", done: true,
      ts: 1000, doneTs: 6000, durationMs: 0,
      blocks: [{
        kind: "process", item_id: "cached-tool", processKind: "hook",
        phase: "end", status: "succeeded", title: "完成", done: true,
      }],
    }],
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(cachedClaudeDurationMarkup, /已处理 5s/,
    "old Claude history caches derive wall time from their timestamps");

  const cachedCodexDurationMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "cached-codex-duration", engine: "codex",
    turns: [{
      id: "cached-codex-turn", prompt: "处理", done: true,
      ts: 1000, doneTs: 6000, durationMs: 0,
      blocks: [{
        kind: "tool", message_id: "cached-codex-message",
        tool_use_id: "cached-codex-tool", tool: "shell",
        input: {}, done: true, result: { content: "ok", is_error: false },
      }],
    }],
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(cachedCodexDurationMarkup, />已处理</);
  assert.doesNotMatch(cachedCodexDurationMarkup, /已处理 0s/,
    "Codex process timing must not fall back to the user-message interval");
  const unknownCodexDurationMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: "unknown-codex-duration", engine: "codex",
    turns: [{
      id: "unknown-codex-turn", prompt: "继续", done: true,
      ts: 1000, doneTs: 1000,
      blocks: [{
        kind: "tool", message_id: "unknown-codex-message",
        tool_use_id: "unknown-codex-tool", tool: "shell",
        input: {}, done: true, result: { content: "ok", is_error: false },
      }],
    }],
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(unknownCodexDurationMarkup, />已处理</);
  assert.doesNotMatch(unknownCodexDurationMarkup, /已处理 0s/,
    "a synthetic steer boundary without duration evidence must not invent 0s");

  // The active marker always stays at the physical tail of the turn so a long
  // process timeline cannot scroll the only running feedback out of view. The
  // process shell still reports phase/timing, but its live icon stays static.
  const thinkingMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [{
      id: "thinking-live", prompt: "继续", done: false,
      blocks: [{ kind: "text", message_id: "thinking-text", channel: "thinking",
        text: "正在检查实现", done: false }],
    }], engine: "claude", activeTurnId: "thinking-live",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(thinkingMarkup, /class="turn-process open"/);
  assert.match(thinkingMarkup, /正在处理/);
  assert.match(thinkingMarkup, /正在检查实现/);
  assert.match(thinkingMarkup, /class="turn-working"[\s\S]*处理中/,
    "visible reasoning keeps the animated marker at the turn tail");
  assert.doesNotMatch(thinkingMarkup, /process-spin/,
    "the active process header stays static beside the tail animation");

  const answerMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [{
      id: "answer-live", prompt: "继续", done: false,
      blocks: [{ kind: "text", message_id: "answer-text", channel: "final",
        text: "正在回答", done: false }],
    }], engine: "codex", activeTurnId: "answer-live",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(answerMarkup, /class="turn-working"/);
  assert.match(answerMarkup, /回答中/);
  assert.doesNotMatch(answerMarkup, /class="turn-process/,
    "final-only streaming does not invent an empty process shell");
  const answeringAfterProcessMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [{
      id: "answering-after-process", prompt: "继续", done: false,
      blocks: [{
        kind: "process", item_id: "completed-command",
        processKind: "command", phase: "end", status: "succeeded",
        title: "命令完成", done: true,
      }, {
        kind: "text", message_id: "answer-after-process",
        channel: "final", text: "正在总结", done: false,
      }],
    }], engine: "codex", activeTurnId: "answering-after-process",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(answeringAfterProcessMarkup, /class="turn-process open"/,
    "the process disclosure stays open until the turn terminal boundary");
  assert.match(answeringAfterProcessMarkup, /turn-process-state done/,
    "a settled process shell cannot keep spinning while the answer streams");
  assert.match(answeringAfterProcessMarkup, /class="turn-working"[\s\S]*回答中/);
  assert.equal(
    (answeringAfterProcessMarkup.match(/process-spin/g) ?? []).length,
    0,
    "the settled process shell cannot add a second live spinner",
  );
  const commentaryWhileAnsweringMarkup = renderToStaticMarkup(createElement(
    ChatView,
    {
      sid: richSid,
      turns: [{
        id: "commentary-while-answering", prompt: "继续", done: false,
        blocks: [{
          kind: "text", message_id: "live-commentary",
          channel: "commentary", text: "仍在检查", done: false,
        }, {
          kind: "text", message_id: "early-final",
          channel: "final", text: "阶段结论", done: false,
        }],
      }],
      engine: "codex", activeTurnId: "commentary-while-answering",
      onEdit: () => {}, onGetDiff: () => {},
    },
  ));
  assert.match(commentaryWhileAnsweringMarkup, /turn-process-state running/);
  assert.match(
    commentaryWhileAnsweringMarkup,
    /class="turn-working"[\s\S]*处理中/,
    "unfinished commentary keeps the live marker at the turn tail",
  );
  assert.doesNotMatch(commentaryWhileAnsweringMarkup, /process-spin/,
    "the process header cannot compete with the tail animation");
  const zeroTokenRunningMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [{
      id: "zero-token-live", clientMsgId: "zero-token-prompt",
      prompt: "开始检查", done: false, blocks: [],
  }], engine: "codex", activeTurnId: "zero-token-live",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.match(zeroTokenRunningMarkup, /class="turn-working"/);
  assert.match(zeroTokenRunningMarkup, /思考中/);
  assert.doesNotMatch(zeroTokenRunningMarkup, /正在处理|等待响应/,
    "a pre-token turn cannot render a fake empty process timeline");
  assert.doesNotMatch(zeroTokenRunningMarkup, /class="turn-process/);
  const { ProcessTimeline } = await reducerHarness.ssrLoadModule(
    "/src/components/ProcessTimeline.tsx");
  const pagedProcessMarkup = renderToStaticMarkup(createElement(
    ProcessTimeline, {
      blocks: [{
        kind: "process", item_id: "paged-process",
        processKind: "command", phase: "end", status: "succeeded",
        title: "当前过程页", done: true,
      }],
      done: true,
      openOverride: true,
      canLoadEarlier: true,
      canLoadNewer: true,
    },
  ));
  assert.match(pagedProcessMarkup, /加载更早过程/);
  assert.match(pagedProcessMarkup, /返回较新过程/,
    "a bounded process window keeps both source navigation controls");
  const failedProcessMarkup = renderToStaticMarkup(createElement(
    ProcessTimeline, {
      blocks: [],
      done: true,
      deferredCount: 12,
      detailError: "详细过程暂时不可用，请稍后重试",
      openOverride: true,
      onLoadDetail: () => true,
    },
  ));
  assert.match(failedProcessMarkup, /role="alert"/);
  assert.match(failedProcessMarkup, /详细过程暂时不可用，请稍后重试/);
  assert.match(failedProcessMarkup, />重试</,
    "detail failure stays in the opened shell instead of requiring a second header click");
  assert.doesNotMatch(failedProcessMarkup, /正在加载过程/);
  const failedLoadedPageMarkup = renderToStaticMarkup(createElement(
    ProcessTimeline, {
      blocks: [],
      done: true,
      deferredCount: 0,
      detailError: "详细过程暂时不可用，请稍后重试",
      openOverride: true,
      onRetryDetail: () => true,
    },
  ));
  assert.match(failedLoadedPageMarkup, /role="alert"/,
    "a later detail-page failure remains visible after the first page loaded");
  const planOnlyBlock: Block = {
    kind: "process",
    item_id: "plan-only",
    processKind: "plan",
    phase: "end",
    status: "succeeded",
    title: "执行计划",
    plan: [{ step: "检查实现", status: "completed" }],
    done: true,
  };
  const planOnlyMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex",
    done: true,
    openOverride: true,
    blocks: [planOnlyBlock],
  }));
  assert.match(planOnlyMarkup, /plan-progress-trigger/,
    "a plan-only timeline keeps the compact plan control");
  assert.doesNotMatch(planOnlyMarkup, /turn-process-head/,
    "a plan-only timeline does not advertise an empty outer disclosure");
  assert.doesNotMatch(planOnlyMarkup, />1 项</);
  const externalPlanOnlyMarkup = renderToStaticMarkup(createElement(
    ProcessTimeline, {
      engine: "codex",
      done: true,
      blocks: [planOnlyBlock],
      externalPlanItemId: "plan-only",
    },
  ));
  assert.equal(externalPlanOnlyMarkup, "",
    "the session-level strip removes its plan control from the message row");

  const restoredPlan = latestPlanProgress([{
    id: "restored-plan-turn",
    prompt: "执行任务",
    done: false,
    detailEventCount: 4,
    detailLoaded: false,
    blocks: [{
      kind: "process",
      item_id: "legacy-plan",
      processKind: "plan",
      phase: "end",
      status: "succeeded",
      title: "旧计划",
      detail: "旧版自由文本计划",
      done: true,
    }],
    detailProjection: {
      segments: [], capped: false, hasMore: false,
      oldestCursor: null, hasNewer: false, newerCursor: null,
      blocks: [{
        kind: "process",
        item_id: "structured-plan",
        processKind: "plan",
        phase: "update",
        status: "running",
        title: "计划",
        plan: [
          { step: "读取状态", status: "completed" },
          { step: "显示固定入口", status: "inProgress" },
        ],
        done: false,
      }],
    },
  }]);
  assert.equal(restoredPlan?.block.item_id, "structured-plan",
    "the fixed strip reads the authoritative structured plan projection");
  assert.equal(restoredPlan?.needsDetail, true,
    "opening a cached fixed strip still requests authoritative detail");
  assert.equal(planProgressPresentation(restoredPlan!.block).progressLabel,
    "1 / 2");
  assert.equal(latestPlanProgress([{
    id: "planned-turn", prompt: "先规划", done: true,
    blocks: [planOnlyBlock],
  }, {
    id: "compact-continuation", prompt: "", done: true,
    terminalSource: "compact_continuation", blocks: [],
  }])?.block.item_id, "plan-only",
  "a promptless assistant or compaction continuation preserves a completed Plan");
  assert.equal(latestPlanProgress([{
    id: "planned-turn", prompt: "先规划", done: true,
    blocks: [planOnlyBlock],
  }, {
    id: "new-turn", prompt: "新的无计划问题", done: false, blocks: [],
  }]), null,
  "the next user turn retires a completed task monitor");
  const completedGoal: ThreadGoal = {
    threadId: "completed-goal-thread",
    objective: "完成旧任务",
    status: "complete",
    engine: "codex",
    tokensUsed: 10,
    timeUsedSeconds: 20,
    updatedAt: 1_800_000_000,
  };
  const oldGoalPlan = latestPlanProgress([{
    id: "old-goal-plan-turn",
    prompt: "执行旧 Goal",
    done: false,
    ts: 1_799_999_990_000,
    blocks: [planOnlyBlock],
  }]);
  const nextGoalPlan = latestPlanProgress([{
    id: "next-plan-turn",
    prompt: "开始新任务",
    done: false,
    ts: 1_800_000_001_000,
    blocks: [{ ...planOnlyBlock, item_id: "next-plan" }],
  }]);
  assert.equal(completedGoalHasNewerUserTurn(completedGoal, [{
    id: "goal-assistant-continuation", prompt: "", done: true,
    ts: 1_800_000_001_000, blocks: [],
  }]), false, "assistant-only Goal continuation cannot retire its Goal");
  assert.equal(completedGoalHasNewerUserTurn(completedGoal, [{
    id: "next-goal-user-turn", prompt: "开始新任务", done: false,
    ts: 1_800_000_001_000, blocks: [],
  }]), true, "the first later user turn retires a completed Goal");
  assert.equal(completedGoalHasNewerUserTurn(completedGoal, [{
    id: "next-goal-image-turn", prompt: "", done: false,
    ts: 1_800_000_001_000, blocks: [],
    images: [{ media_type: "image/png", data: "image" }],
  }]), true, "an image-only user turn retires a completed Goal");
  assert.equal(completedGoalHasNewerUserTurn(completedGoal, [{
    id: "next-goal-image-ref-turn", prompt: "", done: false,
    ts: 1_800_000_001_000, blocks: [],
    imageRefs: [{
      image_id: "image-ref", media_type: "image/png",
      width: 1, height: 1, byte_size: 1,
    }],
  }]), true, "a history image-only user turn retires a completed Goal");
  assert.equal(completedGoalHasNewerUserTurn(completedGoal, [{
    id: "next-goal-file-turn", prompt: "", done: false,
    ts: 1_800_000_001_000, blocks: [],
    files: [{ filename: "task.txt", data: "task" }],
  }]), true, "a file-only user turn retires a completed Goal");
  assert.equal(planFollowsCompletedGoal(completedGoal, oldGoalPlan!), false,
    "the Goal's own Plan stays attached after completion");
  assert.equal(planFollowsCompletedGoal(completedGoal, nextGoalPlan!), true,
    "a later user turn's Plan is independent from the completed Goal");
  const reboundOldGoalPlan = latestPlanProgress([{
    id: "next-plan-turn",
    prompt: "开始新任务",
    done: false,
    ts: 1_800_000_001_000,
    blocks: [{
      ...planOnlyBlock,
      item_id: "rebound-old-plan",
      turn_id: "old-goal-plan-turn",
    }],
  }]);
  assert.equal(
    planFollowsCompletedGoal(completedGoal, reboundOldGoalPlan!), false,
    "a durable old-Goal snapshot rebound onto the newest turn stays retired",
  );
  const activePlanBlock: Block = {
    ...planOnlyBlock,
    item_id: "active-plan",
    phase: "update",
    status: "running",
    plan: [
      { step: "检查实现", status: "completed" },
      { step: "完成验证", status: "inProgress" },
    ],
    done: false,
  };
  assert.equal(latestPlanProgress([{
    id: "active-plan-turn", prompt: "继续任务", done: false,
    blocks: [activePlanBlock],
  }, {
    id: "active-follow-up", prompt: "补充要求", done: false, blocks: [],
  }])?.turnId, "active-plan-turn",
  "a follow-up does not hide an unfinished task monitor");
  const interruptedPlanBlock: Block = {
    ...activePlanBlock,
    item_id: "interrupted-plan",
    status: "interrupted",
    done: true,
  };
  const interruptedPlanOwner: Turn = {
    id: "interrupted-plan-turn",
    prompt: "执行后终止",
    done: true,
    interrupted: true,
    blocks: [interruptedPlanBlock],
  };
  assert.equal(planProgressPresentation(
    latestPlanProgress([interruptedPlanOwner])!.block).failed, true,
  "an interrupted Plan remains visible until the next user turn");
  assert.equal(latestPlanProgress([interruptedPlanOwner, {
    id: "interrupt-system-tail", prompt: "", done: true, blocks: [],
  }])?.turnId, "interrupted-plan-turn",
  "a promptless continuation cannot acknowledge an interrupted Plan");
  const afterInterruptedPlan: Turn = {
    id: "after-interrupted-plan",
    prompt: "按新要求继续",
    done: false,
    blocks: [],
  };
  assert.equal(latestPlanProgress([
    interruptedPlanOwner, afterInterruptedPlan,
  ]), null,
  "the next user turn retires the previous interrupted Plan");
  const interruptedPlanCache = new SessionPlanProgressCache(2);
  const interruptedProgress = latestPlanProgress([interruptedPlanOwner]);
  assert.equal(interruptedPlanCache.resolve({
    sid: "interrupted-plan-session",
    runtime: interruptedProgress,
    history: null,
    runtimeTurns: [interruptedPlanOwner],
    historyTurns: [interruptedPlanOwner],
    recovering: false,
    runtimeLoading: false,
  })?.block.item_id, "interrupted-plan");
  assert.equal(interruptedPlanCache.resolve({
    sid: "interrupted-plan-session",
    runtime: latestPlanProgress([
      interruptedPlanOwner, afterInterruptedPlan,
    ]),
    history: null,
    runtimeTurns: [interruptedPlanOwner, afterInterruptedPlan],
    historyTurns: [interruptedPlanOwner, afterInterruptedPlan],
    recovering: false,
    runtimeLoading: false,
  }), null,
  "the retained cache cannot resurrect an acknowledged interrupted Plan");
  assert.equal(latestPlanProgress([{
    id: "older-plan", prompt: "旧任务", done: true,
    blocks: [planOnlyBlock],
  }, {
    id: "newer-plan", prompt: "新任务", done: false,
    blocks: [{ ...planOnlyBlock, item_id: "newer-plan-item" }],
  }])?.turnId, "newer-plan",
  "the newest plan-bearing turn wins when a session contains several plans");
  const planCache = new SessionPlanProgressCache(4);
  const planOwnerAfterDetail: Turn = {
    id: restoredPlan!.turnId,
    prompt: "执行任务",
    done: false,
    detailEventCount: 4,
    detailLoaded: true,
    // The official full-turn projection omits update_plan.
    blocks: [],
  };
  assert.equal(planCache.resolve({
    sid: "plan-session-a",
    runtime: restoredPlan,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: false,
    runtimeLoading: false,
  })?.block.item_id, "structured-plan");
  assert.equal(planCache.resolve({
    sid: "plan-session-b",
    runtime: null,
    history: null,
    runtimeTurns: [],
    historyTurns: [],
    recovering: false,
    runtimeLoading: false,
  }), null, "a plan from session A must never leak into session B");
  const refocusedPlan = planCache.resolve({
    sid: "plan-session-a",
    runtime: null,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: false,
    runtimeLoading: false,
  });
  assert.equal(refocusedPlan?.block.item_id, "structured-plan",
    "A -> B -> A navigation retains the Plan omitted by full turn detail");
  assert.equal(planCache.resolve({
    machineId: "machine-b",
    engine: "codex",
    space: "code",
    sid: "plan-session-a",
    runtime: null,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: true,
    runtimeLoading: true,
  }), null,
  "the same native sid on another machine cannot inherit a retained Plan");
  assert.equal(planCache.resolve({
    machineId: "legacy",
    engine: "claude",
    space: "code",
    sid: "plan-session-a",
    runtime: null,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: true,
    runtimeLoading: true,
  }), null,
  "the same sid on another engine cannot inherit a retained Plan");
  assert.equal(planCache.resolve({
    machineId: "legacy",
    engine: "codex",
    space: "work",
    sid: "plan-session-a",
    runtime: null,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: true,
    runtimeLoading: true,
  }), null,
  "the same sid in Work cannot inherit Code's retained Plan");
  assert.equal(planProgressPresentation(refocusedPlan!.block).progressLabel,
    "1 / 2");
  assert.equal(refocusedPlan?.needsDetail, false,
    "the retained Plan follows its owner's current detail state");
  const newerLivePlan: Block = {
    kind: "process", item_id: "shared-plan", processKind: "plan",
    phase: "snapshot", status: "succeeded", title: "计划", done: true,
    plan: [
      { step: "读取状态", status: "completed" },
      { step: "修复问题", status: "completed" },
      { step: "完成验证", status: "inProgress" },
    ],
  };
  const staleDetailPlan: Block = {
    ...newerLivePlan,
    plan: [
      { step: "读取状态", status: "completed" },
      { step: "修复问题", status: "inProgress" },
      { step: "完成验证", status: "pending" },
    ],
  };
  for (const location of ["blocks", "liveSpillBlocks"] as const) {
    const livePlanAfterStaleDetail = latestPlanProgress([{
      id: `late-plan-detail-${location}`,
      prompt: "执行任务",
      done: true,
      blocks: location === "blocks" ? [newerLivePlan] : [],
      liveSpillBlocks: location === "liveSpillBlocks"
        ? [newerLivePlan] : undefined,
      detailProjection: {
        segments: [], capped: false, hasMore: false,
        oldestCursor: null, hasNewer: false, newerCursor: null,
        blocks: [staleDetailPlan],
      },
    }]);
    assert.equal(planProgressPresentation(
      livePlanAfterStaleDetail!.block).progressLabel, "2 / 3",
    `a late detail cannot roll back the Plan in ${location}`);
    assert.equal(livePlanAfterStaleDetail!.block.plan?.find(
      (entry) => entry.status === "inProgress")?.step, "完成验证",
    "the newer raw step survives even though its owning turn is terminal");
    assert.equal(planProgressPresentation(
      livePlanAfterStaleDetail!.block).stateLabel,
    "本轮已结束，计划未更新");
  }
  const completedDetailPlan: Block = {
    ...staleDetailPlan,
    plan: [
      { step: "读取状态", status: "completed" },
      { step: "修复问题", status: "completed" },
      { step: "完成验证", status: "completed" },
    ],
  };
  const completedDetailAfterStaleLive = latestPlanProgress([{
    id: "completed-detail-after-stale-live",
    prompt: "执行任务",
    done: true,
    blocks: [staleDetailPlan],
    detailProjection: {
      segments: [], capped: false, hasMore: false,
      oldestCursor: null, hasNewer: false, newerCursor: null,
      blocks: [completedDetailPlan],
    },
  }]);
  assert.equal(planProgressPresentation(
    completedDetailAfterStaleLive!.block).progressLabel, "3 / 3",
  "completed detail cannot be rolled back by an older live snapshot");
  assert.equal(planProgressPresentation(
    completedDetailAfterStaleLive!.block).complete, true);
  const completedPlanCache = new SessionPlanProgressCache(2);
  const completedPlanOwner: Turn = {
    id: "completed-plan-turn",
    prompt: "完成计划",
    done: true,
    blocks: [planOnlyBlock],
  };
  const completedPlan = latestPlanProgress([completedPlanOwner]);
  assert.ok(completedPlan);
  assert.equal(completedPlanCache.resolve({
    sid: "completed-plan-session",
    runtime: completedPlan,
    history: null,
    runtimeTurns: [completedPlanOwner],
    historyTurns: [completedPlanOwner],
    recovering: false,
    runtimeLoading: false,
  })?.block.item_id, "plan-only",
  "a completed Plan stays visible before the next user message");
  const nextUserTurn: Turn = {
    id: "next-user-turn",
    prompt: "开始下一个问题",
    done: false,
    blocks: [],
  };
  assert.equal(completedPlanCache.resolve({
    sid: "completed-plan-session",
    runtime: latestPlanProgress([completedPlanOwner, nextUserTurn]),
    history: null,
    runtimeTurns: [completedPlanOwner, nextUserTurn],
    historyTurns: [completedPlanOwner, nextUserTurn],
    recovering: false,
    runtimeLoading: false,
  }), null,
  "the cache cannot resurrect a completed Plan after the next message");
  const browsedCompletedPlanCache = new SessionPlanProgressCache(2);
  assert.equal(browsedCompletedPlanCache.resolve({
    sid: "browsed-completed-plan",
    runtime: null,
    history: completedPlan,
    runtimeTurns: [{
      id: "live-next-user-turn",
      prompt: "实时的新任务",
      done: false,
      ts: 2_000,
      blocks: [],
    }],
    historyTurns: [{ ...completedPlanOwner, ts: 1_000 }],
    recovering: false,
    runtimeLoading: false,
  }), null,
  "a live user turn retires a completed Plan found on an older history page");
  assert.equal(browsedCompletedPlanCache.resolve({
    sid: "browsed-promptless-plan",
    runtime: null,
    history: completedPlan,
    runtimeTurns: [{
      id: "live-promptless-continuation",
      prompt: "",
      done: true,
      ts: 2_000,
      terminalSource: "compact_continuation",
      blocks: [],
    }],
    historyTurns: [{ ...completedPlanOwner, ts: 1_000 }],
    recovering: false,
    runtimeLoading: false,
  })?.block.item_id, "plan-only",
  "a live promptless continuation preserves a browsed completed Plan");
  assert.equal(planCache.resolve({
    sid: "plan-session-a",
    runtime: null,
    history: null,
    runtimeTurns: [],
    historyTurns: [],
    recovering: false,
    runtimeLoading: true,
  })?.block.item_id, "structured-plan",
  "runtime eviction keeps the Plan visible while authoritative history reloads");
  planCache.clear("plan-session-a");
  assert.equal(planCache.resolve({
    sid: "plan-session-a",
    runtime: null,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: false,
    runtimeLoading: false,
  }), null, "history invalidation clears a stale retained Plan");
  planCache.resolve({
    sid: "tmp-plan-session",
    runtime: restoredPlan,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: false,
    runtimeLoading: false,
  });
  planCache.rekey("tmp-plan-session", "real-plan-session");
  assert.equal(planCache.resolve({
    sid: "real-plan-session",
    runtime: null,
    history: null,
    runtimeTurns: [planOwnerAfterDetail],
    historyTurns: [planOwnerAfterDetail],
    recovering: false,
    runtimeLoading: false,
  })?.block.item_id, "structured-plan",
  "capturing a real session id preserves the temporary session's Plan");
  const declinedMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    blocks: [{ kind: "process", item_id: "approval-denied", processKind: "hook",
      phase: "end", status: "declined", title: "Hook 已拒绝", done: false }],
    done: true,
  }));
  assert.match(declinedMarkup, /process-declined/);
  assert.match(declinedMarkup, /M18 6L6 18M6 6l12 12/);

  const codexProcessMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex", done: true,
    blocks: [
      { kind: "text", message_id: "private-thought", channel: "thinking",
        text: "private reasoning must stay hidden", done: true },
      { kind: "process", item_id: "reasoning-row", processKind: "reasoning",
        phase: "end", status: "succeeded", title: "思考",
        summary: "synthetic reasoning row", done: true },
      { kind: "tool", message_id: "tool-a-message", tool_use_id: "tool-a",
        tool: "shell", input: { command: "pwd" }, done: true,
        result: { content: "/tmp", is_error: false } },
      { kind: "tool", message_id: "tool-b-message", tool_use_id: "tool-b",
        tool: "shell", input: { command: "ls" }, done: true,
        result: { content: "file", is_error: false } },
    ],
  }));
  assert.doesNotMatch(codexProcessMarkup, /private reasoning must stay hidden/);
  assert.doesNotMatch(codexProcessMarkup, /synthetic reasoning row/);
  assert.match(codexProcessMarkup, /2 个工具调用/);
  assert.match(codexProcessMarkup, /aria-expanded="false"/,
    "entering a completed Codex turn keeps process collapsed");
  assert.doesNotMatch(codexProcessMarkup, /class="tool-group"/,
    "a completed Codex process must not render tool details until the user opens it");

  const imageViewMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex",
    done: true,
    openOverride: true,
    blocks: [{
      kind: "process",
      item_id: "view-1",
      processKind: "server_tool",
      phase: "end",
      status: "succeeded",
      title: "查看图片",
      tool: "view_image",
      input: {
        file_path: "/tmp/chart.png",
        preview_id: "view-1",
      },
      done: true,
    }],
    imageAssets: {
      "view-1": {
        status: "ready",
        mediaType: "image/png",
        data: "iVBORw0KGgo=",
      },
    },
  }));
  assert.match(imageViewMarkup, /class="process-image-preview"/);
  assert.match(imageViewMarkup, /\/tmp\/chart\.png/);
  assert.match(imageViewMarkup, /data:image\/png;base64,iVBORw0KGgo=/);
  assert.doesNotMatch(imageViewMarkup, /&quot;preview_id&quot;/,
    "image-view metadata must not render as a raw JSON tool payload");

  const pendingImageViewMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex",
    done: false,
    openOverride: true,
    blocks: [{
      kind: "process",
      item_id: "view-pending",
      processKind: "server_tool",
      phase: "start",
      status: "running",
      title: "查看图片",
      tool: "view_image",
      input: { file_path: "/tmp/pending.png" },
      done: false,
    }],
  }));
  assert.match(pendingImageViewMarkup, /process-image-preview[^>]*disabled/,
    "a live image view stays inert until its immutable snapshot is ready");

  const historyImageViewMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex",
    done: true,
    openOverride: true,
    historyTurnId: "turn-1",
    blocks: [{
      kind: "process",
      item_id: "history-view-1",
      processKind: "server_tool",
      phase: "end",
      status: "succeeded",
      title: "查看图片",
      tool: "view_image",
      input: {
        file_path: "/tmp/history.png",
        history_image: {
          image_id: "img-history-1",
          media_type: "image/png",
          width: 1,
          height: 1,
          byte_size: 68,
        },
      },
      done: true,
    }],
    historyImageAssets: {
      [historyImageAssetKey(
        "turn-1", "img-history-1", "thumbnail")]: {
        status: "ready",
        mediaType: "image/webp",
        data: "UklGRg==",
      },
    },
  }));
  assert.match(historyImageViewMarkup, /\/tmp\/history\.png/);
  assert.match(historyImageViewMarkup, /data:image\/webp;base64,UklGRg==/);
  assert.doesNotMatch(historyImageViewMarkup, /&quot;history_image&quot;/);

  function verifyFileChangeMarkup() {
    const noNativeDiffMarkup = renderToStaticMarkup(createElement(ChatView, {
      sid: "historical-file-without-diff",
      engine: "codex",
      turns: [{
        id: "historical-file-without-diff-turn",
        prompt: "update the file",
        done: true,
        blocks: [{
          kind: "tool",
          message_id: "historical-file-message",
          tool_use_id: "historical-file-tool",
          tool: "fileChange",
          input: {
            changes: [{ path: "/repo/existing.ts", kind: "update" }],
          },
          done: true,
          result: { content: "updated", is_error: false },
        }],
      }],
      // Legacy callers may still pass this callback. Historical cards must not
      // use it to read today's worktree when this turn persisted no native diff.
      onGetDiff: () => {},
      onOpenTurnDiff: () => {},
    }));
    assert.match(noNativeDiffMarkup,
      /class="turn-changes-toggle"[^>]*aria-expanded="false"/);
    assert.doesNotMatch(noNativeDiffMarkup, /class="turn-changes-files"/);
    const unavailableFiles = renderToStaticMarkup(createElement(TurnChangesPanel, {
      turn: { id: "missing", blocks: [], done: true, fileChanges: {
        revision: "missing-revision", files: [{ path: "/repo/existing.ts", state: "unavailable" }],
      } }, open: true, onToggle: () => {}, work: false, onOpenDiff: () => {},
    }));
    assert.match(unavailableFiles, /class="turn-change-path" disabled=""/);
    assert.match(unavailableFiles, /差异未保存/);

    const nativeDiffMarkup = renderToStaticMarkup(createElement(ChatView, {
      sid: "historical-file-with-native-diff",
      engine: "codex",
      turns: [{
        id: "historical-file-with-native-diff-turn",
        prompt: "update the file",
        done: true,
        blocks: [{
          kind: "tool",
          message_id: "native-diff-message",
          tool_use_id: "native-diff-tool",
          tool: "fileChange",
          input: {
            changes: [{ path: "/repo/existing.ts", kind: "update" }],
          },
          done: true,
          result: {
            content: "updated",
            is_error: false,
            diff: "--- /repo/existing.ts\n+++ /repo/existing.ts\n@@ -1 +1 @@\n-old\n+new",
          },
        }],
      }],
      onOpenTurnDiff: () => {},
    }));
    assert.match(nativeDiffMarkup, /class="turn-changes-toggle"[^>]*aria-expanded="false"/);
    assert.doesNotMatch(nativeDiffMarkup, /class="turn-changes-files"/);

    const markdownWithoutDiffMarkup = renderToStaticMarkup(createElement(ChatView, {
      sid: "historical-markdown-without-diff",
      engine: "codex",
      turns: [{
        id: "historical-markdown-without-diff-turn",
        prompt: "update docs",
        done: true,
        blocks: [{
          kind: "tool",
          message_id: "historical-markdown-message",
          tool_use_id: "historical-markdown-tool",
          tool: "fileChange",
          input: { changes: [{ path: "/repo/README.md", kind: "update" }] },
          done: true,
          result: { content: "updated", is_error: false },
        }],
      }],
      onPreviewMarkdown: () => {},
    }));
    assert.match(markdownWithoutDiffMarkup, /class="turn-changes-toggle"/);
    const markdownPreview = renderToStaticMarkup(createElement(TurnChangesPanel, {
      turn: { id: "markdown", blocks: [], done: true, fileChanges: {
        revision: "md-revision", files: [{ path: "/repo/README.md", state: "unavailable" }],
      } }, open: true, onToggle: () => {}, work: false, onPreviewMarkdown: () => {},
    }));
    assert.match(markdownPreview, /class="turn-change-preview"[^>]*>预览当前文件/,
      "Current Markdown preview remains separate from unavailable historical diff");
    const workFiles = renderToStaticMarkup(createElement(TurnChangesPanel, {
      turn: { id: "work-file", blocks: [], done: true, fileChanges: {
        revision: "work-revision", files: [{ path: "/work/report.md", state: "unavailable" }],
      } }, open: true, onToggle: () => {}, work: true,
      onOpenFile: () => {}, onOpenArtifacts: () => {},
    }));
    assert.match(workFiles, /Artifacts/);
    assert.match(workFiles, /查看 Artifacts/);
    assert.doesNotMatch(workFiles, /turn-change-path" disabled|差异未保存|预览当前文件/,
      "Work artifacts open their file viewer independently of historical Code diffs");
  }
  verifyFileChangeMarkup();

  const codexHookWrappedBatchMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex", done: false,
    blocks: [
      { kind: "process", item_id: "hook-a", processKind: "hook",
        phase: "end", status: "succeeded", title: "Hook · preToolUse · command",
        done: true },
      { kind: "tool", message_id: "tool-a-message", tool_use_id: "tool-a",
        tool: "shell", input: { command: "pwd" }, done: true,
        result: { content: "/tmp", is_error: false } },
      { kind: "process", item_id: "hook-b", processKind: "hook",
        phase: "end", status: "succeeded", title: "Hook · preToolUse · command",
        done: true },
      { kind: "tool", message_id: "tool-b-message", tool_use_id: "tool-b",
        tool: "shell", input: { command: "ls" }, done: true,
        result: { content: "file", is_error: false } },
    ],
  }));
  assert.match(codexHookWrappedBatchMarkup, /2 个工具调用/);
  assert.equal((codexHookWrappedBatchMarkup.match(/class="tool-group"/g) || []).length, 1,
    "successful Codex hooks must not split one tool batch into many rows");
  assert.doesNotMatch(codexHookWrappedBatchMarkup, /Hook · preToolUse/);
  const deferredToolMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex",
    done: false,
    deferredCount: 20,
    onLoadDetail: () => {},
    blocks: [{
      kind: "tool",
      message_id: "summary-placeholder-message",
      tool_use_id: "summary-placeholder-tool",
      tool: "shell",
      category: "command",
      title: "运行命令",
      input: {},
      done: false,
    }],
  }));
  assert.match(deferredToolMarkup, /20 项/);
  assert.doesNotMatch(deferredToolMarkup, /class="tool-group"/,
    "payload-free summary tools stay hidden until authoritative detail arrives");
  assert.doesNotMatch(deferredToolMarkup, /运行命令/,
    "summary placeholders cannot render an empty command card");
  const retainedZeroArgToolMarkup = renderToStaticMarkup(
    createElement(ProcessTimeline, {
      engine: "codex",
      done: true,
      deferredCount: 20,
      openOverride: true,
      onLoadDetail: () => {},
      blocks: [{
        kind: "tool",
        message_id: "retained-zero-arg-message",
        tool_use_id: "retained-zero-arg-tool",
        tool: "list_resources",
        title: "List resources",
        input: {},
        done: true,
        result: { content: "", is_error: false, status: "succeeded" },
      }],
    }),
  );
  assert.match(
    retainedZeroArgToolMarkup,
    /class="tool-group"/,
    "a completed zero-argument cached tool is real retained detail, not a summary shell",
  );
  const retainedCommandMetadataMarkup = renderToStaticMarkup(
    createElement(ProcessTimeline, {
      engine: "codex",
      done: true,
      deferredCount: 20,
      openOverride: true,
      onLoadDetail: () => {},
      blocks: [{
        kind: "process",
        item_id: "retained-command-metadata",
        processKind: "command",
        phase: "end",
        status: "succeeded",
        title: "Command finished",
        exit_code: 0,
        duration_ms: 1250,
        done: true,
      }],
    }),
  );
  assert.match(
    retainedCommandMetadataMarkup,
    /Command finished/,
    "completed command metadata remains visible while authoritative detail is deferred",
  );
  assert.match(retainedCommandMetadataMarkup, /exit 0/);
  assert.match(retainedCommandMetadataMarkup, /1s/);

  const codexFailedHookMarkup = renderToStaticMarkup(createElement(ProcessTimeline, {
    engine: "codex", done: false,
    blocks: [{ kind: "process", item_id: "hook-failed", processKind: "hook",
      phase: "end", status: "failed", title: "Hook 执行失败", done: true }],
  }));
  assert.match(codexFailedHookMarkup, /Hook 执行失败/,
    "actionable Codex hook failures must remain visible");
  const { ToolGroup } = await reducerHarness.ssrLoadModule(
    "/src/components/ToolGroup.tsx");
  const collapsedToolMarkup = renderToStaticMarkup(createElement(ToolGroup, {
    tools: [
      { kind: "tool", message_id: "tool-a-message", tool_use_id: "tool-a",
        tool: "shell", input: { command: "pwd" }, done: true,
        result: { content: "/tmp", is_error: false } },
      { kind: "tool", message_id: "tool-b-message", tool_use_id: "tool-b",
        tool: "shell", input: { command: "ls" }, done: true,
        result: { content: "file", is_error: false } },
    ],
  }));
  assert.match(collapsedToolMarkup, /2 个工具调用/);
  assert.doesNotMatch(collapsedToolMarkup, /<details class="tool-group" open/,
    "Codex tool batches must stay collapsed until the user opens them");

  // A background task can be consumed after ResultMessage, when a new turn is
  // already open. Its authoritative engine turn id must route it back to the
  // old turn instead of creating a phantom third turn or attaching to the tail.
  state.runtimes[richSid].turns[0].clientMsgId = "turn-rich-client";
  state.runtimes[richSid].turns[0].historyTurnId = "turn-rich-history";
  state = reduce(state, { type: "query_sent", sid: richSid, prompt: "下一问",
    msg_id: "rich-next", ts: 34_000 });
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: richSid, item_id: "late-agent", kind: "agent",
    phase: "end", status: "succeeded", turn_id: "turn-rich-client",
    title: "后台代理完成", background: true,
  }) });
  assert.equal(state.runtimes[richSid].turns.length, 2);
  assert.ok(state.runtimes[richSid].turns[0].blocks.some(
    (block: { kind: string; item_id?: string }) => block.kind === "process"
      && block.item_id === "late-agent"));
  assert.ok(!state.runtimes[richSid].turns[1].blocks.some(
    (block: { kind: string; item_id?: string }) => block.item_id === "late-agent"));

  // Autonomous Claude narrative uses the same explicit owner. Text and tool
  // frames must not fall through to the newer optimistic question merely
  // because their message/tool ids have not been seen before.
  state = reduce(state, { type: "event", event: event({
    type: "assistant_msg_start", sid: richSid, message_id: "late-answer",
    turn_id: "turn-rich-client", background: true, channel: "commentary",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "delta", sid: richSid, message_id: "late-answer",
    turn_id: "turn-rich-client", background: true, channel: "commentary",
    text: "后台结果正文",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "tool_use", sid: richSid, message_id: "late-answer",
    tool_use_id: "late-read", turn_id: "turn-rich-history", background: true,
    tool: "Read", input: { file_path: "README.md" },
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "tool_result", sid: richSid, tool_use_id: "late-read",
    turn_id: "turn-rich-history", background: true, content: "done", is_error: false,
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "assistant_msg_end", sid: richSid, message_id: "late-answer",
    turn_id: "turn-rich-client", background: true, channel: "commentary",
  }) });
  const oldNarrativeOwner = state.runtimes[richSid].turns[0];
  const newNarrativeTail = state.runtimes[richSid].turns[1];
  assert.ok(oldNarrativeOwner.blocks.some((block: Block) => block.kind === "text"
    && block.message_id === "late-answer" && block.text === "后台结果正文"));
  assert.ok(oldNarrativeOwner.blocks.some((block: Block) => block.kind === "tool"
    && block.tool_use_id === "late-read" && block.done));
  assert.ok(!newNarrativeTail.blocks.some((block: Block) => block.kind === "text"
    ? block.message_id === "late-answer"
    : block.kind === "tool" && block.tool_use_id === "late-read"));

  // A late detached background update remains visible inside the completed
  // turn without reopening its session-level working affordance.
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: richSid, item_id: "late-agent", kind: "agent",
    phase: "update", status: "running", turn_id: "turn-rich",
    title: "后台代理运行中", progress: "继续检查", background: true,
  }) });
  const backgroundTurn = state.runtimes[richSid].turns[0];
  assert.equal(backgroundTurn.done, true);
  assert.equal(hasActiveProcess(backgroundTurn.blocks), true);
  const backgroundMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid: richSid, turns: [backgroundTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {},
  }));
  assert.doesNotMatch(backgroundMarkup, /class="turn-working"/,
    "detached Agent work cannot reopen the completed turn's top-level spark");
  assert.match(
    backgroundMarkup,
    /turn-process-state done"><svg/,
    "the completed parent header stays settled while Agent detail remains live",
  );
  assert.match(backgroundMarkup, /class="turn-done-mark"/);
  state = reduce(state, { type: "event", event: event({
    type: "process", sid: richSid, item_id: "late-agent", kind: "agent",
    phase: "end", status: "succeeded", turn_id: "turn-rich",
    title: "后台代理完成", background: true,
  }) });
  assert.equal(hasActiveProcess(state.runtimes[richSid].turns[0].blocks), false);

  const forkableTurn = {
    id: "message-a", forkPointId: "codex-turn-a", prompt: "在？",
    done: true, doneTs: 12_000,
    blocks: [{ kind: "text", message_id: "answer-a", text: "在的", done: true }],
  };
  const codexMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, turns: [forkableTurn], engine: "codex",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.doesNotMatch(codexMarkup, /复制回复/);
  assert.match(codexMarkup, /class="ubub-meta ai-meta"[\s\S]*aria-label="复制"/);
  assert.match(codexMarkup,
    /class="ubub-meta ai-meta"[\s\S]*?<button[^>]*aria-label="复制"[^>]*><svg[^>]*>[\s\S]*?<rect x="8" y="8" width="14" height="14" rx="2"\/><path d="M4 16a2 2 0 01-2-2V4a2 2 0 012-2h10a2 2 0 012 2"/);
  assert.match(codexMarkup, /aria-label="派生"/);
  assert.match(codexMarkup, /data-tooltip="从此回复派生新会话"/);
  assert.doesNotMatch(codexMarkup, /title="从此回复派生/);
  const assistantMeta = codexMarkup.slice(codexMarkup.indexOf('class="ubub-meta ai-meta"'));
  assert.ok(assistantMeta.indexOf('aria-label="派生"')
    > assistantMeta.indexOf('aria-label="复制"'),
  "the reply copy icon stays immediately before the fork action");
  const claudeMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, turns: [forkableTurn], engine: "claude",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.match(claudeMarkup, /aria-label="派生"/);
  assert.match(claudeMarkup, /data-tooltip="从此回复派生新会话"/);
  const noForkPointMarkup = renderToStaticMarkup(createElement(ChatView, {
    sid, turns: [{ ...forkableTurn, forkPointId: undefined }], engine: "claude",
    onEdit: () => {}, onGetDiff: () => {}, onFork: () => {},
  }));
  assert.doesNotMatch(noForkPointMarkup, /aria-label="派生"/);

  state = reduce(state, { type: "event", event: event({
    type: "takeover_state", sid, pending: true, message: "等待当前回复结束",
  }) });
  assert.equal(state.runtimes[sid].takeoverPending, true);
  assert.equal(state.runtimes[sid].takeoverMessage, "等待当前回复结束");
  state = reduce(state, { type: "event", event: event({
    type: "takeover_state", sid, pending: false,
  }) });
  assert.equal(state.runtimes[sid].takeoverPending, false);
  assert.equal(state.runtimes[sid].takeoverMessage, null);

  // v15 control is authoritative and independently revisioned. Older and
  // equal-revision conflicting frames must never revive a completed lock.
  const controlSid = "revisioned-session-control";
  const control = (
    revision: number,
    control_mode: "remote" | "codex_shared" | "claude_broker" | "external_cli" | "agent_view" | "desktop",
    write_state: "writable" | "read_only" | "takeover_pending" | "input_busy",
    extra: Record<string, unknown> = {},
  ): SessionControl => event({
    type: "session_control", sid: controlSid, revision, control_mode,
    write_state, terminal_attached: false,
    generation: "control-generation", ...extra,
  }) as SessionControl;
  let controlState = reduce({
    ...initialState, focusedSid: controlSid,
    runtimes: { [controlSid]: createRuntime() },
  }, { type: "event", event: control(
    5, "external_cli", "read_only", { can_takeover: true }) });
  assert.equal(controlState.runtimes[controlSid].control?.revision, 5);
  assert.equal(controlState.runtimes[controlSid].external, true);

  controlState = reduce(controlState, { type: "event", event: control(
    4, "remote", "writable") });
  controlState = reduce(controlState, { type: "event", event: control(
    5, "desktop", "read_only") });
  assert.equal(controlState.runtimes[controlSid].control?.control_mode,
    "external_cli", "older/conflicting revisions must be rejected");
  controlState = reduce(controlState, { type: "event", event: event({
    type: "takeover_state", sid: controlSid, pending: true,
    message: "legacy frame arrived late",
  }) });
  assert.equal(controlState.runtimes[controlSid].takeoverPending, false,
    "legacy TakeoverState must be ignored after v15 control exists");

  controlState = reduce(controlState, { type: "event", event: event({
    type: "history", sid: controlSid, session_id: controlSid,
    revision: "control-history", generation: "control-generation",
    build_seq: 2, has_more: false, events: [],
    control: control(6, "remote", "writable"),
  }) });
  assert.equal(controlState.runtimes[controlSid].external, false);
  // This narrative page is stale (build 1 < build 2), but its separately
  // revisioned control value is newer and must still be installed.
  controlState = reduce(controlState, { type: "event", event: event({
    type: "history", sid: controlSid, session_id: controlSid,
    revision: "control-history", generation: "control-generation",
    build_seq: 1, has_more: false, events: [],
    control: control(7, "desktop", "read_only"),
  }) });
  assert.equal(controlState.runtimes[controlSid].control?.revision, 7);
  assert.equal(controlState.runtimes[controlSid].external, true);

  controlState = reduce(controlState, { type: "event", event: control(
    8, "remote", "writable") });
  controlState = reduce(controlState, { type: "event", event: event({
    type: "session_control", revision: 999,
    control_mode: "desktop", write_state: "read_only",
    terminal_attached: true, generation: "control-generation",
  }) });
  assert.equal(controlState.runtimes[controlSid].control?.revision, 8,
    "an unrouted direct control frame cannot target the focused runtime");
  controlState = reduce(controlState, { type: "event", event: event({
    type: "history", sid: controlSid, session_id: controlSid,
    revision: "control-history", generation: "control-generation",
    build_seq: 3, live_seq: 0, has_more: false, events: [],
    external: true, takeover_pending: true,
    control: control(7, "external_cli", "takeover_pending"),
  }) });
  assert.equal(controlState.runtimes[controlSid].control?.revision, 8);
  assert.equal(controlState.runtimes[controlSid].external, false,
    "stale modern plus legacy ownership fields cannot resurrect the lock");
  assert.equal(controlState.runtimes[controlSid].takeoverPending, false);

  controlState = reduce(controlState, { type: "event", event: event({
    type: "snapshot", sid: controlSid, cc_session_id: controlSid,
    state: "idle", tail_text: "",
    control: control(9, "codex_shared", "writable", {
      terminal_attached: true,
    }),
  }) });
  assert.equal(controlState.runtimes[controlSid].control?.control_mode,
    "codex_shared");
  assert.equal(controlState.runtimes[controlSid].external, false,
    "a terminal-attached shared Codex session remains writable");

  const crossSessionControl = control(
    100, "external_cli", "read_only", { sid: "another-session" });
  assert.equal(controlForCachedSession(controlSid, crossSessionControl), undefined,
    "a cache row must drop a control explicitly routed to another session");
  controlState = reduce(controlState, { type: "event", event: event({
    type: "snapshot", sid: controlSid, cc_session_id: controlSid,
    state: "idle", tail_text: "", generation: "control-generation",
    control: crossSessionControl,
  }) });
  assert.equal(controlState.runtimes[controlSid].control?.revision, 9,
    "a Snapshot must not install another session's nested control");
  controlState = reduce(controlState, { type: "event", event: event({
    type: "history", sid: controlSid, session_id: controlSid,
    revision: "cross-session-control-history",
    generation: "control-generation", build_seq: 4,
    has_more: false, events: [], control: crossSessionControl,
  }) });
  assert.equal(controlState.runtimes[controlSid].control?.revision, 9,
    "History must not install another session's nested control");

  let cachedControlState = reduce({
    ...initialState, focusedSid: "cached-control",
  }, {
    type: "hydrate_cache", sid: "cached-control", turns: [], revision: null,
    control: event({
      type: "session_control", sid: "cached-control", revision: 20,
      control_mode: "remote", write_state: "writable",
      terminal_attached: false, generation: "cached-generation-old",
    }) as SessionControl,
    generation: "cached-generation-old",
  });
  cachedControlState = reduce(cachedControlState, {
    type: "event", event: event({
      type: "snapshot", sid: "cached-control", cc_session_id: "cached-control",
      state: "idle", tail_text: "", generation: "cached-generation-old",
      control: event({
        type: "session_control", sid: "cached-control", revision: 19,
        control_mode: "desktop", write_state: "read_only",
        terminal_attached: true, generation: "cached-generation-old",
      }),
    }),
  });
  assert.equal(cachedControlState.runtimes["cached-control"].control?.revision, 20);
  assert.equal(cachedControlState.runtimes["cached-control"].external, false,
    "an older reconnect snapshot cannot revive a cache-superseded lock");
  const mismatchedCachedControl = reduce(initialState, {
    type: "hydrate_cache", sid: "mismatched-cache", turns: [], revision: null,
    generation: "trusted-cache-generation",
    control: event({
      type: "session_control", sid: "mismatched-cache", revision: 99,
      control_mode: "desktop", write_state: "read_only",
      terminal_attached: true, generation: "stale-cache-generation",
    }) as SessionControl,
  });
  assert.equal(mismatchedCachedControl.runtimes["mismatched-cache"].control, null,
    "a control snapshot cannot override its cache row's trusted generation");
  const crossSidCachedControl = reduce(initialState, {
    type: "hydrate_cache", sid: "cache-row-a", turns: [], revision: null,
    control: event({
      type: "session_control", sid: "cache-row-b", revision: 100,
      control_mode: "external_cli", write_state: "read_only",
      terminal_attached: true, generation: "cross-cache-generation",
    }) as SessionControl,
  });
  assert.equal(crossSidCachedControl.runtimes["cache-row-a"].control, null);
  assert.equal(crossSidCachedControl.runtimes["cache-row-a"].controlGeneration, null,
    "a mismatched cache control must not bind its generation to another row");
  cachedControlState = reduce(cachedControlState, {
    type: "event", event: event({
      type: "snapshot", sid: "cached-control", cc_session_id: "cached-control",
      state: "idle", tail_text: "", generation: "cached-generation-new",
      control: event({
        type: "session_control", sid: "cached-control", revision: 0,
        control_mode: "remote", write_state: "writable",
        terminal_attached: false, generation: "cached-generation-new",
      }),
    }),
  });
  assert.equal(cachedControlState.runtimes["cached-control"].control?.revision, 0,
    "a trusted outer generation switch starts a fresh revision epoch");
  cachedControlState = reduce(cachedControlState, {
    type: "event", event: event({
      type: "session_control", sid: "cached-control", revision: 999,
      control_mode: "desktop", write_state: "read_only",
      terminal_attached: true, generation: "cached-generation-old",
    }),
  });
  assert.equal(cachedControlState.runtimes["cached-control"].control?.control_mode,
    "remote", "a high revision from the previous generation must be rejected");

  const generationlessSid = "generationless-control-migration";
  let generationlessState = reduce({
    ...initialState, focusedSid: generationlessSid,
    runtimes: { [generationlessSid]: createRuntime() },
  }, { type: "event", event: event({
    type: "session_control", sid: generationlessSid, revision: 1,
    control_mode: "external_cli", write_state: "read_only",
    terminal_attached: true,
  }) });
  assert.equal(generationlessState.runtimes[generationlessSid].control?.revision, 1,
    "a generation-less control remains compatible with a generation-less runtime");
  generationlessState = reduce(generationlessState, {
    type: "event", event: event({
      type: "snapshot", sid: generationlessSid, cc_session_id: generationlessSid,
      state: "idle", tail_text: "", generation: "migration-generation",
      control: event({
        type: "session_control", sid: generationlessSid, revision: 0,
        control_mode: "remote", write_state: "writable",
        terminal_attached: false, generation: "migration-generation",
      }),
    }),
  });
  generationlessState = reduce(generationlessState, {
    type: "event", event: event({
      type: "session_control", sid: generationlessSid, revision: 999,
      control_mode: "desktop", write_state: "read_only",
      terminal_attached: true,
    }),
  });
  assert.equal(generationlessState.runtimes[generationlessSid].control?.control_mode,
    "remote", "generation-less control cannot overwrite a bound runtime epoch");

  const compatibilityGapSid = "revisioned-control-generation-gap";
  let compatibilityGapState = reduce({
    ...initialState, focusedSid: compatibilityGapSid,
    runtimes: { [compatibilityGapSid]: createRuntime() },
  }, { type: "event", event: event({
    type: "session_control", sid: compatibilityGapSid, revision: 8,
    control_mode: "remote", write_state: "writable",
    terminal_attached: false, generation: "gap-generation-old",
  }) });
  compatibilityGapState = reduce(compatibilityGapState, {
    type: "event", event: event({
      type: "replay_start", sid: compatibilityGapSid,
      from_seq: 0, to_seq: 0, truncated: false,
      generation: "gap-generation-new",
    }),
  });
  assert.equal(compatibilityGapState.runtimes[compatibilityGapSid].control, null);
  compatibilityGapState = reduce(compatibilityGapState, {
    type: "event", event: event({
      type: "takeover_state", sid: compatibilityGapSid, pending: true,
      message: "late legacy lock",
    }),
  });
  assert.equal(
    compatibilityGapState.runtimes[compatibilityGapSid].takeoverPending, false,
    "legacy takeover cannot revive a lock while a new revision epoch is seeding",
  );
  compatibilityGapState = reduce(compatibilityGapState, {
    type: "event", event: event({
      type: "history", sid: compatibilityGapSid,
      session_id: compatibilityGapSid, revision: "gap-history",
      generation: "gap-generation-new", build_seq: 1,
      has_more: false, events: [], external: true, takeover_pending: true,
    }),
  });
  assert.equal(compatibilityGapState.runtimes[compatibilityGapSid].external, false,
    "legacy History ownership cannot regain authority after revisioned control");

  let legacyControlState = reduce({
    ...initialState, focusedSid: "legacy-control",
    runtimes: { "legacy-control": createRuntime() },
  }, { type: "event", event: event({
    type: "takeover_state", sid: "legacy-control", pending: true,
    message: "等待旧式接管",
  }) });
  assert.equal(legacyControlState.runtimes["legacy-control"].takeoverPending, true);
  legacyControlState = reduce(legacyControlState, { type: "event", event: event({
    type: "takeover_state", sid: "legacy-control", pending: false,
  }) });
  assert.equal(legacyControlState.runtimes["legacy-control"].takeoverPending, false);

  const { presentLegacyExternalControl, presentSessionControl } = await reducerHarness.ssrLoadModule(
    "/src/session-control-ui.ts");
  const sharedControl = presentSessionControl(control(
    30, "codex_shared", "writable", { terminal_attached: true }));
  assert.equal(sharedControl.locked, false);
  assert.equal(sharedControl.title, "终端双向连接");
  assert.equal(sharedControl.tone, "attached");
  assert.match(sharedControl.detail, /浏览器与终端共享同一会话/);
  assert.doesNotMatch(sharedControl.detail, /占用/);
  const passiveSharedControl = presentSessionControl(control(
    31, "codex_shared", "writable", { terminal_attached: false }));
  assert.equal(passiveSharedControl.tone, "remote",
    "an available daemon stays visually neutral until this session has a terminal");
  assert.equal(passiveSharedControl.title, "Codex 后台通道可用");
  assert.equal(passiveSharedControl.backend, "可用");
  assert.equal(passiveSharedControl.terminal, "未检测到本机终端",
    "a passive Codex daemon is shown as backend topology, not terminal ownership");
  assert.doesNotMatch(passiveSharedControl.title, /已连接/,
    "a live shared daemon must not be presented as a live terminal connection");
  const reconnectingSharedControl = presentSessionControl(control(
    32, "codex_shared", "writable", {
      terminal_attached: true,
      reason: "Codex 共享通道连接断开；下次操作会自动重试",
    }));
  assert.equal(reconnectingSharedControl.locked, false,
    "an interrupted Codex proxy remains writable instead of becoming an external lock");
  assert.equal(reconnectingSharedControl.disconnected, true);
  assert.equal(reconnectingSharedControl.action, undefined);
  assert.doesNotMatch(reconnectingSharedControl.remote, /只读/);
  const passiveBrokerControl = presentSessionControl(control(
    33, "claude_broker", "writable", { terminal_attached: false }));
  assert.equal(passiveBrokerControl.tone, "remote",
    "Claude and Codex use the same neutral state without an attached terminal");
  assert.equal(passiveBrokerControl.title, "Claude Broker 通道可用");
  assert.equal(passiveBrokerControl.terminal, "未检测到本机终端");
  const attachedBrokerControl = presentSessionControl(control(
    34, "claude_broker", "writable", { terminal_attached: true }));
  assert.equal(attachedBrokerControl.tone, "attached");
  assert.match(attachedBrokerControl.detail, /浏览器与终端共享同一会话/);
  const externalControl = presentSessionControl(control(
    35, "external_cli", "read_only", { can_takeover: true }));
  assert.equal(externalControl.locked, true);
  assert.equal(externalControl.action, "迁移");
  assert.equal(externalControl.tone, "attention");
  assert.match(externalControl.detail, /只读.*接管/);
  const desktopControl = presentSessionControl(control(
    36, "desktop", "writable", { can_takeover: true }));
  assert.equal(desktopControl.locked, true,
    "desktop remains fail-closed even if a producer says writable");
  assert.equal(desktopControl.action, undefined);
  assert.equal(desktopControl.remote, "只读");
  assert.equal(desktopControl.title, "Codex App 使用中 · Web 只读");
  assert.equal(desktopControl.placeholder, "Codex App 使用中 — Web 只读");
  assert.equal(desktopControl.connection, "Codex App 私有 app-server");
  assert.equal(desktopControl.terminal, "Codex App 使用中");
  const exitedBrokerControl = presentSessionControl(control(
    37, "claude_broker", "input_busy", {
      terminal_attached: false,
      reason: "session_exited: Claude broker 已断开",
    }));
  assert.equal(exitedBrokerControl.tone, "disconnected");
  assert.equal(exitedBrokerControl.title, "终端连接已断开");
  assert.equal(exitedBrokerControl.remote, "等待恢复");
  assert.doesNotMatch(exitedBrokerControl.placeholder ?? "", /输入.*忙碌/,
    "a dead broker must not be presented as a merely busy input channel");
  const legacyExternalControl = presentLegacyExternalControl(
    "claude", false, "旧版外部终端");
  assert.equal(legacyExternalControl.locked, true);
  assert.equal(legacyExternalControl.action, "迁移");
  assert.match(legacyExternalControl.detail, /旧版外部终端/);

  const { TerminalControl } = await reducerHarness.ssrLoadModule(
    "/src/components/TerminalControl.tsx");
  const terminalControlMarkup = renderToStaticMarkup(createElement(TerminalControl, {
    control: control(37, "codex_shared", "writable", { terminal_attached: false }),
    engine: "codex",
    availability: "online",
  }));
  assert.match(terminalControlMarkup, /终端状态：Codex 后台通道可用/);
  assert.match(terminalControlMarkup, /tone-remote/);
  assert.doesNotMatch(terminalControlMarkup, /tone-attached/,
    "a passive Codex app-server must not use the connected accent colour");
  assert.doesNotMatch(terminalControlMarkup, /terminal-control-card/,
    "the status card stays closed until the terminal icon is clicked");
  const staleTerminalControlMarkup = renderToStaticMarkup(createElement(TerminalControl, {
    control: control(38, "claude_broker", "writable", { terminal_attached: true }),
    engine: "claude",
    availability: "offline",
  }));
  assert.match(staleTerminalControlMarkup, /终端状态：连接中断/);
  assert.match(staleTerminalControlMarkup, /tone-disconnected/);
  assert.doesNotMatch(staleTerminalControlMarkup, /tone-attached/,
    "a cached attached state must never render green while transport is offline");
  const legacyTerminalControlMarkup = renderToStaticMarkup(createElement(TerminalControl, {
    engine: "claude",
    availability: "online",
    legacyExternal: true,
  }));
  assert.match(legacyTerminalControlMarkup, /终端状态：外部 CLI 只读镜像/,
    "pre-v15 external state retains a visible terminal entry point");

  const progressSid = "progress";
  state = {
    ...state,
    focusedSid: progressSid,
    runtimes: {
      ...state.runtimes,
      [progressSid]: { ...createRuntime(), state: "running", syncReady: true },
    },
  };
  state = reduce(state, {
    type: "query_sent", sid: progressSid, prompt: "status",
    msg_id: "progress-turn", ts: 20_000,
  });
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, msg_id: "progress-turn",
    state: "running", detail: "上游服务暂时不可用（503），Codex 正在重试…",
    phase: "retrying",
  }) });
  assert.equal(state.runtimes[progressSid].turns[0].done, false);
  assert.match(state.runtimes[progressSid].turns[0].progress ?? "", /503/);
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, msg_id: "progress-turn",
    state: "running", detail: null, phase: null,
  }) });
  assert.equal(state.runtimes[progressSid].turns[0].done, false);
  assert.equal(state.runtimes[progressSid].turns[0].progress, undefined);
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, msg_id: "progress-turn",
    state: "running", detail: "上游服务暂时不可用（503），Codex 正在重试…",
    phase: "retrying",
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "error", sid: progressSid, msg_id: "progress-turn",
    code: "cc_crash", message: "Codex 没有返回任何内容",
  }) });
  assert.equal(state.runtimes[progressSid].turns[0].done, true);
  assert.equal(state.runtimes[progressSid].turns[0].progress, undefined);
  assert.equal(state.runtimes[progressSid].turns[0].error,
    "本次回复未完成，请重试。");
  state = reduce(state, { type: "event", event: event({
    type: "turn_end", sid: progressSid, ts: 21,
    result: { subtype: "error", duration_ms: 237252, is_error: true },
  }) });
  assert.equal(state.runtimes[progressSid].state, "running",
    "TurnEnd closes presentation only; it must not unlock before State(idle)");
  state = reduce(state, {
    type: "set_pending",
    query: { prompt: "send after interrupt drain", msg_id: "queued-replace" },
  });
  state = reduce(state, { type: "event", event: event({
    type: "query_queue", sid: progressSid, items: [{
      msg_id: "queued-replace", kind: "replace",
      prompt_preview: "send after interrupt drain",
      image_count: 0, file_count: 0, retained_bytes: 256,
    }],
    total_count: 1, total_bytes: 256,
  }) });
  state = reduce(state, { type: "event", event: event({
    type: "state", sid: progressSid, state: "idle",
  }) });
  assert.equal(state.runtimes[progressSid].pendingSend?.msg_id, "queued-replace",
    "browser idle state must not consume wrapper-owned queued work");
  state = reduce(state, { type: "event", event: event({
    type: "query_queue", sid: progressSid, items: [],
    total_count: 0, total_bytes: 0,
  }) });
  assert.equal(state.runtimes[progressSid].pendingSend, null,
    "only the wrapper's authoritative queue projection removes deferred work");

  const rejectedSid = "rejected-deferred";
  let rejectedState = {
    ...initialState,
    focusedSid: rejectedSid,
    runtimes: { [rejectedSid]: createRuntime() },
  };
  rejectedState = reduce(rejectedState, {
    type: "enqueue",
    sid: rejectedSid,
    query: {
      msg_id: "rejected-message",
      prompt: "complete rejected instruction",
      images: [{ media_type: "image/png", data: "full-image-body" }],
      files: [{ filename: "context.txt", data: "full-file-body" }],
    },
  });
  rejectedState = reduce(rejectedState, {
    type: "event",
    event: event({
      type: "error",
      sid: rejectedSid,
      msg_id: "rejected-message",
      request_id: "rejected-command",
      code: "queue_full",
      message: "server queue full",
    }),
  });
  assert.equal(rejectedState.runtimes[rejectedSid].queue.length, 0);
  assert.deepEqual(
    rejectedState.runtimes[rejectedSid].failedDeferred[0],
    {
      msg_id: "rejected-message",
      prompt: "complete rejected instruction",
      images: [{ media_type: "image/png", data: "full-image-body" }],
      files: [{ filename: "context.txt", data: "full-file-body" }],
      queueKind: "queue",
      queueState: "failed",
      queueError: rejectedState.runtimes[rejectedSid]
        .failedDeferred[0].queueError,
      retainedBytes: queuedQueryWireBytes({
        prompt: "complete rejected instruction",
        images: [{ media_type: "image/png", data: "full-image-body" }],
        files: [{ filename: "context.txt", data: "full-file-body" }],
      }),
      failedAt: rejectedState.runtimes[rejectedSid]
        .failedDeferred[0].failedAt,
    },
    "a wrapper rejection must retain the complete retryable browser payload",
  );

  let acceptedQueueState = {
    ...initialState,
    focusedSid: rejectedSid,
    runtimes: { [rejectedSid]: createRuntime() },
  };
  acceptedQueueState = reduce(acceptedQueueState, {
    type: "enqueue",
    sid: rejectedSid,
    query: {
      msg_id: "accepted-deferred",
      prompt: "full prompt that becomes a bounded preview",
      images: [{ media_type: "image/png", data: "discard-after-acceptance" }],
    },
  });
  acceptedQueueState = reduce(acceptedQueueState, {
    type: "event",
    event: event({
      type: "query_queue",
      sid: rejectedSid,
      items: [{
        msg_id: "accepted-deferred",
        kind: "queue",
        prompt_preview: "full prompt that becomes a bounded preview",
        image_count: 1,
        file_count: 0,
        retained_bytes: 4096,
        error: "daemon reconnect pending",
      }],
      total_count: 1,
      total_bytes: 4096,
    }),
  });
  const acceptedProjection =
    acceptedQueueState.runtimes[rejectedSid].queue[0];
  assert.equal(acceptedProjection.queueState, "queued");
  assert.equal(acceptedProjection.images, undefined,
    "full attachment payload is released only after wrapper acceptance");
  assert.equal(acceptedProjection.retainedBytes, 4096);
  assert.equal(acceptedProjection.queueError, "daemon reconnect pending");
  assert.equal(acceptedQueueState.queryQueueCount, 1);
  assert.equal(acceptedQueueState.queryQueueBytes, 4096);
  acceptedQueueState = reduce(acceptedQueueState, {
    type: "event",
    event: event({
      type: "error",
      sid: rejectedSid,
      msg_id: "accepted-deferred",
      code: "not_running",
      message: "preflight still blocked",
    }),
  });
  assert.equal(
    acceptedQueueState.runtimes[rejectedSid].queue[0].msg_id,
    "accepted-deferred",
    "a launch-preflight error must not remove server-owned queued work",
  );
  assert.equal(
    acceptedQueueState.runtimes[rejectedSid].queue[0].queueError,
    "当前会话暂时不可用，请重新进入后重试。",
  );

  let replacementRejectionState = {
    ...initialState,
    focusedSid: rejectedSid,
    runtimes: { [rejectedSid]: createRuntime() },
  };
  const oldReplacementProjection = event({
    type: "query_queue",
    sid: rejectedSid,
    items: [{
      msg_id: "old-replacement",
      kind: "replace",
      prompt_preview: "existing replacement",
      image_count: 0,
      file_count: 0,
      retained_bytes: 300,
    }],
    total_count: 1,
    total_bytes: 300,
  });
  replacementRejectionState = reduce(replacementRejectionState, {
    type: "event", event: oldReplacementProjection,
  });
  replacementRejectionState = reduce(replacementRejectionState, {
    type: "set_pending",
    sid: rejectedSid,
    query: {
      msg_id: "rejected-replacement",
      prompt: "new replacement whose submission fails",
    },
  });
  replacementRejectionState = reduce(replacementRejectionState, {
    type: "event",
    event: event({
      type: "error",
      sid: rejectedSid,
      msg_id: "rejected-replacement",
      code: "busy",
      message: "replacement is starting",
    }),
  });
  replacementRejectionState = reduce(replacementRejectionState, {
    type: "event", event: oldReplacementProjection,
  });
  assert.equal(
    replacementRejectionState.runtimes[rejectedSid].pendingSend?.msg_id,
    "old-replacement",
    "an unchanged authoritative projection restores a replacement hidden by "
      + "a rejected optimistic submission",
  );
  assert.equal(
    replacementRejectionState.runtimes[rejectedSid].failedDeferred[0].msg_id,
    "rejected-replacement",
  );

  const pinnedBtwSid = "btw-pinned";
  const pinnedBtwTurn = {
    id: "btw-turn", prompt: "侧边问题", blocks: [], done: true, ts: 1,
  };
  let pinnedBtwState = {
    ...initialState,
    focusedSid: "parent-a",
    btwByParentSid: {
      "parent-a": {
        chats: [{ sid: pinnedBtwSid, engine: "codex", createdAt: 1 }],
        activeSid: pinnedBtwSid,
      },
    },
    runtimes: {
      "parent-a": createRuntime(),
      "parent-b": createRuntime(),
      [pinnedBtwSid]: {
        ...createRuntime(),
        turns: [pinnedBtwTurn],
      },
    },
  };
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "focus_session", sid: "parent-b",
  });
  assert.equal(pinnedBtwState.btwByParentSid["parent-b"], undefined,
    "a session without its own BTW must not show another session's fork");
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "event", event: event({
      type: "btw_opened",
      sid: "btw-parent-b",
      request_id: "request-parent-b",
      btw_sid: "btw-parent-b",
      parent_sid: "parent-b",
      engine: "claude",
      created_at: 2,
      revision: 1,
    }),
  });
  assert.deepEqual(pinnedBtwState.btwByParentSid["parent-b"], {
    chats: [{
      sid: "btw-parent-b", engine: "claude", createdAt: 2, state: "idle",
    }],
    activeSid: "btw-parent-b",
  }, "each parent session keeps an independent BTW binding");
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "restore_session_list",
    sessions: [{ session_id: "claude-session", engine: "claude" }],
  });
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "enter_new_chat", cwd: "~", cwdSource: "default",
  });
  assert.deepEqual(pinnedBtwState.btwByParentSid["parent-a"], {
    chats: [{ sid: pinnedBtwSid, engine: "codex", createdAt: 1 }],
    activeSid: pinnedBtwSid,
  });
  assert.deepEqual(
    pinnedBtwState.runtimes[pinnedBtwSid]?.turns,
    [pinnedBtwTurn],
    "session and harness navigation must retain the pinned btw transcript",
  );
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "enqueue",
    sid: pinnedBtwSid,
    query: { msg_id: "btw-queue", prompt: "queued in btw" },
  });
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "set_pending",
    sid: pinnedBtwSid,
    query: { msg_id: "btw-replace", prompt: "replace after interrupt" },
  });
  assert.deepEqual(
    pinnedBtwState.runtimes[pinnedBtwSid]?.queue.map(
      (query: { prompt: string }) => query.prompt),
    ["queued in btw"],
  );
  assert.equal(
    pinnedBtwState.runtimes[pinnedBtwSid]?.pendingSend?.prompt,
    "replace after interrupt",
  );
  assert.deepEqual(pinnedBtwState.runtimes["parent-a"]?.queue, [],
    "BTW queue updates must not mutate the parent runtime");
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "event",
    event: event({
      type: "query_queue",
      sid: pinnedBtwSid,
      items: [
        {
          msg_id: "btw-replace", kind: "replace",
          prompt_preview: "server replacement",
          image_count: 0, file_count: 0, retained_bytes: 300,
        },
        {
          msg_id: "btw-queue", kind: "queue",
          prompt_preview: "server queued",
          image_count: 0, file_count: 0, retained_bytes: 200,
        },
      ],
      total_count: 2,
      total_bytes: 500,
    }),
  });
  assert.equal(
    pinnedBtwState.runtimes[pinnedBtwSid]?.pendingSend?.msg_id,
    "btw-replace",
    "the authoritative BTW replacement stays scoped to its side runtime",
  );
  assert.deepEqual(
    pinnedBtwState.runtimes[pinnedBtwSid]?.queue.map(
      (query: { msg_id?: string }) => query.msg_id),
    ["btw-queue"],
  );
  pinnedBtwState = reduce(pinnedBtwState, {
    type: "clear_btw", parentSid: "parent-a", btwSid: pinnedBtwSid,
  });
  assert.equal(pinnedBtwState.btwByParentSid["parent-a"], undefined);
  assert.equal(pinnedBtwSid in pinnedBtwState.runtimes, false,
    "explicit close discards only that parent's ephemeral BTW runtime");
  assert.deepEqual(pinnedBtwState.btwByParentSid["parent-b"], {
    chats: [{
      sid: "btw-parent-b", engine: "claude", createdAt: 2, state: "idle",
    }],
    activeSid: "btw-parent-b",
  }, "closing one session's BTW must not close a sibling session's BTW");
  assert.equal("btw-parent-b" in pinnedBtwState.runtimes, true);

  pinnedBtwState = reduce(pinnedBtwState, {
    type: "event", event: event({
      type: "session_rekey",
      sid: "parent-real",
      old_key: "parent-b",
      session_id: "parent-real",
    }),
  });
  assert.equal(pinnedBtwState.btwByParentSid["parent-b"], undefined);
  assert.deepEqual(pinnedBtwState.btwByParentSid["parent-real"], {
    chats: [{
      sid: "btw-parent-b", engine: "claude", createdAt: 2, state: "idle",
    }],
    activeSid: "btw-parent-b",
  }, "a parent id capture must carry its BTW binding to the real session id");

  pinnedBtwState = reduce(pinnedBtwState, { type: "clear_all_btw" });
  assert.deepEqual(pinnedBtwState.btwByParentSid, {});
  assert.equal("btw-parent-b" in pinnedBtwState.runtimes, false,
    "wrapper lifecycle reset must discard every ephemeral BTW runtime");

  const { CommandSheet } = await reducerHarness.ssrLoadModule(
    "/src/components/CommandSheet.tsx");
  const picked: string[] = [];
  const tree = CommandSheet({
    open: true, kind: "models", engine: "codex", onClose: () => {},
    onPickModel: (model: string) => { picked.push(model); },
  });
  const clickModelButtons = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(clickModelButtons);
      return;
    }
    if (!node || typeof node !== "object") return;
    const element = node as {
      type?: unknown; key?: string | null;
      props?: { onClick?: () => void; children?: unknown };
    };
    if (element.type === "button"
        && ["gpt-5.6-terra", "gpt-5.6-luna"].includes(element.key ?? "")) {
      element.props?.onClick?.();
    }
    clickModelButtons(element.props?.children);
  };
  clickModelButtons(tree);
  assert.deepEqual(picked, ["gpt-5.6-terra", "gpt-5.6-luna"]);
} finally {
  await reducerHarness.close();
}

const fakePng = new Uint8Array(24);
fakePng.set([0x00, ...new TextEncoder().encode("PNG\r\n\x1a\n")], 0);
fakePng.set(new TextEncoder().encode("IHDR"), 12);
new DataView(fakePng.buffer).setUint32(16, 1);
new DataView(fakePng.buffer).setUint32(20, 1);
assert.equal(imageDimensions(fakePng, "image/png"), null);

// Keep the public RelayWs return contract checked without instantiating a
// browser WebSocket in this zero-network Node test.
const btwWs: Pick<RelayWs, "sendOpenBtw"> = {
  sendOpenBtw: (_parentSid, requestId = "generated-request") => requestId,
};
const btwRequestId = btwWs.sendOpenBtw("parent-1", "btw-request-1");
assert.equal(btwRequestId, "btw-request-1");
const openFrame = makeOpenBtwCommand("parent-1", btwRequestId, 123);
assert.equal(openFrame.type, "open_btw");
assert.equal(openFrame.sid, "parent-1");
assert.equal(openFrame.request_id, btwRequestId);
assert.equal(openFrame.ts, 123);

assert.equal(matchesBtwRequest("btw-request-1", "btw-request-1"), true);
assert.equal(matchesBtwRequest("btw-request-new", "btw-request-old"), false);
assert.equal(matchesBtwRequest(null, "btw-request-old"), false);
const boundedCache = boundCachedTurns(Array.from(
  { length: 120 }, (_, id) => ({ id, prompt: `turn-${id}` })));
assert.equal(boundedCache.length, 100);
assert.equal((boundedCache[0] as { id: number }).id, 20);
const knownBadCompactionCache = [{
  id: "item-51",
  prompt: "continue the task",
  forkPointId: "native-turn",
  done: false,
  blocks: [{
    kind: "process",
    item_id: "item-54",
    processKind: "compaction",
    phase: "snapshot",
    status: "succeeded",
    turn_id: "native-turn",
    title: "压缩上下文",
    done: true,
  }],
}, {
  id: "msg-after-compact",
  prompt: "",
  done: false,
  blocks: [{
    kind: "text",
    message_id: "msg-after-compact",
    channel: "commentary",
    text: "continued output",
    done: true,
  }, {
    kind: "process",
    item_id: "replayed-compaction",
    processKind: "compaction",
    phase: "snapshot",
    status: "succeeded",
    turn_id: "native-turn",
    title: "压缩上下文",
    done: true,
  }],
}];
assert.equal(
  hasOrphanedActiveCompactionProjection(knownBadCompactionCache),
  true,
  "the exact promptless compact replay projection is rebuildable corruption",
);
assert.equal(hasOrphanedActiveCompactionProjection([
  knownBadCompactionCache[0],
  {
    id: "optimistic-steer",
    clientMsgId: "optimistic-steer",
    prompt: "a legitimate steer awaiting acceptance",
    done: false,
    blocks: [],
  },
]), false, "an optimistic steer may legitimately coexist with an active row");
assert.equal(hasOrphanedActiveCompactionProjection([
  knownBadCompactionCache[0],
  knownBadCompactionCache[1],
  { ...knownBadCompactionCache[1], id: "second-orphan", blocks: [{
    ...(knownBadCompactionCache[1].blocks[0] as Record<string, unknown>),
    message_id: "second-orphan",
  }, knownBadCompactionCache[1].blocks[1]] },
]), false, "ambiguous promptless rows must not be guessed away");
assert.equal(hasOrphanedActiveCompactionProjection([{
  ...knownBadCompactionCache[0],
  blocks: [],
}, knownBadCompactionCache[1]]), false,
"a generic shared native task id is not compaction identity proof");
const skipsOneOversizedCacheTurn = boundCachedTurns([
  { id: "small", prompt: "keep" },
  { id: "huge", image: "x".repeat(2 * 1024 * 1024 + 1) },
]);
assert.deepEqual(skipsOneOversizedCacheTurn, [{ id: "small", prompt: "keep" }]);
const oversizedProcessCache = boundCachedTurns([{
  id: "oversized-process",
  prompt: "1",
  done: true,
  images: [{
    media_type: "image/png",
    data: "i".repeat(2 * 1024 * 1024 + 1),
  }],
  blocks: [
    {
      kind: "text", message_id: "oversized-commentary", text: "2",
      done: true, channel: "commentary",
    },
    {
      kind: "tool", message_id: "oversized-tool-message-3",
      tool_use_id: "oversized-tool-3", tool: "Read",
      input: { file_path: "/tmp/3" },
      result: {
        content: "x".repeat(2 * 1024 * 1024 + 1),
        is_error: false,
      },
      done: true,
    },
    {
      kind: "tool", message_id: "oversized-tool-message-4",
      tool_use_id: "oversized-tool-4", tool: "Bash",
      input: { command: "echo 4" }, done: true,
    },
    {
      kind: "process", item_id: "oversized-process-5",
      processKind: "command", phase: "snapshot", status: "succeeded",
      title: "5", done: true,
    },
    {
      kind: "text", message_id: "oversized-final", text: "6",
      done: true, channel: "final",
    },
  ],
  detailProjection: {
    segments: [{
      pageKey: "huge-segment",
      before: null,
      events: [{
        type: "delta", sid: "oversized-process",
        message_id: "huge-event", text: "y".repeat(2 * 1024 * 1024 + 1),
        channel: "commentary", v: 22, ts: 1,
      }],
      hasMore: false,
      oldestCursor: null,
      hasNewer: false,
      newerCursor: null,
      encodedChars: 2 * 1024 * 1024 + 1,
    }],
    blocks: [],
    capped: false,
    hasMore: false,
    oldestCursor: null,
    hasNewer: false,
    newerCursor: null,
  },
  detailEventCount: 4,
  detailLoaded: true,
}] as Turn[]) as Turn[];
assert.equal(oversizedProcessCache.length, 1,
  "one heavy attachment/output must not evict the whole visible turn");
assert.equal(oversizedProcessCache[0].images, undefined,
  "base64 attachments stay in the lazy history-image path");
assert.equal(oversizedProcessCache[0].detailProjection, undefined,
  "raw cursor pages are never copied into the instant-paint cache");
assert.deepEqual(oversizedProcessCache[0].blocks.map((block) => {
  if (block.kind === "text") return block.text;
  if (block.kind === "tool") return block.tool_use_id;
  return block.title;
}), ["2", "oversized-tool-3", "oversized-tool-4", "5", "6"],
"the compact cache keeps every visible 1..6 timeline identity across refresh");
assert.ok(new TextEncoder().encode(
  JSON.stringify(oversizedProcessCache[0])).byteLength < 2 * 1024 * 1024,
"the instant projection must remain bounded independently of heavy detail");
const smallImageCache = boundCachedTurns([{
  id: "small-image-turn",
  prompt: "preview",
  done: true,
  images: [{ media_type: "image/png", data: "small-preview" }],
  blocks: [],
}] as Turn[]) as Turn[];
assert.equal(smallImageCache[0].images?.[0]?.data, "small-preview",
  "small optimistic images remain available during the first paint");
const extremeTimelineCache = boundCachedTurns([{
  id: "extreme-timeline-turn",
  prompt: "keep every visible row",
  done: true,
  blocks: Array.from({ length: 256 }, (_, index) => ({
    kind: "tool" as const,
    message_id: `message-${"m".repeat(12 * 1024)}-${index}`,
    tool_use_id: `tool-${"t".repeat(12 * 1024)}-${index}`,
    tool: "Bash",
    input: { command: `echo ${index}` },
    done: true,
  })),
}] as Turn[]) as Turn[];
assert.equal(extremeTimelineCache.length, 1);
assert.equal(extremeTimelineCache[0].blocks.length, 256,
  "the hard cache fallback preserves the full bounded timeline skeleton");
assert.equal(new Set(extremeTimelineCache[0].blocks.map((block) =>
  block.kind === "tool" ? block.tool_use_id : "")).size, 256,
"hard clipping must retain the unique suffix of every process identity");
assert.ok(new TextEncoder().encode(
  JSON.stringify(extremeTimelineCache[0])).byteLength < 2 * 1024 * 1024,
"even pathological process identifiers cannot evict the current timeline");
const stripsFileBodiesFromCache = boundCachedTurns([{
  id: "file-turn",
  prompt: "upload",
  files: [{ filename: "secret.txt", data: "do-not-persist", extra: "drop-me" }],
}]);
assert.deepEqual(stripsFileBodiesFromCache, [{
  id: "file-turn",
  prompt: "upload",
  files: [{ filename: "secret.txt", data: "" }],
}]);

const boundedRuntimeTurns = boundRuntimeTurns(Array.from(
  { length: 5 }, (_, id) => ({ id: `turn-${id}`, prompt: "x", blocks: [], done: true })),
3, 10_000);
assert.deepEqual(boundedRuntimeTurns.map((turn) => turn.id), ["turn-2", "turn-3", "turn-4"]);
const retainedHistoryPrepend = boundRuntimeTurns([
  ...Array.from({ length: 12 }, (_, id) => ({
    id: `older-${id}`, prompt: "older", blocks: [], done: true,
  })),
  ...Array.from({ length: 200 }, (_, id) => ({
    id: `current-${id}`, prompt: "current", blocks: [], done: true,
  })),
]);
assert.equal(retainedHistoryPrepend[0]?.id, "older-0");
assert.equal(retainedHistoryPrepend.length, 212,
  "the former 200-turn DOM limit must not discard an accepted history page");
const cappedHistoryPrepend = boundRuntimeTurns([
  ...Array.from({ length: 12 }, (_, id) => ({
    id: `older-${id}`, prompt: "older", blocks: [], done: true,
  })),
  ...Array.from({ length: MAX_RUNTIME_TURNS }, (_, id) => ({
    id: `current-${id}`, prompt: "current", blocks: [], done: true,
  })),
]);
assert.equal(cappedHistoryPrepend[0]?.id, "current-0");
assert.equal(cappedHistoryPrepend.some((turn) => turn.id.startsWith("older-")), false,
  "the canonical data window remains bounded independently from the virtual DOM");
const activeTurn = { id: "active", prompt: "running", blocks: [], done: false };
const keepsActiveRuntimeTurns = boundRuntimeTurns([
  { id: "old", prompt: "old", blocks: [], done: true },
  activeTurn,
  { id: "newest-done", prompt: "new", blocks: [], done: true },
], 2, 10_000);
assert.deepEqual(keepsActiveRuntimeTurns.map((turn) => turn.id), ["active", "newest-done"]);
const keepsNewestOversizedTurn = boundRuntimeTurns([
  { id: "older", prompt: "a".repeat(100), blocks: [], done: true },
  { id: "newer", prompt: "b".repeat(100), blocks: [], done: true },
], 10, 10);
assert.deepEqual(keepsNewestOversizedTurn.map((turn) => turn.id), ["newer"]);
const completedAnswerWithBackgroundWork = {
  id: "background-active", prompt: "done", done: true,
  blocks: [{ kind: "process" as const, item_id: "agent-1", title: "代理",
    done: false }],
};
const keepsCompletedAnswerWithBackgroundWork = boundRuntimeTurns([
  completedAnswerWithBackgroundWork,
  { id: "newer-complete", prompt: "new", blocks: [], done: true },
], 1, 10_000);
assert.deepEqual(keepsCompletedAnswerWithBackgroundWork.map((turn) => turn.id), [
  "background-active",
]);

const idleRuntime = () => ({
  state: "idle", syncReady: true, replaying: false,
  turns: [] as Array<{ done: boolean }>, queue: [] as unknown[],
  pendingSend: null, failedDeferred: [] as unknown[], pendingQuestion: null,
});
const prunedRuntimes = pruneRuntimeMap({
  protected: idleRuntime(), oldestIdle: idleRuntime(), newestIdle: idleRuntime(),
}, new Set(["protected"]), 2);
assert.deepEqual(Object.keys(prunedRuntimes), ["protected", "newestIdle"]);
const activeRuntime = { ...idleRuntime(), state: "running" };
const keepsConfirmedActive = pruneRuntimeMap({
  protected: idleRuntime(), active: activeRuntime,
}, new Set(["protected"]), 1);
assert.deepEqual(Object.keys(keepsConfirmedActive), ["protected", "active"]);
const backgroundRuntime = {
  ...idleRuntime(), turns: [completedAnswerWithBackgroundWork],
};
const keepsBackgroundRuntime = pruneRuntimeMap({
  protected: idleRuntime(), background: backgroundRuntime,
}, new Set(["protected"]), 1);
assert.deepEqual(Object.keys(keepsBackgroundRuntime), ["protected", "background"]);
const staleActive = { ...activeRuntime, syncReady: false };
const dropsUnconfirmedOldGeneration = pruneRuntimeMap({
  protected: idleRuntime(), stale: staleActive,
}, new Set(["protected"]), 1);
assert.deepEqual(Object.keys(dropsUnconfirmedOldGeneration), ["protected"]);

// Exercise the actual WebSocket reducer boundary: live delivery can race ahead
// of Hello replay, and a cached command response can arrive after newer state.
class FakeWebSocket {
  static readonly OPEN = 1;
  static readonly instances: FakeWebSocket[] = [];

  readonly sent: string[] = [];
  readyState = FakeWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(raw: string): void {
    this.sent.push(raw);
  }

  close(): void {
    this.readyState = 3;
  }

  receive(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify({ v: 20, ts: 1, ...frame }) });
  }
}

const sessionValues = new Map<string, string>();
const browserSessionStorage = {
  getItem: (key: string) => sessionValues.get(key) ?? null,
  setItem: (key: string, value: string) => { sessionValues.set(key, value); },
  removeItem: (key: string) => { sessionValues.delete(key); },
};
let protocolReloads = 0;
const nativeFetch = globalThis.fetch;
Object.assign(globalThis, {
  window: {
    location: {
      protocol: "http:", host: "relay.test",
      reload: () => { protocolReloads += 1; },
    },
  },
  WebSocket: FakeWebSocket,
  sessionStorage: browserSessionStorage,
});

sessionValues.set("cc-remote:tab-client-id", "copied-tab-id-must-be-ignored");
const persistentClientId = stableTabClientId();
assert.match(persistentClientId,
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
assert.notEqual(persistentClientId, "copied-tab-id-must-be-ignored",
  "a duplicated tab must receive its own connection identity");
assert.equal(stableTabClientId(), persistentClientId,
  "reconnects within one page lifetime must retain their routing identity");

assert.deepEqual(
  protocolRecoveryAction(null, PROTOCOL_VERSION), { kind: "wait" });
assert.deepEqual(
  protocolRecoveryAction(PROTOCOL_VERSION - 1, PROTOCOL_VERSION),
  { kind: "wait" },
);
assert.deepEqual(
  protocolRecoveryAction(PROTOCOL_VERSION, PROTOCOL_VERSION),
  { kind: "reconnect" },
);
assert.deepEqual(
  protocolRecoveryAction(PROTOCOL_VERSION + 1, PROTOCOL_VERSION + 1),
  { kind: "reload", protocol: PROTOCOL_VERSION + 1 },
);

let relayProbeProtocol = PROTOCOL_VERSION;
let webProbeProtocol = PROTOCOL_VERSION;
Object.assign(globalThis, {
  fetch: async (input: string | URL | Request) => ({
    ok: true,
    json: async () => ({
      protocol: String(input).startsWith("/healthz")
        ? relayProbeProtocol : webProbeProtocol,
    }),
  }) as Response,
});

const alignedMismatchStates: Array<{ state: string; detail?: string }> = [];
const alignedMismatch = new RelayWs({
  onEvent: () => {},
  onConnState: (state, detail) => {
    alignedMismatchStates.push({ state, detail });
  },
});
alignedMismatch.start();
const alignedSocketCount = FakeWebSocket.instances.length;
FakeWebSocket.instances.at(-1)?.onclose?.({ code: 4406 });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(protocolReloads, 0,
  "a current bundle must reconnect without an unnecessary page reload");
assert.equal(FakeWebSocket.instances.length, alignedSocketCount + 1,
  "the client should reconnect after relay and bundle protocols align");
assert.deepEqual(alignedMismatchStates.at(-1), {
  state: "connecting", detail: undefined,
});
alignedMismatch.stop();

relayProbeProtocol = PROTOCOL_VERSION - 1;
webProbeProtocol = PROTOCOL_VERSION;
const rollingMismatchStates: Array<{ state: string; detail?: string }> = [];
const rollingMismatchEvents: ServerEvent[] = [];
const rollingMismatch = new RelayWs({
  onEvent: (event) => { rollingMismatchEvents.push(event); },
  onConnState: (state, detail) => {
    rollingMismatchStates.push({ state, detail });
  },
});
rollingMismatch.start();
const rollingSocketCount = FakeWebSocket.instances.length;
const rollingSocket = FakeWebSocket.instances.at(-1);
rollingSocket?.receive({
  type: "error", code: "protocol",
  message: "first frame must be a client hello",
});
assert.equal(rollingMismatchEvents.length, 0,
  "a relay handshake mismatch must not leave a stale refresh banner");
rollingSocket?.onclose?.({ code: 4406 });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(protocolReloads, 0,
  "split relay and web tiers must not trigger a premature reload");
assert.equal(FakeWebSocket.instances.length, rollingSocketCount,
  "the client must wait instead of reconnecting to a known-old relay");
assert.deepEqual(rollingMismatchStates.at(-1), {
  state: "reconnecting",
  detail: "页面版本正在更新，等待服务端完成部署…",
});
rollingMismatch.stop();

relayProbeProtocol = PROTOCOL_VERSION + 1;
webProbeProtocol = PROTOCOL_VERSION + 1;
const staleBundle = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
staleBundle.start();
FakeWebSocket.instances.at(-1)?.onclose?.({ code: 4406 });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(protocolReloads, 1,
  "an aligned newer deployment should refresh a stale bundle once");
staleBundle.stop();

const repeatedMismatch = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
repeatedMismatch.start();
FakeWebSocket.instances.at(-1)?.onclose?.({ code: 4406 });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(protocolReloads, 1,
  "a rolling deploy must not trap the browser in a reload loop");
repeatedMismatch.stop();

Object.assign(globalThis, {
  sessionStorage: {
    getItem: () => { throw new DOMException("blocked", "SecurityError"); },
    setItem: () => { throw new DOMException("blocked", "SecurityError"); },
    removeItem: () => { throw new DOMException("blocked", "SecurityError"); },
  },
});
const storageBlockedEvents: ServerEvent[] = [];
const storageBlockedStates: Array<{ state: string; detail?: string }> = [];
const storageBlocked = new RelayWs({
  onEvent: (event) => { storageBlockedEvents.push(event); },
  onConnState: (state, detail) => {
    storageBlockedStates.push({ state, detail });
  },
});
storageBlocked.start();
const storageBlockedSocket = FakeWebSocket.instances.at(-1);
storageBlockedSocket?.receive({
  type: "state", sid: "storage-blocked", state: "idle",
});
assert.equal(storageBlockedEvents.length, 1,
  "blocked sessionStorage must not make a valid frame look malformed");
storageBlockedSocket?.receive({
  type: "error", code: "protocol", message: "correlated runtime error",
});
assert.equal(storageBlockedEvents.length, 2,
  "protocol errors after a compatible frame must remain visible");
storageBlockedSocket?.onclose?.({ code: 4406 });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(protocolReloads, 1,
  "a mismatch cannot auto-reload safely when the reload guard is unavailable");
assert.deepEqual(storageBlockedStates.at(-1), {
  state: "reconnecting",
  detail: "页面版本正在更新，等待服务端完成部署…",
});
storageBlocked.stop();
Object.assign(globalThis, {
  fetch: nativeFetch,
  sessionStorage: browserSessionStorage,
});

function assertAutomaticPreviewReads() {
  const previewRelay = new RelayWs({ onEvent: () => {}, onConnState: () => {} });
  previewRelay.start();
  const previewSocket = FakeWebSocket.instances.at(-1)!;
  previewSocket.onopen?.();
  try {
    const read = previewRelay.sendAuthorizePreview("file-identity-1", "read-intent-1", "allow", "preview-session");
    assert.ok(read);
    const count = previewSocket.sent.length;
    assert.equal(previewRelay.sendAuthorizePreview("file-identity-1", "read-intent-1", "allow", "preview-session"), read);
    assert.equal(previewSocket.sent.length, count, "duplicate preview mounts share the exact request");
    assert.equal(previewRelay.sendAuthorizePreview("file-identity-2", "read-intent-1", "allow", "preview-session"), null);
    assert.equal(previewSocket.sent.length, count, "replacement files must not trigger an automatic authorization loop");
    assert.ok(previewRelay.sendAuthorizePreview("file-identity-2", "read-intent-2", "allow", "preview-session"), "explicit refresh starts a fresh read");
    assert.ok(previewRelay.sendAuthorizePreview("other-file", "read-intent-1", "allow", "other-preview-session"), "read guards are session-scoped");
  } finally { previewRelay.stop(); }
}
assertAutomaticPreviewReads();

const observed: ServerEvent[] = [];
let wrapperGenerationChanges = 0;
const relay = new RelayWs({
  onEvent: (event) => { observed.push(event); },
  onConnState: () => {},
  onWrapperGenerationChanged: () => { wrapperGenerationChanges += 1; },
});
relay.start();
const socket = FakeWebSocket.instances.at(-1);
assert.ok(socket);
socket.onopen?.();

// Code/Work is a hard focus boundary. Background snapshots and delayed focus
// confirmations from the previous surface must not retarget later commands.
relay.setSessionEngines([
  { session_id: "surface-code", engine: "claude", space: "code" },
  { session_id: "surface-work", engine: "claude", space: "work" },
]);
relay.setFocusedSid("surface-code", "claude", "code");
relay.setSurface("claude", "work");
socket.receive({
  type: "snapshot", sid: "surface-code", cc_session_id: "surface-code",
  state: "idle", tail_text: "",
});
relay.sendGetContext();
assert.equal("sid" in JSON.parse(socket.sent.at(-1) ?? "{}"), false);
const focusEventsBefore = observed.filter((event) => event.type === "session_focus").length;
socket.receive({ type: "session_focus", session_id: "surface-code" });
assert.equal(observed.filter((event) => event.type === "session_focus").length,
  focusEventsBefore);
relay.sendGetContext();
assert.equal("sid" in JSON.parse(socket.sent.at(-1) ?? "{}"), false);
socket.receive({ type: "session_focus", session_id: "surface-work" });
relay.sendGetContext();
assert.equal(JSON.parse(socket.sent.at(-1) ?? "{}").sid, "surface-work");
  relay.setSurface("codex", "code");

relay.sendGetWorkArtifacts("claude", "surface-work");
const artifactsFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(artifactsFrame.type, "get_work_artifacts");
assert.equal(artifactsFrame.engine, "claude");
assert.equal(artifactsFrame.session_id, "surface-work");
assert.equal(typeof artifactsFrame.client_id, "string");

  relay.setFocusedSid("main-after-navigation", "claude", "code");
  assert.equal(relay.sendGetHistory(
    "history-with-cwd", null, 4, "/project/from-list"), true);
const historyWithCwdFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(historyWithCwdFrame.type, "get_history");
assert.equal(historyWithCwdFrame.session_id, "history-with-cwd");
assert.equal(historyWithCwdFrame.cwd, "/project/from-list");
assert.equal(relay.sendGetTurnDetail(
  "history-with-cwd", "detail-turn", "detail-revision",
  "detail-before-cursor", 48,
), true);
const pagedDetailFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.deepEqual({
  type: pagedDetailFrame.type,
  session_id: pagedDetailFrame.session_id,
  turn_id: pagedDetailFrame.turn_id,
  revision: pagedDetailFrame.revision,
  before: pagedDetailFrame.before,
  limit: pagedDetailFrame.limit,
}, {
  type: "get_turn_detail",
  session_id: "history-with-cwd",
  turn_id: "detail-turn",
  revision: "detail-revision",
  before: "detail-before-cursor",
  limit: 48,
}, "intra-turn detail paging carries its exact cursor and bounded page size");
relay.sendGetFilePreview(
  "notes.md", "btw-preview-request", "btw-pinned",
);
const btwPreviewFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwPreviewFrame.type, "get_file_preview");
assert.equal(btwPreviewFrame.sid, "btw-pinned",
  "a pinned BTW file preview must not follow the newly focused main session");
relay.sendSaveMarkdown(
  "notes.md", "updated", 7, "8", "revision",
  "btw-save-request", "btw-pinned",
);
const btwSaveFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwSaveFrame.type, "save_markdown");
assert.equal(btwSaveFrame.sid, "btw-pinned");
relay.sendGetPreviewAsset(
  "image.png", "btw-preview-request",
  "btw-asset-request", "btw-pinned",
);
const btwAssetFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwAssetFrame.type, "get_preview_asset");
assert.equal(btwAssetFrame.sid, "btw-pinned");
relay.sendInterruptTo("btw-pinned");
const btwInterruptFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwInterruptFrame.type, "interrupt");
assert.equal(btwInterruptFrame.sid, "btw-pinned");
assert.equal(typeof btwInterruptFrame.client_id, "string");
relay.sendSetModelTo("btw-pinned", "gpt-5.6-terra");
const btwModelFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwModelFrame.type, "set_model");
assert.equal(btwModelFrame.sid, "btw-pinned");
assert.equal(btwModelFrame.model, "gpt-5.6-terra");
relay.sendSetEffortTo("btw-pinned", "high");
const btwEffortFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(btwEffortFrame.type, "set_effort");
assert.equal(btwEffortFrame.sid, "btw-pinned");
assert.equal(btwEffortFrame.effort, "high");

const codexModels = modelsFor("codex");
const opus5 = MODELS.find((model) => model.id === "claude-opus-5[1m]");
assert.ok(opus5, "Claude's curated model sheet must expose Opus 5 with 1M context");
assert.equal(MODELS[0].id, "claude-opus-5[1m]",
  "Opus 5 with 1M context must be the default curated Claude card");
assert.equal(opus5.name, "Opus 5");
assert.match(opus5.ds, /1M 上下文/);
assert.equal(matchModelId("claude-opus-5[1m]", "claude"),
  "claude-opus-5[1m]",
  "an exact context-qualified Claude model id must not be reduced to its base alias");
assert.equal(matchModelId("claude-opus-5", "claude"),
  "claude-opus-5[1m]",
  "the legacy unqualified Opus 5 alias must resolve to the curated 1M model");
assert.equal(matchModelId("claude-mythos-5[1m]", "claude"),
  "claude-mythos-5[1m]",
  "an old model id must retain its exact identity after a catalog upgrade");
assert.equal(matchModelId("claude-opus-5-custom", "claude"),
  "claude-opus-5-custom",
  "a provider-specific model id must not be swallowed by the curated Opus alias");
assert.equal(matchModelId("claude-opus-5[provider]", "claude"),
  "claude-opus-5[provider]",
  "only the literal 1M suffix may participate in curated model compatibility");
assert.equal(clientSlashesFor("codex").has("plan"), true);
assert.equal(clientSlashesFor("codex").has("normal"), true);
assert.equal(commandsFor("codex").some((command) => (
  "slash" in command && command.slash === "plan"
)), true);
assert.deepEqual(matchCommands("pla", "codex").map((command) => command.slash), ["plan"]);
for (const id of ["gpt-5.6-terra", "gpt-5.6-luna"]) {
  const selected = codexModels.find((model) => model.id === id);
  assert.ok(selected);
  relay.setFocusedSid("codex-model-session", "codex");
  relay.sendSetModel(selected.id);
  const frame = JSON.parse(socket.sent.at(-1) ?? "{}");
  assert.equal(frame.type, "set_model");
  assert.equal(frame.sid, "codex-model-session");
  assert.equal(frame.model, id);
}

relay.setFocusedSid("codex-plan-session", "codex");
relay.sendSetCollaborationMode("plan");
const collaborationFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(collaborationFrame.type, "set_collaboration_mode");
assert.equal(collaborationFrame.sid, "codex-plan-session");
assert.equal(collaborationFrame.mode, "plan");
assert.equal(typeof collaborationFrame.cmd_id, "string");
assert.equal(typeof collaborationFrame.client_id, "string");

relay.sendSetPermissionProfile(":danger-full-access");
const permissionProfileFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(permissionProfileFrame.type, "set_permission_profile");
assert.equal(permissionProfileFrame.sid, "codex-plan-session");
assert.equal(permissionProfileFrame.profile, ":danger-full-access");

relay.sendSetWebSearch("live");
const webSearchFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(webSearchFrame.type, "set_web_search");
assert.equal(webSearchFrame.sid, "codex-plan-session");
assert.equal(webSearchFrame.mode, "live");

relay.sendGetPermissionProfiles();
const permissionProfilesFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(permissionProfilesFrame.type, "get_permission_profiles");
assert.equal(permissionProfilesFrame.sid, "codex-plan-session");

relay.sendGetPermissionProfiles("/tmp/prospective-project");
const newChatProfilesFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(newChatProfilesFrame.type, "get_permission_profiles");
assert.equal(newChatProfilesFrame.cwd, "/tmp/prospective-project");
assert.equal("sid" in newChatProfilesFrame, false,
  "prospective cwd discovery must not inherit the previously focused session");

relay.sendGetPermissionProfiles("/tmp/prospective-project", "stack");
const stackProfilesFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(stackProfilesFrame.codex_profile_id, "stack");

relay.sendNewSession(
  "/tmp/project", "codex", "gpt-5.6-sol", "xhigh",
  { prompt: "先制定计划", msg_id: "first-plan-message" }, "plan",
  "on-request", ":danger-full-access", "live", "fast",
  "code", null, "stack",
);
const newPlanFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(newPlanFrame.type, "new_session");
assert.equal(newPlanFrame.engine, "codex");
assert.equal(newPlanFrame.collaboration_mode, "plan");
assert.equal(newPlanFrame.permission_mode, "on-request");
assert.equal(newPlanFrame.permission_profile, ":danger-full-access");
assert.equal(newPlanFrame.web_search, "live");
assert.equal(newPlanFrame.service_tier, "fast");
assert.equal(newPlanFrame.prompt, "先制定计划");
assert.equal(newPlanFrame.codex_profile_id, "stack");

relay.sendNewSession(
  null, "codex", null, null,
  { prompt: "Work uses primary", msg_id: "work-primary-message" },
  "default", "never", undefined, undefined, "fast",
  "work", "project-1", "stack",
);
const workSessionFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(workSessionFrame.space, "work");
assert.equal(workSessionFrame.codex_profile_id, "stack",
  "Codex Work freezes the selected account with the create request");

relay.sendCreateWorkSchedule(
  "codex", "Stack report", "Build it", Date.now() / 1000 + 60,
  86400, "project-1", "stack",
);
const workScheduleFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(workScheduleFrame.type, "create_work_schedule");
assert.equal(workScheduleFrame.codex_profile_id, "stack");

relay.sendNewSession(
  "/tmp/project", "claude", null, null,
  { prompt: "使用本机默认设置", msg_id: "default-settings-message" },
);
const defaultSessionFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(defaultSessionFrame.type, "new_session");
assert.equal("model" in defaultSessionFrame, false);
assert.equal("effort" in defaultSessionFrame, false);

relay.sendGetModels("claude", "/tmp/project");
const claudeDefaultsFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(claudeDefaultsFrame.type, "get_models");
assert.equal(claudeDefaultsFrame.engine, "claude");
assert.equal(claudeDefaultsFrame.cwd, "/tmp/project");
assert.equal(typeof claudeDefaultsFrame.cmd_id, "string");
assert.equal(typeof claudeDefaultsFrame.client_id, "string");

relay.sendGetModels("codex", undefined, "stack");
const stackModelsFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(stackModelsFrame.codex_profile_id, "stack");

const skillOnlyCapabilitiesRequestId = relay.sendGetEngineCapabilities(
  "codex", "code", "/tmp/project", true, "stack",
);
const skillOnlyCapabilitiesFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(skillOnlyCapabilitiesFrame.type, "get_engine_capabilities");
assert.equal(skillOnlyCapabilitiesFrame.skills_only, true);
assert.equal(skillOnlyCapabilitiesFrame.cwd, "/tmp/project");
assert.equal(skillOnlyCapabilitiesFrame.codex_profile_id, "stack");
assert.equal(typeof skillOnlyCapabilitiesFrame.client_id, "string");
assert.equal(
  skillOnlyCapabilitiesFrame.cmd_id, skillOnlyCapabilitiesRequestId,
  "capability responses need the reliable request id for exact correlation",
);

const appSource = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf8");
function testDshCapabilitiesRouting() {
  assert.ok(socket);
  relay.sendGetEngineCapabilities("dsh", "code", "/tmp/project", true, null, null, "dsh@selected");
  const dshSkillsFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
  assert.equal(dshSkillsFrame.sid, "dsh@selected");
  assert.equal(dshSkillsFrame.engine, "dsh");
}
testDshCapabilitiesRouting();
assert.doesNotMatch(appSource, /<Suspense fallback=\{null\}>[\s\S]{0,120}<GoalPanel/,
  "Goal lazy loading must keep a stable chip placeholder");
assert.match(appSource,
  /fallback=\{\(\(!archivedBrowse[\s\S]{0,180}goalUi\?\.open\)\) \|\| planProgress\)[\s\S]{0,180}goal-suspense/,
  "a live Goal or current Plan stays visible while its component chunk loads");
assert.match(appSource, /externalPlanProgress=\{planProgress/,
  "the session strip must explicitly take ownership from the message row");
assert.match(appSource,
  /const previewAgentFile = [\s\S]{0,180}setAgentPanel\(null\)/,
  "opening a file from Agent detail must switch the shared right slot");
assert.match(appSource, /onOpenFile=\{previewAgentFile\}/,
  "Agent file links must use the right-slot switching callback");
assert.match(appSource, /recoverableReads\.retry\(\["goal", key\]/,
  "a transient Goal read failure must be retried in the same connection");
assert.doesNotMatch(appSource,
  /state\.wrapperOnline\s*\n?\s*\|\|\s*!goalUiPreferencesRef\.current/,
  "Goal recovery must query the focused session on a new browser/device");
assert.match(appSource,
  /Goal chip owns its retry state; feeding the same[\s\S]{0,240}\n\s*return;/,
  "a handled Goal read failure must not also become a global command banner");
const btwPanelSource = readFileSync(
  resolve(process.cwd(), "src/components/BtwPanel.tsx"), "utf8");
for (const optimisticAction of ["set_model", "set_effort", "set_perm", "set_collaboration_mode"]) {
  assert.doesNotMatch(appSource, new RegExp(`dispatch\\(\\{ type: ["']${optimisticAction}["']`));
}
assert.match(appSource,
  /const \{[\s\S]{0,160}cwd,[\s\S]{0,160}autoCompactMode,[\s\S]{0,120}\} = state\.newChat/,
  "new-session autocompact must be captured with cwd/model before the atomic create");
assert.match(appSource, /data-lock-horizontal-swipe/);
assert.match(appSource, /surface=\{space\}/);
assert.match(appSource, /draftKey=\{focusedComposerDraftKey\}/);
assert.match(appSource, /composerDraftsRef\.current\.rekey/,
  "temp session id capture must retain the focused composer draft");
assert.match(appSource, /\{space === "work" \? "Work" : "Code"\}/);
assert.match(appSource, /<select className="engine-toggle" value=\{engine\}[\s\S]{0,500}onChange=\{event => \{\s*toggleEngine/);
assert.match(appSource, /setNewChatAutoFocus\(false\)/,
  "switching engines must not summon the new-chat keyboard");
assert.match(appSource, /prepareSurfaceSwitch\(nextEngine, nextSpace\)/,
  "engine switches must restore the target engine's remembered surface session");
assert.match(appSource, /spacesByEngineRef\.current\[nextEngine\]/,
  "engine switches must remember Code/Work independently");
assert.match(appSource, /prepareSurfaceSwitch\(engine, next\)/,
  "Work/Code switches must share the remembered-session restoration path");
assert.match(appSource,
  /authoritativeSurfaceListsRef\.current\.has\(surfaceKey\)/,
  "notification navigation must wait for an authoritative surface list");
assert.match(appSource,
  /notificationOriginRef\.current === null[\s\S]*?authoritativeSurfaceListsRef\.current\.delete\(surfaceKey\)/,
  "each notification click must invalidate a pre-click target catalog");
const sessionListOwnershipGuard = appSource.indexOf(
  'if (msg.type === "session_list" && !ownership) return;');
const guardedSessionEngineWrite = appSource.indexOf(
  "ws.setSessionEngines", sessionListOwnershipGuard);
assert.ok(sessionListOwnershipGuard >= 0
  && guardedSessionEngineWrite > sessionListOwnershipGuard,
"an unowned or stale SessionList must be rejected before it can rewrite profile ownership");
assert.match(appSource, /resolveNotificationNavigation\(\{/,
  "notification navigation must pass through the fail-closed resolver");
assert.match(appSource, /focusListedSession\(navigation\.session\)/,
  "notification and sidebar navigation must share the exact focus path");
assert.match(appSource,
  /msg\.type === "turn_end" && msg\.sid && msg\.notification_context/,
  "buffered or historical TurnEnd frames must never create a local notification");
assert.match(appSource, /cancelPendingNotificationTarget\(\)/,
  "manual navigation must be able to cancel a pending notification target");
assert.match(appSource,
  /authoritativeSurfaceListsRef\.current\.clear\(\);\s*notificationListRequestRef\.current = null;/,
  "a machine change must clear the prior socket's list request marker");
assert.match(appSource,
  /const ws = wsRef\.current;\s*if \(!ws\) return;[\s\S]*?if \(ws\.sendListSessions/,
  "a target list is marked requested only after the current socket accepts it");
assert.match(appSource,
  /if \(s === "connected"\) \{[\s\S]*?notificationListRequestRef\.current = null;[\s\S]*?bumpNotificationListRevision\(\)/,
  "each underlying socket connection must retry an unresolved target list");
assert.doesNotMatch(appSource,
  /sendCloseBtw\(s\); dispatch\(\{ type: "clear_btw" \}\); \}\s*[\s\S]{0,120}\}, \[focusedSid, engine\]\)/,
  "session and harness navigation must retain a session-scoped BTW");
function testBtwPanelScopes() {
  const btwScope = btwPanelScopeKey("machine-a", "code", "codex", "parent-a");
  const otherBtwScopes = [
    btwPanelScopeKey("machine-b", "code", "codex", "parent-a"),
    btwPanelScopeKey("machine-a", "work", "codex", "parent-a"),
    btwPanelScopeKey("machine-a", "code", "claude", "parent-a"),
    btwPanelScopeKey("machine-a", "code", "codex", "parent-b"),
  ];
  const openedBtwScopes = setBtwPanelScope([], btwScope, true);
  assert.ok(otherBtwScopes.every((key) => !openedBtwScopes.includes(key)),
    "visibility must not leak between machines, surfaces, engines or parents");
  assert.deepEqual(setBtwPanelScope(openedBtwScopes, btwScope, false), []);
  assert.deepEqual(readBtwPanelScopes({ getItem: (key) => (
    key === BTW_PANEL_SCOPES_KEY ? JSON.stringify(openedBtwScopes) : null
  ) }), openedBtwScopes, "refresh restores only the explicitly opened parent");
  assert.deepEqual(readBtwPanelScopes({ getItem: (key) => (
    key === "cc-remote:btw-panel-visible" ? "1" : null
  ) }), [], "a legacy global open flag has no trustworthy parent and cannot migrate");
  assert.deepEqual(readBtwPanelScopes({ getItem: () => "{broken" }), []);
  assert.deepEqual(readBtwPanelScopes({ getItem: () => { throw new Error("private"); } }), []);
  assert.deepEqual(readBtwPanelScopes({ getItem: () => JSON.stringify([
    btwScope, "true", "null", "{}", "[]", '["a","invalid","codex","b"]', btwScope,
  ]) }), openedBtwScopes, "untrusted storage is shape-checked and deduplicated");
  let boundedBtwScopes: string[] = [];
  for (let i = 0; i < 100; i++) boundedBtwScopes = setBtwPanelScope(
    boundedBtwScopes, btwPanelScopeKey("m", "code", "codex", String(i)), true);
  assert.equal(boundedBtwScopes.length, 64);
  assert.deepEqual(rekeyBtwPanelScope(openedBtwScopes, btwScope, otherBtwScopes[3]),
    [otherBtwScopes[3]], "native id capture moves only the original parent's preference");
  assert.equal(rekeyBtwPanelScope(openedBtwScopes, "missing", btwScope), openedBtwScopes);
}
testBtwPanelScopes();
assert.match(appSource,
  /const activeBtwGroup = visibleParentSid[\s\S]{0,100}state\.btwByParentSid\[visibleParentSid\][\s\S]{0,160}activeBtwGroup\.activeSid/,
  "only the focused parent session may expose its selected side chat");
assert.match(appSource,
  /const openBtw = \(\) => \{\s*if \(!confirmArtifactDiscard\(\)\) return;/,
  "opening side chat must preserve the dirty Markdown discard guard");
assert.match(appSource,
  /const closeBtw = \(btwSid = activeBtwSid\) => \{[\s\S]*?sendCloseBtw\(btwSid\)/,
  "the explicit close action must target only the selected side chat");
assert.match(appSource,
  /latest\.collapseBtw\(\);[\s\S]{0,80}else latest\.openBtw\(\)/,
  "the keyboard shortcut must collapse the panel without destroying a chat");
assert.match(appSource, /onNew=\{createBtw\}/,
  "the visible side-chat panel must expose creation of additional chats");
assert.doesNotMatch(appSource, /sendCloseBtw\(msg\.btw_sid\)/,
  "navigation or a late open response must never destroy a side chat");
assert.match(appSource,
  /msg\.revision < stateRef\.current\.btwRevision[\s\S]{0,650}pendingBtwByParentRef\.current\.delete\(requestedParent\)[\s\S]{0,450}setBtwOpeningFor\(requestedParent, false\)/,
  "a newer BTW catalog must settle its stale cached open response");
assert.equal((appSource.match(/sendCloseBtw\(/g) ?? []).length, 1,
  "only the explicit per-chat close action may destroy a side chat");
assert.match(appSource,
  /const previewBtwFile = [\s\S]{0,120}previewFileForSid\(activeBtwSid/,
  "BTW file actions must stay bound to the visible session's fork");
assert.match(appSource,
  /activeBtwSid\s*\?\s*inlineImageAssetsRef\.current\.forSession\(activeBtwSid\)/,
  "the visible BTW must read only its own inline-image cache projection");
assert.match(appSource,
  /sendGetPreviewAsset\(path,\s*previewId,\s*requestId,\s*sid\)/,
  "message image reads must carry their explicit main or BTW session id");
assert.match(appSource,
  /focusedSid !== sid[\s\S]{0,180}activeBtwSid !== sid/,
  "background sessions outside the focused main and visible BTW stay unable to read files");
assert.match(btwPanelSource,
  /<ChatView[\s\S]{0,400}imageAssets=\{p\.imageAssets\}[\s\S]{0,120}onLoadImage=\{p\.onLoadImage\}[\s\S]{0,120}onAuthorizeImage=\{p\.onAuthorizeImage\}/,
  "BTW chat rendering must receive the same bounded image authorization channel");
assert.match(btwPanelSource, /<ChatView[\s\S]{0,100}engine=\{p\.engine\}/,
  "BTW rendering must use its real engine so Codex hook plumbing stays filtered");
assert.match(appSource, /sendInterruptTo\(sid\)/,
  "BTW stop must target the captured fork sid");
assert.match(appSource, /sendSetModelTo\(sid, model\)/,
  "BTW model changes must never follow focusedSid");
assert.match(appSource, /sendSetEffortTo\(sid, effort\)/,
  "BTW effort changes must never follow focusedSid");
assert.match(appSource, /btwSendModeBySid/,
  "BTW send mode must not reuse the main composer's global mode");
assert.match(appSource, /if \(latest && latest\.session_id !== state\.focusedSid\) \{\s*dispatch\(\{ type: "exit_new_chat" \}\)/,
  "restored focus must replace the temporary new-session page");
assert.match(appSource, /<HeaderMenu[\s\S]*notificationMode=\{notificationMode\}/,
  "authenticated header actions must be grouped behind the three-dot menu");
assert.doesNotMatch(appSource, /className="iconbtn header-theme"/,
  "the authenticated header must not retain a separate theme shortcut");
assert.doesNotMatch(appSource, /aria-label="退出登录" title="退出登录"><Icon name="logout"/,
  "the authenticated header must not retain a separate logout shortcut");
assert.match(appSource, /rt\.replaying \|\| !rt\.syncReady \? "syncing" : "online"/,
  "cached terminal state must be downgraded until the focused session is authoritative");
assert.match(appSource, /legacyExternal=\{!rt\.control && !!rt\.external\}/,
  "rolling-deploy compatibility keeps legacy external ownership actionable");
assert.match(appSource, /sessionControlLocksInput\(rt\.control\)/,
  "Shift+Tab must not mutate controls while the authoritative session is read-only");
assert.match(appSource,
  /state\.connState !== "connected" \|\| !state\.wrapperOnline\) return;[\s\S]{0,520}sendContextRequestTo\(focusedSid, deferred\)/,
  "a focused session must prime its context ring after initial sync and reconnect");
assert.doesNotMatch(appSource, /className="work-artifacts-btn"/);
assert.doesNotMatch(appSource, /className="work-head-manage"/);
assert.doesNotMatch(appSource, /sendSetWorkGrant|目录授权/);
assert.match(appSource,
  /focusedSession\?\.native_session_id[\s\S]{0,180}nativeProfileSessionId\(rt\.ccSessionId\)/,
  "the Work header must not expose a profile-namespaced routing id");
assert.match(appSource,
  /<span className=\{`work-profile-owner tone-\$\{focusedWorkProfile\.tone\}`\}/,
  "multi-account Work must expose its immutable owner in the header");
assert.doesNotMatch(appSource,
  /<button className=\{`work-profile-owner/,
  "an existing Work owner is informational and must not become switchable");
assert.match(appSource,
  /workDashboardMachineId === machineId[\s\S]{0,180}workDashboards\[engine\]/,
  "Work dashboard data must not cross a device boundary");
assert.match(appSource,
  /<WorkDashboardSheet[\s\S]{0,180}key=\{sessionScopeKey\(machineId, engine, "work"\)\}/,
  "the Work manager must remount across device and engine scopes");
assert.match(appSource,
  /setWorkManagerOpen\(false\);[\s\S]{0,180}setWorkDashboards\(\{\}\);/,
  "switching devices must close and clear the previous Work manager");
const sidebarSource = readFileSync(
  resolve(process.cwd(), "src/components/SessionsSidebar.tsx"), "utf8");
assert.doesNotMatch(sidebarSource, /onGrant|目录授权/);
assert.match(sidebarSource,
  /disabled=\{archiveBlocked\}[\s\S]{0,160}请停止当前任务并清空排队后再归档/,
  "running Codex sessions must not expose an actionable archive command");
assert.match(sidebarSource,
  /profileFilters\[profileScopeKey\]\s*\?\?\s*"all"/,
  "Code, Work and different devices must not share one account filter");
const newChatSource = readFileSync(
  resolve(process.cwd(), "src/components/NewChatView.tsx"), "utf8");
assert.match(newChatSource, /autoFocus=\{autoFocus\}/,
  "new-chat focus must follow the navigation intent instead of being unconditional");
assert.match(newChatSource, /<PendingImageAttachments/,
  "new-chat attachments must share the interactive image preview");
assert.match(newChatSource, /executionControls\.scopeKey === controlScopeKey/,
  "new-chat execution controls must be scoped before the first switched render");
assert.match(newChatSource, /space === "work" \? "never" : permissionMode/,
  "Codex Work must request its authoritative non-escalating approval policy");
assert.match(appSource, /space === "work" \? "never" : permissionMode/,
  "the atomic new-session wire must retain the authoritative Work policy");
const composerSource = readFileSync(
  resolve(process.cwd(), "src/components/Composer.tsx"), "utf8");
assert.match(composerSource,
  /locked = offline \|\| !!controlUi\?\.locked \|\| p\.archived === true/,
  "archived history must keep the composer read-only");
assert.match(composerSource,
  /loading=\{p\.engine === "codex" && p\.statusLoading\}[\s\S]{0,80}disabled=\{locked\}[\s\S]{0,100}if \(locked\) return/,
  "a locked composer must not reopen live account controls");
assert.match(composerSource,
  /aria-label="上下文占用"[\s\S]{0,100}disabled=\{locked\}[\s\S]{0,100}if \(locked\) return/,
  "a locked composer must not reopen or request live context");
const contextPopoverSource = readFileSync(
  resolve(process.cwd(), "src/components/ContextPopover.tsx"), "utf8");
for (const [surface, source] of [
  ["session composer", composerSource],
  ["new chat", newChatSource],
  ["BTW", btwPanelSource],
] as const) {
  assert.match(source, /\buuid\(\)/,
    `${surface} long pastes must use the insecure-origin UUID fallback`);
  assert.doesNotMatch(source, /crypto\.randomUUID\(/,
    `${surface} must remain usable over supported private-IP HTTP origins`);
}
assert.match(composerSource, /<PendingImageAttachments/,
  "session drafts must expose the shared interactive image preview");
assert.match(composerSource, /workSurface \? \(/);
assert.match(composerSource, /p\.draftStore\.get\(p\.draftKey\)/);
assert.match(composerSource, /accept="image\/\*" multiple/);
assert.match(composerSource, /aria-label="添加照片"/);
assert.match(composerSource, /aria-label="添加文件"/);
assert.match(composerSource, /className="work-compose-card"/);
assert.match(composerSource, /Artifacts · \{p\.workArtifactCount\}/);
assert.doesNotMatch(composerSource, /交付物/);
assert.doesNotMatch(composerSource, /项目与资料/);
assert.match(composerSource, /工作设置/);
assert.match(contextPopoverSource, /会话新增上下文/);
assert.match(composerSource, /workContext\.session_percentage \?\? workContext\.percentage/,
  "the Work summary uses the report's precomputed session percentage; details load on demand");
assert.match(contextPopoverSource,
  /usage\(p\.report\.total_tokens, p\.report\.percentage\)/,
  "Code must render the last native engine-total context reading");
assert.match(composerSource, /contextAvailable = p\.contextReport\?\.available !== false/,
  "an absent tokenUsage report must not be rendered as a real zero");
assert.match(contextPopoverSource, /正在读取真实上下文/,
  "the context popover must explain that its native reading is still loading");
assert.match(composerSource, /ref=\{workSettingsRef\}/);
assert.match(composerSource, /document\.addEventListener\("pointerdown", onPointerDown\)/);
assert.match(composerSource, /disabled=\{locked\}[\s\S]*?: "选择模型"/,
  "external read-only sessions must disable model changes");
assert.match(composerSource, /disabled=\{locked\}[\s\S]*?: "思考强度"/,
  "external read-only sessions must disable effort changes");
assert.match(composerSource, /const deferredClaudeControls = externalClaudeOwner !== null/,
  "native Claude ownership must distinguish saved takeover preferences from live controls");
assert.match(composerSource, />\s*接管后\s*<\/span>/,
  "saved Claude model and effort must be labelled as post-takeover controls");
assert.match(composerSource, /当前权限模式未公开/,
  "native Claude ownership must not present Remote's saved permission as live state");
assert.match(composerSource, /不是\$\{externalClaudeOwner\}当前模型/,
  "native Claude ownership must not present Remote's saved model as live state");
assert.match(composerSource, /不是\$\{externalClaudeOwner\}当前强度/,
  "native Claude ownership must not present Remote's saved effort as live state");
assert.doesNotMatch(composerSource, /终端占用/,
  "shared control must never be presented as exclusive terminal occupancy");
assert.match(composerSource, /presentLegacyExternalControl/);
assert.match(composerSource, /p\.engine === "codex"[\s\S]*?<kbd>\$<\/kbd> Skills/,
  "desktop Codex controls must advertise the $ Skill completion trigger");
assert.match(composerSource, /skillMatches\[0\]\.name/,
  "Enter must complete the first matching Skill instead of sending a partial trigger");
assert.match(composerSource, /正在读取当前目录的 Skills/,
  "the Skill palette must stay visible while native discovery is in flight");
assert.match(appSource, /skills=\{skillCatalogs\[focusedSkillCatalogKey\]\?\.items\}/,
  "the composer must paint a cwd-scoped Skill cache synchronously");
assert.match(appSource,
  /onRequestSkills=\{\(\) => \{[\s\S]{0,500}skillsOnly: true/,
  "$ completion must request only the lightweight Skill inventory");
assert.match(appSource,
  /onOpenExtensions=\{\(kind\) => \{[\s\S]{0,600}skillsOnly: false/,
  "the extensions sheet must continue requesting the complete inventory");
assert.match(appSource,
  /report=\{capabilitiesByScope\[focusedSkillCatalogKey\] \?\? null\}/,
  "the full Extensions report must share the machine and cwd scope key");
assert.match(appSource,
  /skillCatalogRefreshSucceeded\(msg\)[\s\S]{0,200}storeSkillCatalog/,
  "a failed Skill refresh must not replace a usable cached catalog");
assert.match(appSource, /Warm the cwd\/account-scoped Skill catalog[\s\S]*requestSkillCatalog/,
  "focused sessions must prefetch account-scoped Skills before the user types $");
assert.match(appSource,
  /msg\.type === "wrapper_reconnected"[\s\S]*skillCatalogsRef\.current = \{\}[\s\S]*requestSkillCatalog\(focusedSkills, true\)/,
  "a wrapper generation change must invalidate and rewarm native Skill catalogs");
assert.doesNotMatch(appSource,
  /onRequestSkills=\{\(\) => \{[\s\S]{0,500}delete next\[/,
  "opening $ completion must never clear a usable Skill cache");
assert.doesNotMatch(composerSource, /control-bar/,
  "terminal state no longer consumes a permanent row above the composer");
assert.doesNotMatch(composerSource, /hint-mode[^]*stateZh\[p\.state\]/,
  "the composer permission chip must not repeat the header runtime state");
const permissionControlIndex = composerSource.indexOf('className={"hint-mode"');
const runtimeControlsIndex = composerSource.indexOf('className="hint-right"');
const shortcutHintIndex = composerSource.indexOf('className="hint-kbds"');
assert.ok(permissionControlIndex >= 0
    && permissionControlIndex < shortcutHintIndex
    && shortcutHintIndex < runtimeControlsIndex,
  "desktop composer must keep its original split control layout");
assert.doesNotMatch(composerSource, /发消息…/,
  "the compact composer placeholder must not wrap a redundant send label");
assert.match(composerSource, /输入 \/ 命令，\$ Skill/,
  "the Codex placeholder must retain command and Skill discovery");
assert.match(composerSource, /p\.fast \? "快速" : "标准"/,
  "the Fast control must use stable text labels");
assert.doesNotMatch(composerSource, /⚡/,
  "the Fast control must not add a lightning marker");
const workDashboardSource = readFileSync(
  resolve(process.cwd(), "src/components/WorkDashboardSheet.tsx"), "utf8");
assert.match(workDashboardSource,
  /Drafts and account choices may outlive[\s\S]*\[props\.scopeKey\]/,
  "Work manager drafts and schedule owners must reset across control scopes");
assert.match(workDashboardSource, /<DateTimePicker value=\{scheduleAt\}/,
  "Work schedules must use the themed date-time picker");
assert.doesNotMatch(workDashboardSource, /datetime-local/,
  "Work schedules must not expose the browser-native date-time popup");
const dateTimePickerSource = readFileSync(
  resolve(process.cwd(), "src/components/DateTimePicker.tsx"), "utf8");
assert.match(dateTimePickerSource, /createPortal\(<>.*document\.body\)/s,
  "the date-time popover must escape the scrollable Work manager container");
const appCssSource = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
assert.doesNotMatch(appCssSource, /\.hint-mode\.busy/,
  "the composer permission chip must not duplicate runtime state by color");
assert.match(appCssSource, /\.hint-right\{[^}]*margin-left:auto/,
  "desktop composer controls must retain the original right alignment");
assert.match(appCssSource,
  /@media \(max-width:640px\)\{[\s\S]*?\.hint-kbds\{ display:none; \}[\s\S]*?\.hint-right\{[^}]*margin-left:0;[^}]*flex:1;[^}]*justify-content:flex-end;/,
  "mobile composer controls must stay compact at the end of the row");
assert.match(appCssSource,
  /@media \(max-width:420px\)\{[\s\S]*?\.hint\{[^}]*flex-wrap:nowrap;[\s\S]*?\.hint-right\{[^}]*width:auto;[^}]*flex:1 1 auto;/,
  "phone composer controls must remain on one row");
assert.match(appCssSource,
  /@media \(max-width:420px\)\{[\s\S]*?\.hint\{[^}]*gap:11px;[\s\S]*?\.hint-right\{[^}]*gap:11px;/,
  "phone composer controls must retain readable separation");
assert.match(appCssSource,
  /\.usage-meter-line \.good\{ background:color-mix\(in srgb,var\(--ok\) 68%,white\); \}/,
  "healthy compact quota bars must use a softer green");
assert.match(appCssSource, /\.capabilities-sheet>header\{[^}]*flex:none/s,
  "the Extensions header must not collapse under a long capability list");
assert.match(appCssSource, /\.capabilities-tabs\{[^}]*flex:none/s,
  "the Extensions tabs must remain visible above a long capability list");
assert.match(appCssSource, /\.capabilities-body\{[^}]*flex:1; min-height:0/s,
  "only the Extensions body may consume and scroll through remaining height");
assert.doesNotMatch(appCssSource, /\.work-form-actions button/,
  "Work action styles must not repaint nested calendar buttons");
assert.match(appCssSource, /\.work-form-actions>button/,
  "Work action styles must be limited to direct action buttons");
assert.match(appCssSource, /\.date-time-days button\{ height:32px/,
  "desktop calendar rows must stay compact enough to fit above the viewport edge");
assert.match(appCssSource,
  /@media \(max-width:640px\)\{[\s\S]*?\.work-settings\{ position:static; \}[\s\S]*?\.work-settings-pop\{ left:0; right:0; width:auto; \}/,
  "mobile Work settings must use the full composer footer instead of overflowing its trigger");
const sessionControlUiSource = readFileSync(
  resolve(process.cwd(), "src/session-control-ui.ts"), "utf8");
assert.match(sessionControlUiSource, /Codex App 使用中 · Web 只读/);
assert.match(sessionControlUiSource, /外部 CLI 控制中/);
assert.match(sessionControlUiSource, /未检测到本机终端/,
  "backend connectivity must not be presented as confirmed terminal ownership");
const terminalControlSource = readFileSync(
  resolve(process.cwd(), "src/components/TerminalControl.tsx"), "utf8");
assert.match(terminalControlSource, /control\?\.control_mode === "external_cli"/);
assert.match(terminalControlSource, /onTakeover\?\.\(\)/,
  "can_takeover remains a displayed request action, not client-side authorization");
assert.match(terminalControlSource, /aria-label="终端连接状态"/);
assert.match(terminalControlSource, /event\.key !== "Tab"/);
assert.match(terminalControlSource, /document\.contains\(trigger\).*trigger\.focus\(\)/,
  "dialog close restores focus to its terminal-status trigger");
assert.doesNotMatch(layoutCss, /\.header-secondary\{ display:none!important; \}/,
  "mobile headers must not blanket-hide Agent and notification controls");
assert.doesNotMatch(layoutCss, /\.hstat-label\{ display:none; \}/,
  "mobile headers must retain the runtime state label");
assert.doesNotMatch(layoutCss, /\.c-head \.hstat\{ display:none; \}/,
  "tiny headers must retain runtime state");
assert.doesNotMatch(appSource, /className="iconbtn header-agent"/,
  "extension management is a slash command, not permanent header chrome");
assert.match(composerSource, /case "skills": p\.onOpenExtensions\?\.\("skill"\)/,
  "Skills remain reachable from the composer on mobile");
assert.match(composerSource, /case "hooks": p\.onOpenExtensions\?\.\("hook"\)/,
  "Hooks remain reachable from the composer on mobile");
const headerMenuSource = readFileSync(
  resolve(process.cwd(), "src/components/HeaderMenu.tsx"), "utf8");
assert.match(headerMenuSource, /createPortal\(/,
  "the desktop menu must escape the clipped header");
assert.match(headerMenuSource, /event\.key === "Escape"/);
assert.match(headerMenuSource, /document\.contains\(trigger\).*trigger\.focus\(\)/s,
  "closing the menu restores focus to the three-dot trigger");
assert.match(headerMenuSource, /data-lock-horizontal-swipe/,
  "the menu trigger and portal must opt out of global horizontal navigation");
assert.match(headerMenuSource,
  /<div className="header-menu-scrim"[^>]*onClick=/s,
  "outside taps must close only after the complete click to prevent touch-through");
assert.doesNotMatch(headerMenuSource,
  /<div className="header-menu-scrim"[^>]*onPointerDown=/s,
  "pointerdown must not unmount the scrim before the tap click is dispatched");
assert.doesNotMatch(headerMenuSource, /header-menu-close/,
  "the anchored menu closes through its trigger, outside tap, or Escape without an extra X");
assert.match(headerMenuSource,
  /top: Math\.min\(rect\.bottom \+ 8, window\.innerHeight - 24\)/,
  "the header menu must stay anchored below its trigger within the viewport");
assert.match(headerMenuSource,
  /engine === "codex" && <button[\s\S]*使用活动/,
  "account activity must be a Codex-only header-menu action");
assert.match(appSource,
  /<HeaderMenu[\s\S]*engine=\{engine\}[\s\S]*onOpenUsageActivity=\{openUsageActivity\}/,
  "the app must wire the selected engine and activity opener into the menu");
assert.match(appSource,
  /usageActivityOpen && engine === "codex" && <Suspense[\s\S]{0,120}<UsageActivitySheet/,
  "the activity sheet must fail closed when the selected surface is Claude");
assert.match(layoutCss, /\.header-menu-card\{[^}]*position:fixed/s);
assert.match(layoutCss,
  /\.header-menu-card\{[^}]*right:max\(var\(--header-menu-right\),env\(safe-area-inset-right\)\)/s,
  "the anchored header menu must respect the iPhone right safe area");
assert.doesNotMatch(layoutCss,
  /\.header-menu-card\{ top:auto!important; right:0!important; bottom:/,
  "mobile header actions must remain anchored to the three-dot trigger");
assert.match(layoutCss, /env\(safe-area-inset-bottom\)/,
  "mobile overlays and composers must preserve the home-indicator safe area");
const serviceWorkerSource = readFileSync(
  resolve(process.cwd(), "public/sw.js"), "utf8");
assert.match(serviceWorkerSource, /url\.pathname === "\/healthz"/,
  "protocol health probes must bypass the service-worker cache");
assert.match(serviceWorkerSource, /url\.pathname === "\/cc-remote-build\.json"/,
  "the deployed protocol manifest must bypass the service-worker cache");
assert.match(serviceWorkerSource, /existing\.postMessage\(\{[\s\S]*cc-remote-notification/,
  "an already-open page must receive the exact notification target");
assert.match(serviceWorkerSource, /self\.clients\.openWindow\(target\)/,
  "cold notification clicks must retain the fragment route");
assert.match(serviceWorkerSource,
  /url\.pathname === "\/html-preview-runner\.html"[\s\S]*url\.pathname === "\/html-preview-runner\.js"[\s\S]*return/,
  "the isolated HTML runner must bypass shell and asset caching");
assert.match(layoutCss, /var\(--app-height,100dvh\) - var\(--keyboard-inset,0px\)/,
  "keyboard-aware mobile sheets must account for the virtual keyboard inset");

class MemoryStorage {
  readonly values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}
const legacyNotifications = new MemoryStorage();
legacyNotifications.setItem("cc_remote_notifications", "1");
assert.equal(readNotificationMode(legacyNotifications), "generic");
assert.equal(legacyNotifications.getItem(NOTIFICATION_MODE_KEY), "generic",
  "legacy enabled users must migrate to the privacy-preserving generic mode");
writeNotificationMode(legacyNotifications, "session");
assert.equal(legacyNotifications.getItem("cc_remote_notifications"), "1",
  "the rollback-compatible enabled marker remains set for both active modes");
writeNotificationMode(legacyNotifications, "off");
assert.equal(legacyNotifications.getItem("cc_remote_notifications"), null);

const notificationRoute = {
  machine_id: "nono",
  session_id: "session-a",
  engine: "codex" as const,
  space: "work" as const,
};
const notificationUrl = encodeNotificationRoute(notificationRoute);
assert.match(notificationUrl, /^\/#notification=/);
assert.deepEqual(
  parseNotificationFragment(new URL(notificationUrl, "https://remote.example").hash),
  notificationRoute,
);
assert.equal(parseNotificationFragment(
  `#notification=${encodeURIComponent(JSON.stringify({
    ...notificationRoute, injected: "value",
  }))}`), null, "unknown notification-route fields must fail closed");
assert.equal(parseNotificationFragment(
  `#notification=${encodeURIComponent(JSON.stringify({
    ...notificationRoute, session_id: "../../other",
  }))}`), null, "notification routes accept only protocol-safe session ids");
assert.equal(parseNotificationFragment(
  `?notification=${encodeURIComponent(JSON.stringify(notificationRoute))}`), null,
  "notification routing must never use a query string");
let replacedNotificationUrl = "";
assert.deepEqual(captureNotificationFragment({
  hash: new URL(notificationUrl, "https://remote.example").hash,
  pathname: "/",
  search: "",
}, {
  setItem() { throw new Error("storage unavailable"); },
}, {
  replaceState(_data, _unused, url) {
    replacedNotificationUrl = String(url);
  },
}), notificationRoute,
"a valid route remains usable in memory when sessionStorage is unavailable");
assert.equal(replacedNotificationUrl, "/",
  "notification fragments must be cleared even when storage is unavailable");

const notificationOrigin = {
  machineId: "mac",
  engine: "claude" as const,
  space: "code" as const,
  sid: "origin-session",
};
const navigationBase = {
  target: notificationRoute,
  origin: notificationOrigin,
  deviceState: "ready" as const,
  authorizedMachineIds: ["mac", "nono"],
  machineId: "nono",
  engine: "claude" as const,
  space: "code" as const,
};
assert.deepEqual(resolveNotificationNavigation({
  ...navigationBase,
  authoritativeSessions: null,
}), {
  kind: "request_list",
  engine: "codex",
  space: "work",
}, "the authoritative target list must arrive before focus");
assert.deepEqual(resolveNotificationNavigation({
  ...navigationBase,
  authoritativeSessions: [],
}), {
  kind: "fail",
  reason: "session_missing",
  restore: notificationOrigin,
}, "an unknown cross-device sid must restore the exact origin");
assert.deepEqual(resolveNotificationNavigation({
  ...navigationBase,
  origin: { ...notificationOrigin, machineId: "nono" },
  authoritativeSessions: [],
}), {
  kind: "fail",
  reason: "session_missing",
  restore: null,
}, "an unknown sid on another surface must not switch that surface");
const listedNotificationSession = {
  session_id: notificationRoute.session_id,
  summary: "Release prep",
};
assert.deepEqual(resolveNotificationNavigation({
  ...navigationBase,
  authoritativeSessions: [listedNotificationSession],
}), {
  kind: "switch_surface",
  engine: "codex",
  space: "work",
  session: {
    ...listedNotificationSession,
    engine: "codex",
    space: "work",
  },
});
assert.deepEqual(resolveNotificationNavigation({
  ...navigationBase,
  machineId: "mac",
  authoritativeSessions: null,
}), {
  kind: "switch_machine",
  machineId: "nono",
}, "a validated cross-device target switches only the machine first");
assert.deepEqual(resolveNotificationNavigation({
  ...navigationBase,
  deviceState: "error",
  authoritativeSessions: null,
}), {
  kind: "fail",
  reason: "devices_unavailable",
  restore: notificationOrigin,
}, "device-list failure after a cross-device switch restores the origin");

type DeferredBinding = {
  target: PushBindingTarget | null;
  resolve: (enabled: boolean) => void;
};
const bindingCalls: DeferredBinding[] = [];
const bindingCoordinator = new PushBindingCoordinator(
  (target) => new Promise<boolean>((resolveBinding) => {
    bindingCalls.push({ target, resolve: resolveBinding });
  }),
);
const bindingA = { machineId: "machine-a", mode: "generic" as const };
const bindingB = { machineId: "machine-b", mode: "session" as const };
const bindA = bindingCoordinator.setTarget(bindingA);
const bindB = bindingCoordinator.setTarget(bindingB);
await Promise.resolve();
assert.deepEqual(bindingCalls.map((entry) => entry.target), [bindingA],
  "machine rebinds must be serialized");
bindingCalls[0].resolve(true);
await Promise.resolve();
await Promise.resolve();
assert.deepEqual(bindingCalls.map((entry) => entry.target), [bindingA, bindingB]);
assert.equal(bindingCoordinator.isRemoteActive("machine-b"), false,
  "an old machine binding must not masquerade as the new target");
bindingCalls[1].resolve(true);
await Promise.all([bindA, bindB]);
assert.equal(bindingCoordinator.snapshot().state, "remote");
assert.equal(bindingCoordinator.isRemoteActive("machine-b"), true);
assert.equal(bindingCoordinator.isRemoteActive("machine-a"), false);

const reboundCalls: DeferredBinding[] = [];
const reboundCoordinator = new PushBindingCoordinator(
  (target) => new Promise<boolean>((resolveBinding) => {
    reboundCalls.push({ target, resolve: resolveBinding });
  }),
);
const reboundA1 = reboundCoordinator.setTarget(bindingA);
const reboundB = reboundCoordinator.setTarget(bindingB);
const reboundA2 = reboundCoordinator.setTarget(bindingA);
await Promise.resolve();
reboundCalls[0].resolve(true);
await Promise.all([reboundA1, reboundB, reboundA2]);
assert.deepEqual(reboundCalls.map((entry) => entry.target), [bindingA],
  "A to B to A coalesces to the latest binding without stale completion");
assert.equal(reboundCoordinator.isRemoteActive("machine-a"), true);

const recoveryCalls: DeferredBinding[] = [];
const recoveryCoordinator = new PushBindingCoordinator(
  (target) => new Promise<boolean>((resolveBinding) => {
    recoveryCalls.push({ target, resolve: resolveBinding });
  }),
);
const recoveryA = recoveryCoordinator.setTarget(bindingA);
await Promise.resolve();
recoveryCalls[0].resolve(true);
await recoveryA;
const recoveryB = recoveryCoordinator.setTarget(bindingB);
await Promise.resolve();
assert.equal(recoveryCoordinator.isRemoteActive("machine-b"), false);
recoveryCalls[1].resolve(false);
await recoveryB;
assert.equal(recoveryCoordinator.snapshot().state, "local");
assert.equal(recoveryCoordinator.isRemoteActive("machine-a"), false,
  "a failed machine-B bind cannot report the stale machine-A endpoint as active");
await recoveryCoordinator.setTarget(bindingA);
assert.equal(recoveryCoordinator.snapshot().state, "remote",
  "returning to the last confirmed binding recovers without another mutation");

const modeCalls: DeferredBinding[] = [];
const modeCoordinator = new PushBindingCoordinator(
  (target) => new Promise<boolean>((resolveBinding) => {
    modeCalls.push({ target, resolve: resolveBinding });
  }),
);
const genericA = modeCoordinator.setTarget(bindingA);
await Promise.resolve();
modeCalls[0].resolve(true);
await genericA;
const sessionA = {
  machineId: bindingA.machineId,
  mode: "session" as const,
};
const modeUpdate = modeCoordinator.setTarget(sessionA);
await Promise.resolve();
assert.equal(modeCoordinator.snapshot().state, "binding");
assert.equal(modeCoordinator.isRemoteActive(bindingA.machineId), true,
  "same-machine privacy-mode updates retain remote ownership while binding");
modeCalls[1].resolve(false);
await modeUpdate;
assert.equal(modeCoordinator.isRemoteActive(bindingA.machineId), true,
  "a failed same-machine update must not add a duplicate local notification");

const successfulTurn = {
  v: 20, type: "turn_end" as const, ts: 1, sid: "session-a", turn_id: "turn-a",
  result: { subtype: "success", duration_ms: 1, is_error: false },
  notification_context: {
    engine: "codex" as const,
    space: "work" as const,
    display_name: "Release prep",
  },
};
const interruptedTurn = {
  ...successfulTurn, ts: 2, turn_id: "turn-b",
  result: { subtype: "error_during_execution", duration_ms: 2, is_error: true },
};
assert.equal(classifyTurnNotification(successfulTurn.result), "success");
assert.equal(classifyTurnNotification(interruptedTurn.result), "interrupted");
assert.equal(turnNotificationBody("Codex", interruptedTurn.result), "Codex 会话已中断");
assert.notEqual(turnNotificationTag(successfulTurn), turnNotificationTag(interruptedTurn),
  "successive turns in one session must not replace each other's notifications");
assert.deepEqual(turnNotificationPresentation(successfulTurn, "session"), {
  title: "Release prep",
  body: "Codex 会话已经完成",
  sessionId: "session-a",
  engine: "codex",
  space: "work",
});
assert.equal(
  turnNotificationPresentation(interruptedTurn, "session").body,
  "Codex 会话已中断",
);
assert.equal(
  turnNotificationPresentation(successfulTurn, "generic").sessionId,
  null,
  "generic notifications must not retain an exact route",
);
const chatViewSource = readFileSync(
  resolve(process.cwd(), "src/components/ChatView.tsx"), "utf8");
assert.match(chatViewSource, /<TurnChangesPanel/);
assert.match(chatViewSource,
  /const terminalProblem = t\.done[\s\S]{0,120}const working = !terminalProblem/,
  "an interrupted or failed terminal must beat stale open process metadata");
assert.match(chatViewSource, /t\.done && t\.interrupted && !t\.error/,
  "an interrupted label must never render beside a live spinner");
assert.match(layoutCss, /\.work-thread-shell \.thread-in\s*\{/);

assert.equal(relay.sendTakeover("codex-model-session"), true);
const takeoverFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(takeoverFrame.type, "takeover");
assert.equal(takeoverFrame.sid, "codex-model-session");
assert.equal(typeof takeoverFrame.cmd_id, "string");
assert.equal(typeof takeoverFrame.client_id, "string");

// Stale/user-edited localStorage values must never reach strict Pydantic
// command literals. The App applies these guards before listing sessions or
// requesting a themed diff.
assert.equal(normalizeEngine("codex"), "codex");
assert.equal(normalizeEngine("legacy-engine"), "claude");
assert.equal(normalizeEngine(null), "claude");
assert.equal(normalizeDiffTheme("dark"), "dark");
assert.equal(normalizeDiffTheme("sepia"), "light");
assert.equal(normalizeDiffTheme(null), "light");

// These one-shot commands are just as reliable as chat mutations: each must
// carry the outbox identity needed for reconnect replay and wrapper dedupe.
relay.setFocusedSid("codex-control-session", "codex");
const reliableControlFrames: Array<[string, () => void]> = [
  ["get_status", () => relay.sendGetStatus()],
  ["consume_rate_limit_reset_credit", () =>
    relay.sendConsumeRateLimitResetCredit(
      "codex-control-session", "opaque/credit")],
  ["get_goal", () => relay.sendGetGoal()],
  ["set_goal", () => relay.sendSetGoal("ship it", "active", 1024)],
  ["clear_goal", () => relay.sendClearGoal()],
  ["get_diff", () => relay.sendGetDiff("src/App.tsx", "dark")],
];
for (const [type, sendCommand] of reliableControlFrames) {
  sendCommand();
  const frame = JSON.parse(socket.sent.at(-1) ?? "{}");
  assert.equal(frame.type, type);
  assert.equal(frame.sid, "codex-control-session");
  assert.equal(typeof frame.cmd_id, "string");
  assert.equal(typeof frame.client_id, "string");
  if (type === "consume_rate_limit_reset_credit") {
    assert.equal(frame.credit_id, "opaque/credit");
  }
}
const targetedGoalRequest = relay.sendGetGoalTo("background-goal-session");
assert.equal(typeof targetedGoalRequest, "string");
const targetedGoalFrame = JSON.parse(socket.sent.at(-1) ?? "{}");
assert.equal(targetedGoalFrame.type, "get_goal");
assert.equal(targetedGoalFrame.sid, "background-goal-session");
assert.equal(typeof targetedGoalFrame.cmd_id, "string");

socket.receive({
  type: "snapshot", sid: "s1", cc_session_id: "s1", generation: "g1",
  state: "running", tail_text: "",
});
socket.receive({ type: "delta", sid: "s1", seq: 1, message_id: "m1", text: "X" });
socket.receive({
  type: "replay_start", sid: "s1", generation: "g1", from_seq: 1,
  to_seq: 2, truncated: false, rebuild: false,
});
socket.receive({ type: "delta", sid: "s1", seq: 1, message_id: "m1", text: "X" });
socket.receive({ type: "model", sid: "s1", seq: 2, model: "new" });
socket.receive({ type: "replay_end", sid: "s1", to_seq: 2, truncated: false });
socket.receive({ type: "model", sid: "s1", seq: 1, model: "old" });

assert.deepEqual(
  observed.filter((event) => event.type === "delta").map((event) => event.text),
  ["X"],
);
assert.deepEqual(
  observed.filter((event) => event.type === "model").map((event) => event.model),
  ["new"],
);

// Rebuild deliberately resets the seq epoch, so lower body frames must survive;
// once ReplayEnd closes it, the ordinary duplicate gate applies again.
socket.receive({ type: "delta", sid: "s1", seq: 10, message_id: "old", text: "old" });
socket.receive({
  type: "replay_start", sid: "s1", generation: "g1", from_seq: 1,
  to_seq: 1, truncated: false, rebuild: true,
});
socket.receive({
  type: "delta", sid: "s1", seq: 1, message_id: "rebuilt", text: "rebuilt",
});
socket.receive({ type: "replay_end", sid: "s1", to_seq: 1, truncated: false });
socket.receive({ type: "delta", sid: "s1", seq: 1, message_id: "stale", text: "stale" });
assert.equal(
  observed.filter((event) => event.type === "delta" && event.text === "rebuilt").length,
  1,
);
assert.equal(
  observed.filter((event) => event.type === "delta" && event.text === "stale").length,
  0,
);
assert.equal(relay.lastSeqFor("s1"), 1);

// The same numeric seq belongs to a different cursor domain after generation
// change and must not be mistaken for a duplicate.
// BtwOpened deliberately has no generation. Losing its following Snapshot used
// to leave the old fork stuck open because a normal session's g1 -> g2 change
// only notified the App when a per-btw generation had already been recorded.
socket.receive({
  type: "btw_opened", request_id: "btw-gap", btw_sid: "btw-old",
  parent_sid: "s1", engine: "claude", created_at: 1, revision: 1,
});
socket.receive({
  type: "snapshot", sid: "s1", cc_session_id: "s1", generation: "g2",
  state: "running", tail_text: "",
});
socket.receive({ type: "model", sid: "s1", seq: 1, model: "next-generation" });
assert.equal(
  observed.filter((event) => event.type === "model"
    && event.model === "next-generation").length,
  1,
);
assert.equal(wrapperGenerationChanges, 1);
socket.receive({
  type: "snapshot", sid: "s2", cc_session_id: "s2", generation: "g2",
  state: "idle", tail_text: "",
});
assert.equal(wrapperGenerationChanges, 1); // one notice per wrapper generation

// BtwSync itself may be the first new-generation frame after a sleeping
// browser reconnects. Its newly installed catalog must survive generation
// invalidation so the selected chat's following bounded replay is accepted.
socket.receive({
  type: "btw_sync", generation: "g3", revision: 1,
  sessions: [{
    btw_sid: "btw-restored", parent_sid: "s2", engine: "codex",
    created_at: 3, state: "running",
  }],
});
socket.receive({
  type: "model", sid: "btw-restored", seq: 1, model: "gpt-restored",
});
assert.equal(wrapperGenerationChanges, 2);
assert.equal(observed.filter((event) => event.type === "model"
  && event.sid === "btw-restored" && event.model === "gpt-restored").length, 1,
"a BtwSync catalog must remain authoritative after it changes generation");
relay.stop();

// A separate socket exercises the v15 control watermark without perturbing
// the narrative generation assertions above.
const controlObserved: ServerEvent[] = [];
const controlRelay = new RelayWs({
  onEvent: (event) => { controlObserved.push(event); },
  onConnState: () => {},
});
controlRelay.start();
const controlSocket = FakeWebSocket.instances.at(-1);
assert.ok(controlSocket);
controlSocket.onopen?.();
const wireControlSid = "wire-control-revision";
controlRelay.seedReplayState({}, {}, {
  [wireControlSid]: {
    v: 20, ts: 1, type: "session_control", sid: wireControlSid,
    control_mode: "remote", write_state: "writable",
    terminal_attached: false, generation: "wire-generation", revision: 10,
  },
});
controlSocket.receive({
  type: "session_control", sid: wireControlSid,
  control_mode: "desktop", write_state: "read_only",
  terminal_attached: true, generation: "wire-generation", revision: 9,
});
assert.equal(controlObserved.filter(
  (event) => event.type === "session_control").length, 0,
"the WS watermark must drop an older direct control frame");
controlSocket.receive({
  type: "history", sid: wireControlSid, session_id: wireControlSid,
  revision: "wire-history", generation: "wire-generation",
  has_more: false, events: [],
  control: {
    v: 20, ts: 1, type: "session_control", sid: wireControlSid,
    control_mode: "external_cli", write_state: "read_only",
    terminal_attached: true, generation: "wire-generation", revision: 9,
  },
});
const strippedHistory = controlObserved.at(-1);
assert.equal(strippedHistory?.type, "history");
assert.equal(strippedHistory?.type === "history" && strippedHistory.control,
  undefined, "WS must retain History while stripping only stale control");
controlSocket.receive({
  type: "history", sid: wireControlSid, session_id: wireControlSid,
  revision: "wire-cross-session-history", generation: "wire-generation",
  has_more: false, events: [],
  control: {
    v: 20, ts: 1, type: "session_control", sid: "wire-other-session",
    control_mode: "external_cli", write_state: "read_only",
    terminal_attached: true, generation: "wire-generation", revision: 100,
  },
});
const crossSessionHistory = controlObserved.at(-1);
assert.equal(crossSessionHistory?.type, "history");
assert.equal(crossSessionHistory?.type === "history"
  && crossSessionHistory.control, undefined,
"WS must strip a History control routed to another session");
controlSocket.receive({
  type: "snapshot", sid: wireControlSid, cc_session_id: wireControlSid,
  state: "idle", tail_text: "", generation: "wire-generation",
  control: {
    v: 20, ts: 1, type: "session_control", sid: "wire-other-session",
    control_mode: "external_cli", write_state: "read_only",
    terminal_attached: true, generation: "wire-generation", revision: 100,
  },
});
const crossSessionSnapshot = controlObserved.at(-1);
assert.equal(crossSessionSnapshot?.type, "snapshot");
assert.equal(crossSessionSnapshot?.type === "snapshot"
  && crossSessionSnapshot.control, undefined,
"WS must strip a Snapshot control routed to another session");
controlSocket.receive({
  type: "session_control", sid: wireControlSid,
  control_mode: "external_cli", write_state: "read_only",
  terminal_attached: true, generation: "wire-generation",
  revision: 11, can_takeover: true,
});
assert.equal(controlObserved.at(-1)?.type, "session_control");
controlSocket.receive({
  type: "session_control", sid: wireControlSid,
  control_mode: "remote", write_state: "writable",
  terminal_attached: false, generation: "wire-generation", revision: 11,
});
assert.equal(controlObserved.filter(
  (event) => event.type === "session_control").length, 1,
"an equal-revision conflicting control frame must be dropped");
controlSocket.receive({
  type: "snapshot", sid: wireControlSid, cc_session_id: wireControlSid,
  state: "idle", tail_text: "", generation: "wire-generation-next",
  control: {
    v: 20, ts: 2, type: "session_control", sid: wireControlSid,
    control_mode: "remote", write_state: "writable",
    terminal_attached: false, generation: "wire-generation-next", revision: 0,
  },
});
const nextGenerationSnapshot = controlObserved.at(-1);
assert.equal(nextGenerationSnapshot?.type, "snapshot");
assert.equal(nextGenerationSnapshot?.type === "snapshot"
  && nextGenerationSnapshot.control?.revision, 0);
controlSocket.receive({
  type: "session_control", sid: wireControlSid,
  control_mode: "desktop", write_state: "read_only",
  terminal_attached: true, generation: "wire-generation", revision: 999,
});
assert.equal(controlObserved.at(-1), nextGenerationSnapshot,
  "old-generation direct control must be rejected regardless of revision");

const beforeUnroutedControl = controlObserved.length;
controlSocket.receive({
  type: "session_control", control_mode: "desktop", write_state: "read_only",
  terminal_attached: true, generation: "wire-generation-next", revision: 1000,
});
assert.equal(controlObserved.length, beforeUnroutedControl,
  "an unrouted direct control frame must be dropped at the transport boundary");

// A temp runtime can capture a real id which still has a stale cache watermark.
// The live temp cursor wins together with its generation and control revision.
const aliasOld = "wire-control-temp";
const aliasReal = "wire-control-real";
controlRelay.seedReplayState(
  { [aliasOld]: 3, [aliasReal]: 2 },
  { [aliasOld]: "wire-alias-live", [aliasReal]: "wire-alias-cache" },
  {
    [aliasOld]: {
      v: 20, ts: 3, type: "session_control", sid: aliasOld,
      control_mode: "remote", write_state: "writable",
      terminal_attached: false, generation: "wire-alias-live", revision: 2,
    },
    [aliasReal]: {
      v: 20, ts: 2, type: "session_control", sid: aliasReal,
      control_mode: "desktop", write_state: "read_only",
      terminal_attached: true, generation: "wire-alias-cache", revision: 50,
    },
  },
);
controlSocket.receive({
  type: "session_rekey", old_key: aliasOld, session_id: aliasReal,
});
assert.equal(controlRelay.generationFor(aliasReal), "wire-alias-live");
assert.equal(controlRelay.lastSeqFor(aliasReal), 3);
const afterAliasRekey = controlObserved.length;
controlSocket.receive({
  type: "session_control", sid: aliasReal,
  control_mode: "desktop", write_state: "read_only", terminal_attached: true,
  generation: "wire-alias-cache", revision: 999,
});
assert.equal(controlObserved.length, afterAliasRekey,
  "rekey must not retain the stale real-id generation watermark");
controlRelay.stop();

// Cwd ownership is local transport metadata. A create response freezes the
// surface epoch at send time; a later temp rekey cannot borrow the epoch of a
// surface selected after the request was sent.
const scopedObserved: Array<{ event: ServerEvent; ownership?: {
  scopeKey: string; surfaceEpoch: number; connectionGeneration: number;
} }> = [];
const scopedRelay = new RelayWs({
  onEvent: (event, ownership) => { scopedObserved.push({ event, ownership }); },
  onConnState: () => {},
}, "machine-scope");
scopedRelay.start();
const scopedSocket = FakeWebSocket.instances.at(-1);
assert.ok(scopedSocket);
scopedSocket.onopen?.();
assert.equal(scopedRelay.sendNewSession(
  "/work/a", "claude", null, null,
  { prompt: "hi", msg_id: "create-scope-a" },
), true);
scopedSocket.receive({
  type: "session_focus", session_id: "tmp-scope-a",
  request_id: "create-scope-a", cwd: "/work/a",
});
const createdScoped = scopedObserved.at(-1);
assert.equal(createdScoped?.event.type, "session_focus");
assert.equal(createdScoped?.ownership?.scopeKey, "machine-scope:code:claude");
assert.equal(createdScoped?.ownership?.connectionGeneration, 1);
const originalEpoch = createdScoped?.ownership?.surfaceEpoch;

scopedRelay.setSurface("codex", "code");
scopedRelay.setSurface("claude", "code");
scopedSocket.receive({
  type: "session_rekey", old_key: "tmp-scope-a",
  session_id: "real-scope-a", cwd: "/work/a",
});
const staleRekey = scopedObserved.at(-1);
assert.equal(staleRekey?.event.type, "session_rekey");
assert.equal(staleRekey?.ownership, undefined,
  "A→B→A must not relabel an old temp rekey with the current surface epoch");
assert.ok(originalEpoch != null);

scopedSocket.receive({
  type: "session_rekey", old_key: "unknown-temp",
  session_id: "unknown-real", cwd: "/deleted",
});
assert.equal(scopedObserved.at(-1)?.ownership, undefined,
  "an unknown temp key has no cwd ownership");

const beforeReconnectFrame = scopedObserved.length;
(scopedRelay as unknown as { connect: () => void }).connect();
const replacementSocket = FakeWebSocket.instances.at(-1);
assert.ok(replacementSocket && replacementSocket !== scopedSocket);
replacementSocket.onopen?.();
scopedSocket.receive({
  type: "session_rekey", old_key: "real-scope-a",
  session_id: "too-late", cwd: "/deleted/old-socket",
});
assert.equal(scopedObserved.length, beforeReconnectFrame,
  "a delayed frame from an older underlying WebSocket generation is dropped");
scopedRelay.stop();

// Codex account ownership is frozen together with the normal surface epoch.
// Identical native UUIDs must remain routed by their profile-prefixed sid
// through list -> switch and temp focus -> rekey lifecycles.
const profileOwnershipObserved: Array<{ event: ServerEvent; ownership?: {
  scopeKey: string; codexProfileId?: string | null;
  surfaceEpoch: number; connectionGeneration: number;
} }> = [];
const profileOwnershipRelay = new RelayWs({
  onEvent: (event, ownership) => {
    profileOwnershipObserved.push({ event, ownership });
  },
  onConnState: () => {},
}, "machine-profile");
profileOwnershipRelay.start();
const profileOwnershipSocket = FakeWebSocket.instances.at(-1);
assert.ok(profileOwnershipSocket);
profileOwnershipSocket.onopen?.();
profileOwnershipRelay.setSurface("codex", "code");
assert.equal(profileOwnershipRelay.sendListSessions("codex", "code"), true);
const profileListRequest = JSON.parse(
  profileOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
profileOwnershipSocket.receive({
  type: "session_list",
  engine: "codex",
  space: "code",
  request_id: profileListRequest.cmd_id,
  codex_profiles: [
    { id: "primary", label: "主账号" },
    { id: "stack", label: "Stack" },
  ],
  default_codex_profile_id: "primary",
  sessions: [{
    session_id: "stack@same-native-id",
    native_session_id: "same-native-id",
    codex_profile_id: "stack",
    codex_profile_label: "Stack",
    engine: "codex",
    space: "code",
  }],
});
profileOwnershipRelay.setFocusedSid(
  "stack@same-native-id", "codex", "code");
profileOwnershipRelay.sendSwitchSession(
  "stack@same-native-id", "codex", "code");
profileOwnershipSocket.receive({
  type: "session_focus",
  session_id: "stack@same-native-id",
  cwd: "/repo/stack",
});
assert.equal(
  profileOwnershipObserved.at(-1)?.ownership?.codexProfileId,
  "stack",
  "switch focus inherits the accepted list's exact Codex profile",
);

assert.equal(profileOwnershipRelay.sendNewSession(
  "/repo/stack", "codex", null, null,
  { prompt: "new stack thread", msg_id: "profile-create" },
  "default", "never", undefined, undefined, "fast",
  "code", null, "stack",
), true);
profileOwnershipSocket.receive({
  type: "session_focus",
  session_id: "tmp-profile-create",
  request_id: "profile-create",
  cwd: "/repo/stack",
});
assert.equal(
  profileOwnershipObserved.at(-1)?.ownership?.codexProfileId,
  "stack",
  "new-session focus retains the request-time account",
);
profileOwnershipSocket.receive({
  type: "session_rekey",
  old_key: "tmp-profile-create",
  session_id: "stack@new-native-id",
  cwd: "/repo/stack",
});
assert.equal(
  profileOwnershipObserved.at(-1)?.ownership?.codexProfileId,
  "stack",
  "temp rekey atomically inherits the originating Codex profile",
);
profileOwnershipRelay.setSurface("codex", "work");
assert.equal(profileOwnershipRelay.sendNewSession(
  null, "codex", null, null,
  { prompt: "new stack work", msg_id: "profile-work-create" },
  "default", "never", undefined, undefined, "default",
  "work", null, "stack",
), true);
profileOwnershipSocket.receive({
  type: "session_focus",
  session_id: "tmp-profile-work-create",
  request_id: "profile-work-create",
  cwd: "/private/work/stack",
});
assert.equal(
  profileOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-profile:work:codex",
);
assert.equal(
  profileOwnershipObserved.at(-1)?.ownership?.codexProfileId,
  "stack",
  "a Work temp session retains its request-time account",
);
profileOwnershipRelay.stop();

const goalOwnershipObserved: Array<{ event: ServerEvent; ownership?: {
  scopeKey: string; surfaceEpoch: number; connectionGeneration: number;
} }> = [];
const goalOwnershipRelay = new RelayWs({
  onEvent: (event, ownership) => {
    goalOwnershipObserved.push({ event, ownership });
  },
  onConnState: () => {},
}, "machine-goal");
goalOwnershipRelay.start();
const goalOwnershipSocket = FakeWebSocket.instances.at(-1);
assert.ok(goalOwnershipSocket);
goalOwnershipSocket.onopen?.();
goalOwnershipRelay.setSurface("codex", "work");
assert.equal(goalOwnershipRelay.sendListSessions("codex", "work"), true);
const goalListRequest = JSON.parse(
  goalOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
assert.equal(typeof goalListRequest.cmd_id, "string");
goalOwnershipSocket.receive({
  type: "session_list", engine: "codex", space: "work",
  request_id: goalListRequest.cmd_id,
  sessions: [{
    session_id: "background-goal-session",
    engine: "codex",
    space: "work",
  }],
});
const listedGoalOwnership = goalOwnershipObserved.at(-1)?.ownership;
assert.equal(listedGoalOwnership?.scopeKey, "machine-goal:work:codex");
const correlatedGoalRequest = goalOwnershipRelay.sendGetGoalTo(
  "background-goal-session");
assert.equal(typeof correlatedGoalRequest, "string");
goalOwnershipRelay.setSurface("claude", "code");
goalOwnershipSocket.receive({
  type: "goal_state", sid: "background-goal-session", goal: persistedGoal,
  request_id: correlatedGoalRequest,
});
assert.equal(
  goalOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-goal:work:codex",
  "a Goal read keeps the exact request-time surface after the user switches",
);
goalOwnershipSocket.receive({
  type: "goal_state", sid: "background-goal-session", goal: persistedGoal,
});
assert.equal(
  goalOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-goal:work:codex",
  "a valid background Goal broadcast keeps its accepted session ownership",
);
goalOwnershipSocket.receive({
  type: "goal_state", sid: "unknown-goal-session", goal: persistedGoal,
});
assert.equal(goalOwnershipObserved.at(-1)?.ownership, undefined,
  "an unknown Goal sid must not borrow the active surface");
const beforeOldGoalFrame = goalOwnershipObserved.length;
(goalOwnershipRelay as unknown as { connect: () => void }).connect();
const nextGoalOwnershipSocket = FakeWebSocket.instances.at(-1);
assert.ok(nextGoalOwnershipSocket
  && nextGoalOwnershipSocket !== goalOwnershipSocket);
nextGoalOwnershipSocket.onopen?.();
goalOwnershipSocket.receive({
  type: "goal_state", sid: "background-goal-session", goal: persistedGoal,
});
assert.equal(goalOwnershipObserved.length, beforeOldGoalFrame,
  "a Goal frame from the retired socket generation must be ignored");
goalOwnershipRelay.stop();

// List reads run concurrently and one Codex request may return cached + fresh
// frames. Ownership must follow cmd_id, never response arrival order.
const listOwnershipObserved: Array<{ event: ServerEvent; ownership?: {
  scopeKey: string; surfaceEpoch: number; connectionGeneration: number;
} }> = [];
const listOwnershipRelay = new RelayWs({
  onEvent: (event, ownership) => {
    listOwnershipObserved.push({ event, ownership });
  },
  onConnState: () => {},
}, "machine-list-order");
listOwnershipRelay.start();
const listOwnershipSocket = FakeWebSocket.instances.at(-1);
assert.ok(listOwnershipSocket);
listOwnershipSocket.onopen?.();
assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const oldListRequest = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
listOwnershipRelay.setSurface("codex", "code");
listOwnershipRelay.setSurface("claude", "code");
assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const currentListRequest = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
assert.notEqual(currentListRequest.cmd_id, oldListRequest.cmd_id);

listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: currentListRequest.cmd_id,
  sessions: [{ session_id: "current-list-session" }],
});
assert.equal(
  listOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-list-order:code:claude",
  "a newer request keeps its frozen ownership when its response wins the race",
);
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: oldListRequest.cmd_id,
  sessions: [{ session_id: "stale-list-session" }],
});
assert.equal(listOwnershipObserved.at(-1)?.ownership, undefined,
  "a late old request cannot consume the current surface ownership");

assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const cachedFreshRequest = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
for (const sessionId of ["cached-list-session", "fresh-list-session"]) {
  listOwnershipSocket.receive({
    type: "session_list", engine: "claude", space: "code",
    request_id: cachedFreshRequest.cmd_id,
    sessions: [{ session_id: sessionId }],
  });
  assert.equal(
    listOwnershipObserved.at(-1)?.ownership?.scopeKey,
    "machine-list-order:code:claude",
    "cached and refreshed responses share one exact request ownership",
  );
}

// Sidebar mutations and derived-session commands return SessionList using
// their own transport cmd_id, not an id created by sendListSessions(). Each
// path must therefore register the same frozen ownership before it is sent.
listOwnershipRelay.setSessionEngines([
  { session_id: "mutation-claude", engine: "claude", space: "code" },
  { session_id: "mutation-codex", engine: "codex", space: "code" },
]);
const assertCommandListOwnership = (
  sendCommand: () => unknown,
  expectedType: string,
  engine: "claude" | "codex",
  space: "code" | "work" = "code",
) => {
  listOwnershipRelay.setSurface(engine, space);
  sendCommand();
  const frame = JSON.parse(
    listOwnershipSocket.sent.at(-1) ?? "{}") as {
      type?: string; cmd_id?: string;
    };
  assert.equal(frame.type, expectedType);
  assert.equal(typeof frame.cmd_id, "string");
  listOwnershipSocket.receive({
    type: "session_list", engine, space,
    request_id: frame.cmd_id,
    sessions: [{ session_id: `${expectedType}-result`, engine, space }],
  });
  assert.equal(
    listOwnershipObserved.at(-1)?.ownership?.scopeKey,
    `machine-list-order:${space}:${engine}`,
    `${expectedType} refresh keeps its exact command-time ownership`,
  );
  return frame.cmd_id!;
};
assertCommandListOwnership(
  () => listOwnershipRelay.sendRenameSession(
    "mutation-claude", "renamed", "claude", "code"),
  "rename_session", "claude",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendArchiveSession(
    "mutation-claude", true, "claude", "code"),
  "archive_session", "claude",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendPinSession(
    "mutation-claude", true, "claude", "code"),
  "pin_session", "claude",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendDeleteSession(
    "mutation-claude", "claude", "code"),
  "delete_session", "claude",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendDeleteWorkSession(
    "mutation-work", "claude"),
  "delete_work_session", "claude", "work",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendForkSession(
    "mutation-claude", "fork-point"),
  "fork_session", "claude",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendForkSessionWorktree(
    "mutation-codex", "review-fix"),
  "fork_session_worktree", "codex",
);
assertCommandListOwnership(
  () => listOwnershipRelay.sendMigrateSession(
    "mutation-codex", "/repo/new"),
  "migrate_session", "codex",
);

// Mutation failures may be followed by an authoritative list that reconciles
// an outcome the wrapper could not report atomically.
listOwnershipRelay.setSurface("claude", "code");
listOwnershipRelay.sendArchiveSession(
  "mutation-claude", false, "claude", "code");
const failedMutation = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
assert.equal(typeof failedMutation.cmd_id, "string");
listOwnershipSocket.receive({
  type: "error", code: "internal", message: "outcome uncertain",
  request_id: failedMutation.cmd_id,
});
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: failedMutation.cmd_id,
  sessions: [{ session_id: "archive-reconciled" }],
});
assert.equal(
  listOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-list-order:code:claude",
  "an Error followed by its mutation list keeps exact request ownership",
);

// A newer mutation that terminates with Error only must not suppress an older
// list that is still the newest authoritative response to actually arrive.
// Claude rename/archive and several delete failures have exactly this shape.
assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const listBeforeErrorOnlyMutation = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
listOwnershipRelay.sendRenameSession(
  "mutation-claude", "rename-that-fails", "claude", "code");
const errorOnlyMutation = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
assert.equal(typeof errorOnlyMutation.cmd_id, "string");
listOwnershipSocket.receive({
  type: "error", code: "internal", message: "rename failed",
  request_id: errorOnlyMutation.cmd_id,
});
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: listBeforeErrorOnlyMutation.cmd_id,
  sessions: [{ session_id: "list-survived-error-only-mutation" }],
});
assert.equal(
  listOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-list-order:code:claude",
  "an Error-only mutation cannot suppress an older unconsumed list",
);

// Fork/migrate errors carry their stable product request_id rather than the
// transport cmd_id used by SessionList. Correct ordering cannot depend on an
// Error being able to identify and roll back the list ownership entry.
assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const listBeforeFailedFork = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
const failedForkRequestId = "failed-fork-product-request";
listOwnershipRelay.sendForkSession(
  "mutation-claude", "fork-point", failedForkRequestId);
const failedForkCommand = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
assert.notEqual(failedForkCommand.cmd_id, failedForkRequestId);
listOwnershipSocket.receive({
  type: "error", code: "busy", message: "fork rejected",
  request_id: failedForkRequestId,
});
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: listBeforeFailedFork.cmd_id,
  sessions: [{ session_id: "list-survived-failed-fork" }],
});
assert.equal(
  listOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-list-order:code:claude",
  "a product-request fork error cannot strand an older list",
);

assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const supersededSameScopeRequest = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
assert.equal(listOwnershipRelay.sendListSessions("claude", "code"), true);
const latestSameScopeRequest = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { cmd_id?: string };
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: latestSameScopeRequest.cmd_id,
  sessions: [{ session_id: "latest-same-scope-session" }],
});
assert.equal(
  listOwnershipObserved.at(-1)?.ownership?.scopeKey,
  "machine-list-order:code:claude",
);
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  request_id: supersededSameScopeRequest.cmd_id,
  sessions: [{ session_id: "superseded-same-scope-session" }],
});
assert.equal(listOwnershipObserved.at(-1)?.ownership, undefined,
  "a slower request for the same scope cannot roll back a newer session list");
listOwnershipSocket.receive({
  type: "session_list", engine: "claude", space: "code",
  sessions: [{ session_id: "uncorrelated-list-session" }],
});
assert.equal(listOwnershipObserved.at(-1)?.ownership, undefined,
  "an uncorrelated list fails closed");

const invalidationEventCount = listOwnershipObserved.length;
const sentBeforeInvalidation = listOwnershipSocket.sent.length;
listOwnershipRelay.setSurface("claude", "code");
listOwnershipSocket.receive({
  type: "session_list_invalidated", engine: "claude", space: "code",
});
const invalidationRefresh = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as {
    type?: string; cmd_id?: string; client_id?: string;
  };
assert.equal(listOwnershipSocket.sent.length, sentBeforeInvalidation + 1);
assert.equal(invalidationRefresh.type, "list_sessions");
assert.equal(typeof invalidationRefresh.cmd_id, "string");
assert.equal(listOwnershipObserved.length, invalidationEventCount,
  "a catalog invalidation is transport control, not mutable UI state");

listOwnershipSocket.receive({
  type: "session_list_invalidated", engine: "claude", space: "code",
});
assert.equal(listOwnershipSocket.sent.length, sentBeforeInvalidation + 1,
  "same-scope invalidations coalesce while one correlated list is in flight");
listOwnershipSocket.receive({
  type: "session_list_invalidated", engine: "codex", space: "work",
});
assert.equal(listOwnershipSocket.sent.length, sentBeforeInvalidation + 1,
  "an inactive surface invalidation must not issue or mutate a visible list");

listOwnershipSocket.receive({
  type: "command_ack",
  client_id: invalidationRefresh.client_id,
  cmd_id: invalidationRefresh.cmd_id,
});
const coalescedRefresh = JSON.parse(
  listOwnershipSocket.sent.at(-1) ?? "{}") as { type?: string; cmd_id?: string };
assert.equal(listOwnershipSocket.sent.length, sentBeforeInvalidation + 2);
assert.equal(coalescedRefresh.type, "list_sessions");
assert.notEqual(coalescedRefresh.cmd_id, invalidationRefresh.cmd_id,
  "one dirty bit schedules exactly one follow-up after the first refresh ACK");
listOwnershipRelay.stop();

const stoppedEvents: ServerEvent[] = [];
const stoppedStates: string[] = [];
const stoppedRelay = new RelayWs({
  onEvent: (event) => { stoppedEvents.push(event); },
  onConnState: (state) => { stoppedStates.push(state); },
});
stoppedRelay.start();
const stoppedSocket = FakeWebSocket.instances.at(-1);
assert.ok(stoppedSocket);
stoppedSocket.onopen?.();
const stoppedStateCount = stoppedStates.length;
stoppedRelay.stop();
stoppedSocket.receive({
  type: "state", sid: "stopped-session", state: "running",
});
stoppedSocket.onopen?.();
stoppedSocket.onclose?.({ code: 1006 });
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(stoppedEvents.length, 0,
  "stop must invalidate delayed frames from the retired socket");
assert.equal(stoppedStates.length, stoppedStateCount,
  "retired socket open/close callbacks cannot mutate the next App lifecycle");

// Query acceptance is a per-session lifecycle barrier, not an outbox ACK bit.
// It survives this RelayWs instance's automatic reconnect and only correlated
// authoritative narrative proof may release it.
const acceptanceRelay = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
acceptanceRelay.start();
const acceptanceSocket = FakeWebSocket.instances.at(-1);
assert.ok(acceptanceSocket);
acceptanceSocket.onopen?.();
acceptanceRelay.setFocusedSid("accept-wire-a", "claude", "code");
assert.equal(acceptanceRelay.sendQuery(
  "first", "accept-wire-message-a"), true);
const acceptanceFrameA = JSON.parse(acceptanceSocket.sent.at(-1) ?? "{}");
assert.equal(acceptanceRelay.sendQuery(
  "must defer", "accept-wire-message-a2"), false,
  "a second direct query to one sid is rejected before it enters the outbox");
acceptanceRelay.setFocusedSid("accept-wire-b", "claude", "code");
acceptanceSocket.receive({
  type: "history", sid: "accept-wire-b", session_id: "accept-wire-b",
  revision: "accept-wire-history", generation: "accept-wire-generation",
  build_seq: 1, live_seq: 20, detail: "summary", events: [],
  turns: [{
    id: "accept-wire-native-old", prompt: "other session", blocks: [],
    done: true, detailEventCount: 0, detailLoaded: false,
  }],
  has_more: false, oldest_id: "accept-wire-native-old",
  newest_id: "accept-wire-native-old",
});
assert.equal(acceptanceRelay.sendQuery(
  "other session", "accept-wire-message-b"), true);
acceptanceSocket.receive({
  type: "command_ack",
  client_id: acceptanceFrameA.client_id,
  cmd_id: acceptanceFrameA.cmd_id,
});
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-a"),
  "accept-wire-message-a",
  "command ACK alone cannot release query acceptance",
);

(acceptanceRelay as unknown as { connect: () => void }).connect();
const acceptanceReconnectSocket = FakeWebSocket.instances.at(-1);
assert.ok(acceptanceReconnectSocket
  && acceptanceReconnectSocket !== acceptanceSocket);
acceptanceReconnectSocket.onopen?.();
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-a"),
  "accept-wire-message-a",
  "the ACKed latch survives automatic WebSocket reconnect",
);
assert.ok(acceptanceReconnectSocket.sent.some((raw) => {
  const frame = JSON.parse(raw);
  return frame.type === "query" && frame.msg_id === "accept-wire-message-b";
}), "the unacknowledged session-B query is replayed after reconnect");

acceptanceReconnectSocket.receive({
  type: "error", sid: "accept-wire-a", msg_id: "wrong-message",
  code: "busy", message: "wrong",
});
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-a"),
  "accept-wire-message-a",
);
acceptanceReconnectSocket.receive({
  type: "error", sid: "accept-wire-a", msg_id: "accept-wire-message-a",
  code: "busy", message: "rejected",
});
assert.equal(acceptanceRelay.pendingQueryFor("accept-wire-a"), null);
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-b"),
  "accept-wire-message-b",
  "session-A correlation cannot release session B",
);
acceptanceReconnectSocket.receive({
  type: "history", sid: "accept-wire-b", session_id: "accept-wire-b",
  revision: "accept-wire-history", generation: "accept-wire-generation",
  build_seq: 1, live_seq: 20, detail: "summary", events: [],
  turns: [{
    id: "accept-wire-native-old", prompt: "other session", blocks: [],
    done: true, detailEventCount: 0, detailLoaded: false,
  }],
  has_more: false, oldest_id: "accept-wire-native-old",
  newest_id: "accept-wire-native-old",
});
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-b"),
  "accept-wire-message-b",
  "a delayed old History with the same prompt is not query acceptance",
);
acceptanceReconnectSocket.receive({
  type: "history", sid: "accept-wire-b", session_id: "accept-wire-b",
  revision: "accept-wire-history", generation: "accept-wire-generation",
  build_seq: 2, live_seq: 21, detail: "summary", events: [],
  turns: [{
    id: "accept-wire-native-page", prompt: "other session", blocks: [],
    done: true, detailEventCount: 0, detailLoaded: false,
  }],
  before: "accept-wire-native-old", has_more: true,
  oldest_id: "accept-wire-native-page", newest_id: "accept-wire-native-page",
});
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-b"),
  "accept-wire-message-b",
  "an older pagination page cannot release query acceptance",
);
acceptanceReconnectSocket.receive({
  type: "history", sid: "accept-wire-b", session_id: "accept-wire-b",
  revision: "accept-wire-history", generation: "accept-wire-generation",
  build_seq: 2, live_seq: 21, detail: "summary", events: [],
  turns: [{
    id: "accept-wire-native-wrong", prompt: "unrelated", blocks: [],
    done: true, detailEventCount: 0, detailLoaded: false,
  }],
  has_more: false, oldest_id: "accept-wire-native-wrong",
  newest_id: "accept-wire-native-wrong",
});
assert.equal(
  acceptanceRelay.pendingQueryFor("accept-wire-b"),
  "accept-wire-message-b",
  "a different appended native turn cannot release query acceptance",
);
acceptanceReconnectSocket.receive({
  type: "history", sid: "accept-wire-b", session_id: "accept-wire-b",
  revision: "accept-wire-history", generation: "accept-wire-generation",
  build_seq: 3, live_seq: 22, detail: "summary", events: [],
  turns: [{
    id: "accept-wire-native-new", prompt: "other session", blocks: [],
    done: true, detailEventCount: 0, detailLoaded: false,
  }],
  has_more: false, oldest_id: "accept-wire-native-new",
  newest_id: "accept-wire-native-new",
});
assert.equal(acceptanceRelay.pendingQueryFor("accept-wire-b"), null,
  "a matching appended native History head recovers an echo missed during reconnect");
acceptanceRelay.stop();

// Deferred queries enter the reliable outbox and the wire immediately, but do
// not create a browser-owned acceptance latch. The wrapper, not a later idle
// React render, is responsible for starting them.
const deferredRelay = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
deferredRelay.start();
const deferredSocket = FakeWebSocket.instances.at(-1);
assert.ok(deferredSocket);
deferredSocket.onopen?.();
assert.equal(deferredRelay.sendDeferredQueryTo(
  "deferred-session",
  "run after the active turn",
  "deferred-message",
  "queue",
), true);
const deferredFrame = JSON.parse(deferredSocket.sent.at(-1) ?? "{}");
assert.equal(deferredFrame.type, "query");
assert.equal(deferredFrame.sid, "deferred-session");
assert.equal(deferredFrame.msg_id, "deferred-message");
assert.equal(deferredFrame.delivery, "queue");
assert.equal(typeof deferredFrame.cmd_id, "string");
assert.equal(typeof deferredFrame.client_id, "string");
assert.equal(deferredRelay.pendingQueryFor("deferred-session"), null,
  "wrapper-owned queues must not wait on a browser narrative latch");
const queuedDetailCommand = deferredRelay.sendGetQueuedQueryTo(
  "deferred-session", "deferred-message");
assert.equal(typeof queuedDetailCommand, "string");
const queuedDetailFrame = JSON.parse(deferredSocket.sent.at(-1) ?? "{}");
assert.equal(queuedDetailFrame.type, "get_queued_query");
assert.equal(queuedDetailFrame.sid, "deferred-session");
assert.equal(queuedDetailFrame.msg_id, "deferred-message");
assert.equal(queuedDetailFrame.cmd_id, queuedDetailCommand);
assert.equal(typeof queuedDetailFrame.client_id, "string");
const queuedUpdateCommand = deferredRelay.sendUpdateQueuedQueryTo(
  "deferred-session", "deferred-message", "edited while queued");
assert.equal(typeof queuedUpdateCommand, "string");
const queuedUpdateFrame = JSON.parse(deferredSocket.sent.at(-1) ?? "{}");
assert.equal(queuedUpdateFrame.type, "update_queued_query");
assert.equal(queuedUpdateFrame.sid, "deferred-session");
assert.equal(queuedUpdateFrame.msg_id, "deferred-message");
assert.equal(queuedUpdateFrame.prompt, "edited while queued");
assert.equal(queuedUpdateFrame.cmd_id, queuedUpdateCommand);
assert.equal(typeof queuedUpdateFrame.client_id, "string");
assert.equal(deferredRelay.sendCancelQueuedQueryTo(
  "deferred-session", "deferred-message"), true);
const cancelDeferredFrame = JSON.parse(deferredSocket.sent.at(-1) ?? "{}");
assert.equal(cancelDeferredFrame.type, "cancel_queued_query");
assert.equal(cancelDeferredFrame.sid, "deferred-session");
assert.equal(cancelDeferredFrame.msg_id, "deferred-message");
deferredRelay.stop();

// Steer uses the same reliable outbox and narrative acceptance barrier as a
// query, but always targets its explicit sid rather than the current focus.
const steerRelay = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
steerRelay.start();
const steerSocket = FakeWebSocket.instances.at(-1);
assert.ok(steerSocket);
steerSocket.onopen?.();
steerRelay.setSessionEngines([
  { session_id: "steer-target", engine: "codex", space: "code" },
  { session_id: "steer-focused", engine: "codex", space: "code" },
]);
steerRelay.setFocusedSid("steer-focused", "codex", "code");
const steerSentBeforeEmptyTarget = steerSocket.sent.length;
assert.equal(steerRelay.sendSteerTo(
  "", "must not follow focus", "steer-without-target"), false);
assert.equal(steerSocket.sent.length, steerSentBeforeEmptyTarget,
  "steer requires an explicit non-empty session id");
assert.equal(steerRelay.sendSteerTo(
  "steer-target",
  "change the active task",
  "steer-message",
  [{ media_type: "image/png", data: "steer-wire-image" }],
  [{ filename: "steer-wire.txt", data: "steer-wire-file" }],
), true);
const steerFrame = JSON.parse(steerSocket.sent.at(-1) ?? "{}");
assert.deepEqual({
  type: steerFrame.type,
  sid: steerFrame.sid,
  prompt: steerFrame.prompt,
  msg_id: steerFrame.msg_id,
  images: steerFrame.images,
  files: steerFrame.files,
}, {
  type: "steer",
  sid: "steer-target",
  prompt: "change the active task",
  msg_id: "steer-message",
  images: [{ media_type: "image/png", data: "steer-wire-image" }],
  files: [{ filename: "steer-wire.txt", data: "steer-wire-file" }],
}, "sendSteerTo serializes the explicit target and complete payload");
assert.equal(typeof steerFrame.client_id, "string");
assert.equal(typeof steerFrame.cmd_id, "string");
assert.equal(steerRelay.pendingQueryFor("steer-target"), "steer-message",
  "a sent steer remains protected until its narrative acceptance arrives");
assert.equal(steerRelay.pendingQueryFor("steer-focused"), null,
  "the focused runtime cannot inherit another session's steer latch");
assert.equal(steerRelay.sendSteerTo(
  "steer-target", "must wait", "steer-message-2"), false,
  "one target cannot queue a second direct steer before acceptance");

(steerRelay as unknown as { connect: () => void }).connect();
const steerReconnectSocket = FakeWebSocket.instances.at(-1);
assert.ok(steerReconnectSocket && steerReconnectSocket !== steerSocket);
steerReconnectSocket.onopen?.();
const steerReplayFrames = steerReconnectSocket.sent.map(
  (raw) => JSON.parse(raw) as Record<string, unknown>);
assert.deepEqual(
  steerReplayFrames.map((frame) => frame.type),
  ["hello", "switch_session", "steer", "switch_session"],
  "reconnect makes the steer target resident, replays its outbox frame, then restores focus",
);
assert.equal(steerReplayFrames[1].session_id, "steer-target");
assert.equal(steerReplayFrames[2].sid, "steer-target");
assert.equal(steerReplayFrames[2].cmd_id, steerFrame.cmd_id,
  "outbox replay preserves the steer command identity for server deduplication");
assert.equal(steerReplayFrames[3].session_id, "steer-focused");
assert.equal(steerRelay.pendingQueryFor("steer-target"), "steer-message",
  "the steer narrative latch survives automatic reconnect");

steerReconnectSocket.receive({
  type: "turn_steered",
  sid: "steer-focused",
  msg_id: "steer-message",
  turn_id: "wrong-native-task",
  prompt: "wrong target",
});
assert.equal(steerRelay.pendingQueryFor("steer-target"), "steer-message",
  "a TurnSteered from another sid cannot release the target's latch");
steerReconnectSocket.receive({
  type: "command_ack",
  client_id: steerFrame.client_id,
  cmd_id: steerFrame.cmd_id,
});
assert.equal(steerRelay.pendingQueryFor("steer-target"), "steer-message",
  "transport ACK removes the outbox frame but is not narrative steer acceptance");

(steerRelay as unknown as { connect: () => void }).connect();
const steerAckedReconnectSocket = FakeWebSocket.instances.at(-1);
assert.ok(steerAckedReconnectSocket
  && steerAckedReconnectSocket !== steerReconnectSocket);
steerAckedReconnectSocket.onopen?.();
assert.equal(
  steerAckedReconnectSocket.sent.some((raw) =>
    JSON.parse(raw).type === "steer"),
  false,
  "an ACKed steer is not replayed from the outbox on a later reconnect",
);
assert.equal(steerRelay.pendingQueryFor("steer-target"), "steer-message",
  "the ACKed narrative latch still survives that later reconnect");
steerAckedReconnectSocket.receive({
  type: "turn_steered",
  sid: "steer-target",
  msg_id: "steer-message",
  turn_id: "steer-native-task",
  prompt: "change the active task",
});
assert.equal(steerRelay.pendingQueryFor("steer-target"), null,
  "the exact TurnSteered echo is authoritative steer acceptance");
steerRelay.stop();

// A materialized steer alias is exact acceptance even on a cold socket with no
// frozen History baseline. The native rollout id remains distinct from the
// browser command id by design.
const aliasSteerRelay = new RelayWs({
  onEvent: () => {},
  onConnState: () => {},
});
aliasSteerRelay.start();
const aliasSteerSocket = FakeWebSocket.instances.at(-1);
assert.ok(aliasSteerSocket);
aliasSteerSocket.onopen?.();
aliasSteerRelay.setFocusedSid("alias-steer", "codex", "code");
assert.equal(aliasSteerRelay.sendSteerTo(
  "alias-steer", "recover by alias", "alias-steer-message"), true);
aliasSteerSocket.receive({
  type: "history", sid: "alias-steer", session_id: "alias-steer",
  revision: "alias-steer-revision", generation: "alias-steer-generation",
  build_seq: 1, live_seq: 1, detail: "summary", events: [],
  turns: [{
    id: "native-steer-row",
    clientMsgId: "alias-steer-message",
    prompt: "recover by alias",
    blocks: [],
    done: false,
    detailEventCount: 1,
    detailLoaded: false,
  }],
  has_more: false,
  oldest_id: "native-steer-row",
  newest_id: "native-steer-row",
});
assert.equal(aliasSteerRelay.pendingQueryFor("alias-steer"), null,
  "ConversationTurn.clientMsgId releases a cold steer acceptance latch");

assert.equal(aliasSteerRelay.sendSteerTo(
  "alias-steer", "recover from full events", "alias-event-message"), true);
aliasSteerSocket.receive({
  type: "history", sid: "alias-steer", session_id: "alias-steer",
  revision: "alias-steer-revision-2", generation: "alias-steer-generation",
  build_seq: 2, live_seq: 2, detail: "full",
  events: [{
    type: "user_msg",
    msg_id: "native-event-row",
    client_msg_id: "alias-event-message",
    prompt: "recover from full events",
  }],
  turns: [],
  has_more: false,
  oldest_id: "native-event-row",
  newest_id: "native-event-row",
});
assert.equal(aliasSteerRelay.pendingQueryFor("alias-steer"), null,
  "UserMsg.client_msg_id releases a full-history steer acceptance latch");
aliasSteerRelay.stop();

function testGeneratedImageCacheAliases() {
const generatedImagesCache = new HistoryImageAssetCache(2);
assert.equal(generatedImagesCache.begin({
  sid: "generated-session", turnId: "old-public-id", imageId: "img-native-hash",
  variant: "full", requestId: "generated-full", revision: "generated-r1",
}), true);
assert.equal(readyGeneratedImageAsset(
  generatedImagesCache.forSession("generated-session"), "img-native-hash"), undefined,
"an unfinished image request must not be aliased");
assert.equal(generatedImagesCache.accept({
  v: 52, ts: 1, type: "history_image", session_id: "generated-session",
  turn_id: "old-public-id", image_id: "img-native-hash", variant: "full",
  request_id: "generated-full", revision: "generated-r1",
  data: "original-bytes", media_type: "image/png", width: 1400, height: 900,
}), true);
assert.equal(readyGeneratedImageAsset(
  generatedImagesCache.forSession("generated-session"), "img-native-hash")?.data, "original-bytes");
assert.equal(readyGeneratedImageAsset(
  generatedImagesCache.forSession("another-session"), "img-native-hash"), undefined);
assert.equal(readyGeneratedImageAsset(
  generatedImagesCache.forSession("generated-session"), "another-image"), undefined);
assert.equal(readyGeneratedImageAsset({
  [historyImageAssetKey("old-public-id", "img-native-hash", "thumbnail")]: {
    status: "ready", data: "low-res", mediaType: "image/png",
  },
}, "img-native-hash"), undefined, "a thumbnail must not replace a full-width generated image");
generatedImagesCache.dropSession("generated-session");
assert.equal(readyGeneratedImageAsset(
  generatedImagesCache.forSession("generated-session"), "img-native-hash"), undefined,
"hard history invalidation must also drop the warm image alias");
}
testGeneratedImageCacheAliases();

console.log("web reliability tests passed");
