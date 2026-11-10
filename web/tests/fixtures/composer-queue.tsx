import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NewChatView } from "../../src/components/NewChatView";
import { Composer } from "../../src/components/Composer";
import { BtwPanel } from "../../src/components/BtwPanel";
import { ComposerDraftStore } from "../../src/composer-drafts";
import { createRuntime, type PendingQuery } from "../../src/reducer";
import type { State } from "../../src/protocol";
import "../../src/index.css";
import "../../src/App.css";

const params = new URLSearchParams(location.search);
const engine = params.get("engine") as "claude" | "codex" | "dsh" || "claude";
const side = params.has("side");
const work = params.has("work");
const fresh = params.has("new");
const profiles = [
  { id: "cc_remote_work", allowed: true },
  { id: ":read-only", allowed: true },
  { id: ":workspace", allowed: true },
  { id: ":danger-full-access", allowed: true },
];
const sid = "queue-fixture";
export function Fixture() {
  const [visible, setVisible] = useState(true);
  const [state, setState] = useState<State>("running");
  const [perm, setPerm] = useState(engine === "codex" ? "never" : work ? "acceptEdits" : "bypassPermissions");
  const [profile, setProfile] = useState<string | null>(work ? "cc_remote_work" : null);
  const [search, setSearch] = useState<"cached" | "live">("cached");
  const [queue, setQueue] = useState<PendingQuery[]>([]);
  const [events, setEvents] = useState<{ kind: string; prompt: string }[]>([]);
  const [drafts] = useState(() => {
    const store = new ComposerDraftStore();
    if (params.has("attachment")) store.set(sid, {
      input: "", images: [], pastes: [],
      files: [{ filename: "notes.txt", data: "aGk=" }],
    });
    return store;
  });
  const record = (kind: string, prompt = "") => {
    if (params.has("reject") && kind === "queue") return false;
    setEvents(current => [...current, { kind, prompt }]);
    return true;
  };
  const common = {
    draftKey: sid, draftStore: drafts, engine, catalog: {},
    unconfirmedQueued: [], unconfirmedReplaceable: [],
    queueCapacity: params.has("full") ? { maxCount: 0 } : {}, replaceQueueCapacity: {},
    onInterrupt: () => record("stop"),
    onEnqueue: (query: PendingQuery) => {
      if (!record("queue", query.prompt)) return false;
      setQueue(current => [...current, { ...query, msg_id: String(current.length), queueKind: "queue", queueState: "queued" }]);
      return true;
    },
    onSetPending: (query: PendingQuery) => record("replace", query.prompt),
    onRemoveQueued: (query: PendingQuery) => setQueue(current => current.filter(item => item !== query)),
    onInspectQueued: (query: PendingQuery) => record("inspect", query.prompt),
    onSetModel: () => record("model"), onSetEffort: () => {},
    onSetServiceTier: () => record("speed"), onSetAutoCompact: () => true,
  };
  const runtime = { ...createRuntime(), state, queue, model: "opus", effort: "high" };
  return <>
    <button data-testid="state" onClick={() => setState(state === "running" ? "idle" : "running")}>Toggle state</button>
    <output data-testid="events">{JSON.stringify(events)}</output>
    {fresh && <button data-testid="auth-remount" onClick={() => setVisible(value => !value)}>Toggle login</button>}
    {fresh ? visible && <NewChatView engine={engine} space={work ? "work" : "code"}
      draftKey={sid} draftStore={drafts}
      cwd="/tmp" controlScopeKey={`${engine}:${work}:new`} onPickCwd={() => {}}
      onGetPermissionProfiles={() => record("catalog")} permissionProfiles={profiles}
      onSend={(prompt, _images, _files, _collaboration, permission, permissionProfile, webSearch) =>
        record("create", JSON.stringify({ prompt, permission, permissionProfile, webSearch }))} />
      : side ? <BtwPanel {...common} sid={sid} rt={runtime}
      chats={[{ sid, engine, title: "Test", state, needsAnswer: false }]}
      active="btw" hasArtifact={false} onTab={() => {}} onNew={() => {}}
      onSelect={() => {}} onCloseChat={() => {}}
      onSend={prompt => record("send", prompt)} onSteer={prompt => record("steer", prompt)}
      onAnswerQuestion={() => {}} onCollapse={() => {}} onDismissNotice={() => {}} />
      : <Composer {...common} state={state} connState="connected" wrapperOnline
        queue={queue} pendingSend={null} failedDeferred={[]}
        surface={work ? "work" : undefined}
        model="opus" effort="high" perm={perm}
        permissionProfile={profile} permissionProfiles={profiles} webSearch={search}
        collaborationMode="default" editPrompt={null} onEditConsumed={() => {}}
        onSendQuery={prompt => record("send", prompt)} onSteerQuery={prompt => record("steer", prompt)}
        onSetPerm={value => { record("permission", value); if (!params.has("reject-control")) setPerm(value); }}
        onSetPermissionProfile={value => { record("profile", value); if (!params.has("reject-control")) setProfile(value); }}
        onGetPermissionProfiles={() => record("catalog")}
        onSetWebSearch={value => { record("search", value); setSearch(value); }} onSetCollaborationMode={() => {}}
        onClear={() => {}} onContext={() => {}} contextReport={null}
        skills={[{ kind: "skill", id: "review", name: "review", description: "Review changes", enabled: true }]}
      />}
  </>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
