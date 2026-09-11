import type { Turn } from "../domain/conversation";
import { Icon } from "../icons";
import "./DshFeatures.css";

export default function DshProducedFiles({ turn, onOpen }: { turn: Turn; onOpen: (path: string) => void }) {
  const paths = new Set<string>();
  for (const block of turn.blocks) {
    if (block.kind !== "tool" || !block.result || block.result.is_error) continue;
    const input = block.input;
    const path = ["write", "edit"].includes(block.tool) ? input.file_path
      : block.tool === "str_replace_editor" && ["create", "str_replace", "insert"].includes(String(input.command)) ? input.path : null;
    if (typeof path === "string" && path.trim()) paths.add(path);
    if (block.tool === "present" && Array.isArray(input.files)) for (const file of input.files) {
      if (file && typeof file.path === "string") paths.add(file.path);
    }
  }
  if (!paths.size) return null;
  return <div className="dsh-produced" aria-label="本轮产出文件">{[...paths].slice(-32).map(path => <button key={path} title={path} onClick={() => onOpen(path)}>
    <Icon name="read" size={13} />{path.split("/").at(-1) || path}
  </button>)}</div>;
}
