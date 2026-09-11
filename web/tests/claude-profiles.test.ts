import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  skillCatalogKey,
} from "../src/skill-catalog-cache.ts";


const personalSkillKey = skillCatalogKey(
  "machine-a", "claude", "code", "/repo", null, "personal");
const companySkillKey = skillCatalogKey(
  "machine-a", "claude", "code", "/repo", null, "company");
assert.notEqual(
  personalSkillKey,
  companySkillKey,
  "Claude Skill catalogs must remain inside their CLAUDE_CONFIG_DIR lane",
);

const harness = await createServer({
  root: process.cwd(),
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, watch: null },
});
try {
  const {
    newChatCatalogRequest,
    NewChatView,
  } = await harness.ssrLoadModule("/src/components/NewChatView.tsx");
  const {
    newWorkProfileForSidebarFilter,
    workProfileDisplayName,
  } = await harness.ssrLoadModule("/src/work-profile-selection.ts");
  const {
    SessionsSidebar,
  } = await harness.ssrLoadModule("/src/components/SessionsSidebar.tsx");
  const {
    initialState,
    modelCatalogScopeKey,
    reduce,
  } = await harness.ssrLoadModule("/src/reducer.ts");

  assert.deepEqual(
    newChatCatalogRequest(
      "claude", "code", "/repo", undefined, "company"),
    { engine: "claude", cwd: "/repo", claudeProfileId: "company" },
  );
  assert.equal(
    newChatCatalogRequest(
      "claude", "work", "/stale", undefined, "company"),
    null,
    "Claude Work must not probe settings through a stale Code cwd",
  );
  assert.equal(
    newWorkProfileForSidebarFilter("claude", "work", "company"),
    "company",
  );

  const profiles = [
    { id: "personal", label: "Personal" },
    { id: "company", label: "Company" },
  ];
  assert.equal(
    workProfileDisplayName([profiles[0]], "personal", "personal"),
    "Personal",
    "one configured account must use its label, not look removed",
  );
  assert.equal(
    workProfileDisplayName(profiles, "personal", "company"),
    "nyx · Company",
  );
  assert.equal(
    workProfileDisplayName(profiles, "personal", "retired"),
    "账号已移除",
  );
  const newChatMarkup = renderToStaticMarkup(createElement(NewChatView, {
    cwd: "~",
    engine: "claude",
    controlScopeKey: "machine-a:code:claude\0company",
    model: null,
    effort: null,
    claudeProfiles: profiles,
    defaultClaudeProfileId: "personal",
    claudeProfileId: "company",
    onPickClaudeProfile: () => {},
    onPickModel: () => {},
    onPickEffort: () => {},
    onPickCwd: () => {},
    onSend: () => true,
  }));
  assert.match(newChatMarkup, /aria-label="选择 Claude 账号"/);
  assert.doesNotMatch(newChatMarkup, /<select/,
    "the account trigger opens our shared picker instead of a native select");
  assert.match(newChatMarkup, />nyx · Company</);

  const sidebarMarkup = renderToStaticMarkup(createElement(SessionsSidebar, {
    open: true,
    engine: "claude",
    space: "code",
    profileScopeKey: "machine-a:claude:code",
    claudeProfiles: profiles,
    defaultClaudeProfileId: "personal",
    sessions: [
      {
        session_id: "personal@native-a",
        native_session_id: "native-a",
        claude_profile_id: "personal",
        claude_profile_label: "Personal",
        summary: "Personal",
        state: "idle",
        engine: "claude",
        space: "code",
      },
      {
        session_id: "company@native-b",
        native_session_id: "native-b",
        claude_profile_id: "company",
        claude_profile_label: "Company",
        summary: "Company",
        state: "idle",
        engine: "claude",
        space: "code",
      },
    ],
    liveStates: {},
    completionBadges: {},
    activeSessionId: null,
    onSpaceChange: () => {},
    onSelect: () => {},
    onNew: () => {},
    onNewInDir: () => {},
    onClose: () => {},
    onRename: () => {},
    onArchive: () => {},
    onPin: () => {},
    onDelete: () => {},
    onForkWorktree: () => {},
  }));
  assert.match(sidebarMarkup, /aria-label="筛选 Claude 账号"/);
  assert.match(sidebarMarkup,
    /class="scard-profile-ribbon tone-0"[^>]*>default</);
  assert.match(sidebarMarkup,
    /class="scard-profile-ribbon tone-1"[^>]*>nyx</);
  assert.match(sidebarMarkup, /Claude 账号：nyx · Company/);

  const ownership = {
    scopeKey: "machine-a:code:claude",
    machineId: "machine-a",
    engine: "claude",
    space: "code",
    surfaceEpoch: 1,
    connectionGeneration: 1,
    claudeProfileId: "company",
  };
  let state = reduce({
    ...initialState,
    newChat: {
      cwd: "/repo",
      cwdSource: "explicit",
      model: null,
      effort: null,
      claudeProfileId: "company",
    },
  }, {
    type: "event",
    ownership,
    event: {
      v: 42,
      ts: 1,
      type: "session_list",
      engine: "claude",
      space: "code",
      claude_profiles: profiles,
      default_claude_profile_id: "personal",
      sessions: [{
        session_id: "company@same-native-id",
        native_session_id: "same-native-id",
        claude_profile_id: "company",
        claude_profile_label: "Company",
        engine: "claude",
        space: "code",
      }],
    },
  });
  assert.equal(
    state.claudeProfileByScope[ownership.scopeKey],
    "company",
  );
  assert.equal(state.newChat.claudeProfileId, "company");

  const temporary = reduce(state, {
    type: "event",
    ownership,
    event: {
      v: 42,
      ts: 2,
      type: "session_focus",
      session_id: "tmp-company",
      cwd: "/repo",
      request_id: "create-company",
    },
  });
  const temporaryRow = temporary.sessions.find(
    (session: { session_id: string }) => session.session_id === "tmp-company",
  );
  assert.equal(temporaryRow?.claude_profile_id, "company");
  const rekeyed = reduce(temporary, {
    type: "event",
    ownership,
    event: {
      v: 42,
      ts: 3,
      type: "session_rekey",
      old_key: "tmp-company",
      session_id: "company@captured-native",
      cwd: "/repo",
    },
  });
  const rekeyedRow = rekeyed.sessions.find(
    (session: { session_id: string }) => (
      session.session_id === "company@captured-native"
    ),
  );
  assert.equal(rekeyedRow?.claude_profile_id, "company");
  assert.equal(rekeyedRow?.native_session_id, "captured-native");

  for (const [profileId, modelId] of [
    ["personal", "personal-claude"],
    ["company", "company-claude"],
  ]) {
    state = reduce(state, {
      type: "event",
      event: {
        v: 42,
        ts: 2,
        type: "models",
        engine: "claude",
        claude_profile_id: profileId,
        cwd: "/repo",
        models: [{
          id: modelId,
          display_name: modelId,
          description: "",
          efforts: [],
          default_effort: null,
        }],
      },
    });
  }
  assert.equal(
    state.catalog[modelCatalogScopeKey("claude", "personal")][0].id,
    "personal-claude",
  );
  assert.equal(
    state.catalog[modelCatalogScopeKey("claude", "company")][0].id,
    "company-claude",
  );
} finally {
  await harness.close();
}

console.log("Claude profile tests passed");
