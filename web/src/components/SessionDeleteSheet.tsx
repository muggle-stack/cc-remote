import { useRef, useState } from "react";
import { CenteredSheet } from "./CenteredSheet";
import { Icon } from "../icons";
import "./SessionDeleteSheet.css";

export default function SessionDeleteSheet({ title, cwd, work, onClose, onConfirm }: {
  title: string;
  cwd: string;
  work: boolean;
  onClose: () => void;
  onConfirm: () => boolean;
}) {
  const submitted = useRef(false);
  const [error, setError] = useState(false);
  const noun = work ? "工作" : "会话";
  const confirm = () => {
    if (submitted.current) return;
    submitted.current = true;
    if (!onConfirm()) {
      submitted.current = false;
      setError(true);
    }
  };
  return <CenteredSheet open viewport label={work ? "删除这项工作？" : "删除这条会话？"} onClose={onClose}
    maxWidth={440} minimumHeight={240} className="session-delete-sheet">
    <div className="session-delete-body">
      <div className="session-delete-target"><Icon name="trash" size={20} /><div>
        <b>{title}</b>
        {!work && cwd && <small>{cwd}</small>}
      </div></div>
      <p>{work ? "这项工作的会话历史和私有文件将被永久删除。"
        : "这条会话的历史将被永久删除，项目目录和代码文件会保留。"}</p>
      <p className="session-delete-note">删除后无法恢复。</p>
      {error && <p role="alert" className="session-delete-error">删除请求未发送，请检查连接后重试。</p>}
      <div className="session-delete-buttons">
        <button onClick={onClose}>取消</button>
        <button className="session-delete-danger" onClick={confirm}>删除{noun}</button>
      </div>
    </div>
  </CenteredSheet>;
}
