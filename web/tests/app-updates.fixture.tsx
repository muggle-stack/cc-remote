import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import "../src/App.css";
import { HeaderMenu } from "../src/components/HeaderMenu";
import AppUpdateSheet from "../src/components/AppUpdateSheet";
import { useAppUpdates } from "../src/use-app-updates";
import { useTheme } from "../src/use-theme";
import { useBoldText } from "../src/use-bold-text";
import { newerVersion } from "../src/app-updates";
const noop = () => {};
export function Fixture() {
  const [open, setOpen] = useState(false);
  const [machine, setMachine] = useState("device-a");
  const [draft, setDraft] = useState("");
  const appearance = useTheme("codex");
  const { boldText, setBoldText } = useBoldText();
  const updates = useAppUpdates(true, machine, open);
  return <div className="app"><section className="pane code-pane">
    <header className="c-head"><div className="titlewrap"><div className="ttl">Code</div>
      <div className="sub">cc-remote · {machine}</div></div><span className="hstat idle">idle</span>
      <HeaderMenu engine="codex" appearance={appearance} boldText={boldText} onBoldText={setBoldText}
        notificationMode="off" notificationBinding="off" notificationAvailable={false}
        onNotificationMode={async () => false} onOpenUsageActivity={noop} onLogout={noop}
        onOpenUpdates={() => setOpen(true)} currentVersion="5.3.14"
        updateAvailable={newerVersion(updates.data?.latest?.version, updates.data?.current)} />
    </header>
    <main style={{ padding: 28, flex: 1 }}><p>更新会在任务结束后安装。</p>
      <button onClick={() => { setOpen(false); setMachine(m => m === "device-a" ? "device-b" : "device-a"); }}>切换测试设备</button>
    </main>
    <textarea aria-label="输入消息" value={draft} onChange={event => setDraft(event.target.value)}
      style={{ margin: 24, padding: 20, borderRadius: 24 }} />
    {open && <AppUpdateSheet updates={updates} deviceName={machine} onClose={() => setOpen(false)}
      webUpdate={{ version: null, updating: false, message: null, reload: async () => {} }} />}
  </section></div>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
