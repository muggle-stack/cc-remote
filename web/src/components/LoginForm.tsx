import { useState } from "react";
import { Icon, ClaudeMark } from "../icons";
import { useImeSubmit } from "../use-ime-submit";

export function LoginForm({
  onLogin, theme, onToggleTheme,
}: {
  onLogin: () => void;
  theme: string;
  onToggleTheme: () => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (value = password) => {
    if (!value) return;
    setError("");
    setLoading(true);
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: value }),
      });
      if (r.status === 429) { setError("尝试太频繁，等一分钟再试"); return; }
      if (!r.ok) { setError("密码错误"); return; }
      onLogin();
    } catch {
      setError("网络错误");
    } finally {
      setLoading(false);
    }
  };
  const imeSubmit = useImeSubmit<HTMLInputElement>((value) => { void submit(value); });

  return (
    <div className="login">
      <button className="iconbtn tt" onClick={onToggleTheme} aria-label="切换主题">
        <Icon name={theme === "dark" ? "sun" : "moon"} />
      </button>
      <div className="login-card">
        <div className="login-brand">
          <span className="brand-mark"><ClaudeMark size={30} /></span>
          <span className="name"><b>cc</b><span>·remote</span></span>
        </div>
        <p className="login-tag serif" style={{ fontSize: 15 }}>你的 Claude Code，随身遥控</p>
        <div className="login-field">
          <Icon name="lock" size={18} />
          <input
            ref={imeSubmit.inputRef}
            type="password"
            placeholder="访问密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onCompositionStart={imeSubmit.startComposition}
            onCompositionEnd={(e) => {
              imeSubmit.endComposition();
              setPassword(e.currentTarget.value);
            }}
            onKeyDown={(e) => {
              if (!imeSubmit.shouldSubmitKey({
                key: e.key,
                shiftKey: e.shiftKey,
                isComposing: e.nativeEvent.isComposing,
                keyCode: e.nativeEvent.keyCode,
              })) return;
              e.preventDefault();
              imeSubmit.requestSubmit();
            }}
            disabled={loading}
            autoFocus
          />
        </div>
        {error && <div className="login-err">{error}</div>}
        <button className="login-btn"
          onPointerDown={imeSubmit.commitCompositionBeforePointerSubmit}
          onClick={imeSubmit.requestSubmit} disabled={loading || !password}>
          {loading ? "登录中…" : "进入"}
        </button>
      </div>
    </div>
  );
}
