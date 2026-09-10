import type {
  ClaudeProfileInfo,
  CodexProfileInfo,
  Engine,
  SessionInfo,
  SessionList,
  Space,
  State,
} from "./protocol";

export interface NormalizedSessionList {
  sessions: SessionInfo[];
  claudeProfiles: ClaudeProfileInfo[];
  defaultClaudeProfileId: string | null;
  codexProfiles: CodexProfileInfo[];
  defaultCodexProfileId: string | null;
}

/** Apply one authoritative/partial-success catalog response exactly once.
 * Every consumer (React state, surface caches, RelayWs ownership maps) must use
 * this same projection or A→B→A can resurrect a list that dropped the rows of
 * one temporarily unavailable account. */
export function normalizeSessionList(
  previousSessions: readonly SessionInfo[],
  previousProfiles: readonly CodexProfileInfo[],
  previousDefaultProfileId: string | null,
  event: SessionList,
  previousClaudeProfiles: readonly ClaudeProfileInfo[] = [],
  previousDefaultClaudeProfileId: string | null = null,
): NormalizedSessionList {
  const codexProfiles = event.engine === "codex" && event.codex_profiles?.length
    ? [...event.codex_profiles]
    : [...previousProfiles];
  const claudeProfiles = event.engine === "claude" && event.claude_profiles?.length
    ? [...event.claude_profiles]
    : [...previousClaudeProfiles];
  const activeProfiles = event.engine === "codex"
    ? codexProfiles : claudeProfiles;
  const configuredProfileIds = new Set(
    activeProfiles.map((profile) => profile.id),
  );
  const unavailableProfileIds = new Set(
    activeProfiles
      .filter((profile) => !!profile.error)
      .map((profile) => profile.id),
  );
  const incomingIds = new Set(
    event.sessions.map((session) => session.session_id),
  );
  const listedSpace = event.space ?? "code";
  const retained = unavailableProfileIds.size === 0
    ? []
    : previousSessions.filter((session) =>
      !session.provisional_fork
      && session.engine === event.engine
      && (session.space ?? "code") === listedSpace
      && !!(event.engine === "codex"
        ? session.codex_profile_id : session.claude_profile_id)
      && configuredProfileIds.has(event.engine === "codex"
        ? session.codex_profile_id! : session.claude_profile_id!)
      && unavailableProfileIds.has(event.engine === "codex"
        ? session.codex_profile_id! : session.claude_profile_id!)
      && !incomingIds.has(session.session_id));
  return {
    sessions: [...event.sessions, ...retained],
    claudeProfiles,
    defaultClaudeProfileId: event.engine === "claude"
      ? event.default_claude_profile_id ?? previousDefaultClaudeProfileId
      : previousDefaultClaudeProfileId,
    codexProfiles,
    defaultCodexProfileId: event.engine === "codex"
      ? event.default_codex_profile_id ?? previousDefaultProfileId
      : previousDefaultProfileId,
  };
}

export function shouldAcceptSessionList(
  activeEngine: "claude" | "codex" | "dsh",
  activeSpace: Space,
  event: SessionList,
): boolean {
  return event.engine === activeEngine && (event.space ?? "code") === activeSpace;
}

/** Resolve the focus identity that one accepted catalog is allowed to
 * validate. During a surface switch React may still expose the previous
 * surface's focused row; that stale row must never invalidate the explicit
 * bookmark already claimed for the incoming engine/space. */
export function scopedFocusForSessionList(
  currentSid: string | null,
  currentSessions: readonly SessionInfo[],
  rememberedSid: string | null | undefined,
  engine: Engine,
  space: Space,
): string | null {
  const current = currentSid
    ? currentSessions.find((session) => session.session_id === currentSid)
    : undefined;
  const currentMatches = !!current
    && (current.engine ?? "claude") === engine
    && (current.space ?? "code") === space;
  return currentMatches ? currentSid : rememberedSid ?? null;
}

export function updateScopedSessionLifecycle(
  catalog: Readonly<Record<string, SessionInfo[]>>,
  engine: Engine,
  space: Space,
  sid: string,
  state: State,
): Record<string, SessionInfo[]> {
  const key = `${space}:${engine}`;
  const listed = catalog[key];
  if (!listed) return catalog as Record<string, SessionInfo[]>;
  let changed = false;
  const updated = listed.map((session) => {
    if (session.session_id !== sid || session.state === state) return session;
    changed = true;
    return { ...session, state };
  });
  return changed ? { ...catalog, [key]: updated }
    : catalog as Record<string, SessionInfo[]>;
}
