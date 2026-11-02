import { useContext, useState } from "react";
import { viewerScopeKey, type ViewerScope, type ViewerSelection } from "../remote-viewer";
import { RightPanelPresentation } from "../right-panel-presentation";
import { RemoteViewerPanel } from "./RemoteViewerPanel";
import { ViewerPagesProvider } from "./ViewerPagesProvider";

interface Entry { key: string; scope: ViewerScope; requestedUrl?: string; openId?: string; used: number }
interface Props {
  machineId: string;
  activeScope: ViewerScope | null;
  selections: Record<string, ViewerSelection | null>;
  requested?: { key: string; href: string; openId: string } | null;
  devices: { machine_id: string; label: string }[];
  onSelect: (key: string, selection: ViewerSelection | null) => void;
  onClose: (key: string) => void;
}

/** Keep DOM parents and frame identities stable across surface switches. Only
 * visited panels live here (at most three); persisted descriptors aren't frames.
 * Closing, changing devices or unmounting the authenticated app releases leases.
 */
export function RetainedViewerPanels({ machineId, activeScope, selections, requested, devices, onSelect, onClose }: Props) {
  const presentation = useContext(RightPanelPresentation);
  const [state, setState] = useState<{ entries: Entry[]; activeKey: string | null; clock: number }>(
    { entries: [], activeKey: null, clock: 0 });
  const retained = state.entries;
  const key = activeScope ? viewerScopeKey(activeScope) : null;
  const clock = state.clock + (key !== state.activeKey ? 1 : 0);
  let entries = retained.filter(entry => entry.scope.machineId === machineId && Object.hasOwn(selections, entry.key));
  if (activeScope && key && Object.hasOwn(selections, key)) {
    const previous = entries.find(entry => entry.key === key);
    const request = requested?.key === key ? requested : undefined;
    const sameRequest = !request || previous?.openId === request.openId;
    const entry = previous && sameRequest && previous.used === clock ? previous
      : { key, scope: activeScope, requestedUrl: sameRequest ? previous?.requestedUrl : request?.href,
        openId: sameRequest ? previous?.openId : request?.openId, used: clock };
    // Never sort/move retained DOM nodes: moving an iframe also reloads it.
    entries = previous ? entries.map(item => item.key === key ? entry : item) : [...entries, entry];
    if (entries.length > 3) {
      const oldest = entries.reduce((old, item) => item.used < old.used ? item : old);
      entries = entries.filter(item => item !== oldest);
    }
  }
  if (key !== state.activeKey || entries.length !== retained.length || entries.some((entry, index) => entry !== retained[index]))
    setState({ entries, activeKey: key, clock });
  return entries.map(entry => {
    const active = entry.key === key;
    return <div key={entry.key} style={{ display: active ? "contents" : "none" }} inert={!active}>
      <RightPanelPresentation.Provider value={active ? presentation : { ...presentation, expanded: false, covered: true }}>
        <ViewerPagesProvider scope={entry.scope} onOpen={page => onSelect(entry.key,
          { machine_id: page.machine_id, site_id: page.site_id, entry: page.entry })}>
          <RemoteViewerPanel key={entry.openId ?? ""} active={active} scope={entry.scope}
            selection={selections[entry.key]} requestedUrl={entry.requestedUrl} devices={devices}
            onSelect={selection => onSelect(entry.key, selection)} onClose={() => onClose(entry.key)} />
        </ViewerPagesProvider>
      </RightPanelPresentation.Provider>
    </div>;
  });
}
