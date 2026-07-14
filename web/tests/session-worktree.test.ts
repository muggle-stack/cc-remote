import assert from "node:assert/strict";
import type { SessionInfo } from "../src/protocol.ts";
import { makeForkSessionCommand, makeForkSessionWorktreeCommand } from "../src/protocol.ts";
import {
  canForkTurn,
  isWorktreeForkNameValid,
  isWorktreeForkBlockedByState,
  isTerminalWorktreeForkError,
  matchesSessionForkRequest,
  matchesWorktreeForkRequest,
  normalizeWorktreeForkName,
  sessionMenuCapabilities,
  WORKTREE_FORK_NAME_MAX,
} from "../src/session-worktree.ts";

const codex: SessionInfo = { session_id: "codex-parent", engine: "codex" };
const claude: SessionInfo = { session_id: "claude-parent", engine: "claude" };
const archivedCodex: SessionInfo = { session_id: "codex-archived", engine: "codex", tag: "archived" };

assert.deepEqual(sessionMenuCapabilities(codex), {
  rename: true,
  archive: true,
  forkWorktree: true,
});
assert.deepEqual(sessionMenuCapabilities(claude), {
  rename: true,
  archive: true,
  forkWorktree: false,
});
assert.equal(sessionMenuCapabilities(archivedCodex).forkWorktree, false);
assert.equal(isWorktreeForkBlockedByState("running"), true);
assert.equal(isWorktreeForkBlockedByState("interrupting"), true);
assert.equal(isWorktreeForkBlockedByState("idle"), false);

assert.equal(normalizeWorktreeForkName("  fix-login  "), "fix-login");
assert.equal(isWorktreeForkNameValid(""), false);
assert.equal(isWorktreeForkNameValid("   "), false);
assert.equal(isWorktreeForkNameValid("x".repeat(WORKTREE_FORK_NAME_MAX)), true);
assert.equal(isWorktreeForkNameValid("x".repeat(WORKTREE_FORK_NAME_MAX + 1)), false);

assert.deepEqual(
  makeForkSessionCommand("codex-parent", "turn-7", "request-message", 122),
  {
    v: 9,
    type: "fork_session",
    session_id: "codex-parent",
    request_id: "request-message",
    last_turn_id: "turn-7",
    ts: 122,
  },
);

assert.deepEqual(
  makeForkSessionWorktreeCommand("codex-parent", "fix-login", "request-1", 123),
  {
    v: 9,
    type: "fork_session_worktree",
    session_id: "codex-parent",
    name: "fix-login",
    request_id: "request-1",
    ts: 123,
  },
);
assert.equal(
  makeForkSessionWorktreeCommand(
    "codex-parent", "fix-login", "request-1", 123, "turn-7").last_turn_id,
  "turn-7",
);

const pending = { requestId: "request-1", parentSessionId: "codex-parent" };
assert.equal(matchesWorktreeForkRequest(pending, "request-1", "codex-parent"), true);
assert.equal(matchesWorktreeForkRequest(pending, "request-2", "codex-parent"), false);
assert.equal(matchesWorktreeForkRequest(pending, "request-1", "other-parent"), false);
assert.equal(matchesWorktreeForkRequest(pending, "request-1"), true);
assert.equal(matchesWorktreeForkRequest(null, "request-1"), false);
const pendingMessage = {
  requestId: "request-message",
  parentSessionId: "codex-parent",
  forkPointId: "turn-7",
  engine: "codex" as const,
};
assert.equal(matchesSessionForkRequest(
  pendingMessage, "request-message", "codex-parent", "turn-7"), true);
assert.equal(matchesSessionForkRequest(
  pendingMessage, "request-message", "codex-parent", "turn-other"), false);
assert.equal(matchesSessionForkRequest(
  pendingMessage, "request-message", "codex-parent"), true);
assert.equal(matchesSessionForkRequest(
  pendingMessage, "request-other", "codex-parent", "turn-7"), false);
assert.equal(canForkTurn(
  "codex", { done: true, forkPointId: "turn-7" }), true);
assert.equal(canForkTurn(
  "codex", { done: false, forkPointId: "turn-7" }), false);
assert.equal(canForkTurn(
  "codex", { done: true }), false);
assert.equal(canForkTurn(
  "claude", { done: true, forkPointId: "assistant-uuid" }), true);
assert.equal(canForkTurn(
  "claude", { done: true }), false);
assert.equal(isTerminalWorktreeForkError("wrapper_offline"), false);
assert.equal(isTerminalWorktreeForkError("fork_reconciling"), false);
assert.equal(isTerminalWorktreeForkError("internal"), true);

console.log("session worktree tests passed");
