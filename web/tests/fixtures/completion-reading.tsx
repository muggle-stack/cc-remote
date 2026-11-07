import { useState } from "react";
import { ChatView } from "../../src/components/ChatView";
import { createRuntime, initialState, reduce, type Turn } from "../../src/reducer";
import { PROTOCOL_VERSION, type ServerEvent } from "../../src/protocol";

export function CompletionReadingFixture() {
  const [done, setDone] = useState(false);
  const [extra, setExtra] = useState(0);
  const [refreshed, setRefreshed] = useState(false);
  const params = new URLSearchParams(location.search);
  const shortProcess = params.has("short-process");
  const background = params.has("background");
  const longAnswer = params.has("long-answer");
  const turns: Turn[] = [{
    id: "previous", prompt: "Earlier question", done: true,
    blocks: [{ kind: "text", message_id: "previous-answer", done: true,
      text: Array.from({ length: 20 }, (_, i) => `Earlier answer paragraph ${i}.`).join("\n\n") }],
  }, {
    id: "answering", prompt: "Explain the implementation", done,
    responseProcesses: background && refreshed ? [{
      id: "native-response", answerId: "native-answer", background: true, itemCount: 1,
    }] : undefined,
    blocks: [
      { kind: "text", message_id: "thinking", channel: "thinking", done: true, background,
        text: Array.from({ length: shortProcess ? 1 : longAnswer ? 60 : 30 }, (_, i) => `Thinking paragraph ${i}. Checking the implementation before answering.`).join("\n\n") },
      { kind: "text", message_id: "answer", channel: "final", done, background,
        text: Array.from({ length: longAnswer ? 40 : 20 }, (_, i) => `Answer paragraph ${i}. This is the explanation the user is reading.`).join("\n\n")
          + "\n\nFinal additional paragraph.\n\n".repeat(extra * 8) },
    ],
  }];
  // The assembled Claude record binds both stream envelopes to native UUIDs.
  // Completion without this identity handoff misses the real remount race.
  const sid = "completion-reading";
  let state = { ...initialState, runtimes: { [sid]: { ...createRuntime(), turns } } };
  if (done) for (const [message_id, channel] of [["thinking", "thinking"], ["answer", "final"]]) {
    state = reduce(state, { type: "event", event: {
      v: PROTOCOL_VERSION, ts: 1, sid, type: "assistant_msg_end",
      turn_id: "answering", message_id, canonical_message_id: `native-${message_id}`, channel, background,
    } as ServerEvent });
  }
  return <main style={{ display: "flex", flexDirection: "column", height: "100dvh" }}>
    <header style={{ height: 48, flex: "none" }}>Reading a streaming answer</header>
    <ChatView sid={sid} engine="claude" turns={state.runtimes[sid].turns}
      historyRevision="stable" activeTurnId={done ? null : "answering"} />
    <footer style={{ height: 48, flex: "none" }}>
      <button data-testid="append-answer" onClick={() => setExtra(value => value + 1)}>Append answer</button>
      <button data-testid="complete-answer" onClick={() => setDone(true)}>Complete answer</button>
      <button data-testid="refresh-answer" onClick={() => setRefreshed(true)}>Refresh summary</button>
    </footer>
  </main>;
}
