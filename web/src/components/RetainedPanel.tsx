import { useContext, useState, type ReactNode } from "react";
import { PanelVisible, RightPanelPresentation } from "../right-panel-presentation";

interface Entry { key: string; content: ReactNode; used: number }

/** Navigation parks the mounted view. A null child in the same scope closes it.
 * Keep insertion order stable: moving an iframe reloads its document. */
export function RetainedPanel({ scope, children, retain }: {
  scope: string | null;
  children: ReactNode;
  retain: (scope: string) => boolean;
}) {
  const presentation = useContext(RightPanelPresentation);
  const [state, setState] = useState<{ entries: Entry[]; scope: string | null; clock: number }>({
    entries: [], scope: null, clock: 0,
  });
  const clock = state.clock + (scope !== state.scope ? 1 : 0);
  let entries = state.entries;
  if (entries.some(entry => entry.key !== scope && !retain(entry.key))) {
    entries = entries.filter(entry => entry.key === scope || retain(entry.key));
  }
  if (scope) {
    const previous = entries.find(entry => entry.key === scope);
    if (!children && previous) entries = entries.filter(entry => entry.key !== scope);
    else if (children && !previous) entries = [...entries, { key: scope, content: children, used: clock }];
    else if (children && previous && (previous.content !== children || previous.used !== clock)) {
      entries = entries.map(entry => entry.key === scope ? { key: scope, content: children, used: clock } : entry);
    }
  }
  if (entries.length > 3) {
    const oldest = entries.reduce((prior, entry) => entry.used < prior.used ? entry : prior);
    entries = entries.filter(entry => entry !== oldest);
  }
  if (entries !== state.entries || scope !== state.scope) setState({ entries, scope, clock });
  return entries.map(entry => {
    const visible = entry.key === scope && !!children;
    return <PanelVisible.Provider key={entry.key} value={visible}>
      <RightPanelPresentation.Provider value={visible ? presentation : {
        ...presentation, expanded: false, covered: true,
      }}>
        <div style={{ display: visible ? "contents" : "none" }} inert={!visible}>
          {entry.content}
        </div>
      </RightPanelPresentation.Provider>
    </PanelVisible.Provider>;
  });
}
