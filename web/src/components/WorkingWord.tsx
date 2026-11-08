import { useEffect, useState, type CSSProperties } from "react";
import { WORKING_WORDS } from "../working-words";

const WORD_INTERVAL_MS = 8000;
const SHIMMER_PERIOD_MS = 3000;
const MAX_REMEMBERED_WORDS = 128;

interface RememberedWord { word: string; shimmerStarted: number }
interface WordLayer { word: string; phase: string }

// Presentation only. Focus changes and virtual-row remounts keep the last word;
// this bounded cache never changes a session's native lifecycle or transcript.
const remembered = new Map<string, RememberedWord>();

function randomWord(previous?: string): string {
  const previousIndex = previous ? WORKING_WORDS.indexOf(previous) : -1;
  let index = Math.floor(Math.random() * (WORKING_WORDS.length - (previousIndex >= 0 ? 1 : 0)));
  if (previousIndex >= 0 && index >= previousIndex) index++;
  return WORKING_WORDS[index];
}

function wordForScope(scope: string): RememberedWord {
  const entry = remembered.get(scope) ?? { word: randomWord(), shimmerStarted: Date.now() };
  remembered.delete(scope);
  remembered.set(scope, entry);
  while (remembered.size > MAX_REMEMBERED_WORDS) remembered.delete(remembered.keys().next().value!);
  return entry;
}

function wordLayer(entry: RememberedWord): WordLayer {
  return { word: entry.word, phase: `${-((Date.now() - entry.shimmerStarted) % SHIMMER_PERIOD_MS)}ms` };
}

function RotatingWord({ scope, label }: { scope: string; label: string }) {
  const [entry] = useState(() => wordForScope(scope));
  const [layers, setLayers] = useState(() => [wordLayer(entry)]);
  useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    let transition: ReturnType<typeof setTimeout> | undefined;
    const timer = window.setInterval(() => {
      if (document.hidden || reduced?.matches) return;
      entry.word = randomWord(entry.word);
      const next = wordLayer(entry);
      setLayers(current => [current[current.length - 1], next]);
      transition = setTimeout(() => setLayers([next]), 250);
    }, WORD_INTERVAL_MS);
    return () => { window.clearInterval(timer); clearTimeout(transition); };
  }, [entry]);
  return <span className="working-word">
    <span className="working-word-label">{label}</span>
    {layers.map((layer, index) => <span key={layer.word} aria-hidden="true"
      className={`working-word-layer ${index === layers.length - 1 ? "is-current" : "is-leaving"}`}
      style={{ "--working-word-phase": layer.phase } as CSSProperties}>
      {layer.word}...
    </span>)}
  </span>;
}

export function WorkingWord({ scope, label }: { scope: string; label: string }) {
  return <RotatingWord key={scope} scope={scope} label={label} />;
}
