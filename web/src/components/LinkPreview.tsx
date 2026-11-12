import { lazy, Suspense, Fragment, useRef, type ReactNode } from "react";
import { useLinkPopover } from "../use-link-popover";
import { createPortal } from "react-dom";
import { previewUrl } from "../web-link-url";
import { Icon } from "../icons";

export function LocalFileLink({ location, children, onOpen, id }: {
  location: string; children: ReactNode; onOpen: () => void; id?: string;
}) {
  // Let a passing pointer leave before the path covers the conversation.
  const pop = useLinkPopover(500);
  const path = useRef<HTMLSpanElement>(null);
  return <>
    <span ref={pop.anchor} className="message-link-anchor">
      <button id={id} type="button" className="message-file-link"
        aria-label={`在 Remote 中打开 ${location}`} aria-describedby={pop.shown ? pop.id : undefined}
        onPointerEnter={pop.enter} onPointerLeave={pop.leave} onFocus={pop.focus}
        onBlur={pop.leave} onClick={() => { pop.close(); onOpen(); }}>{children}</button>
    </span>
    {pop.shown && createPortal(<div ref={pop.panel} id={pop.id} role="tooltip"
      className="message-file-tooltip" style={pop.position}
      onPointerEnter={pop.keep} onPointerLeave={pop.leave}>
      <span ref={path} className="message-file-tooltip-path" onDoubleClick={(event) => {
        event.preventDefault();
        const selection = window.getSelection();
        if (!path.current || !selection) return;
        const range = document.createRange(); range.selectNodeContents(path.current);
        selection.removeAllRanges(); selection.addRange(range);
      }}>{location.split(/(?<=\/)/).map((part, index) =>
        <Fragment key={index}>{part}<wbr /></Fragment>)}</span>
    </div>, pop.anchor.current?.closest("dialog[open]") ?? document.body)}
  </>;
}

const PreviewWebLink = lazy(() => import("./WebLinkPreview"));

export function WebLink({ href, children, title, id }: {
  href: string; children: ReactNode; title?: string; id?: string;
}) {
  const url = previewUrl(href);
  return url ? <PreviewableWebLink key={url.href} href={href} url={url} title={title} id={id}>{children}</PreviewableWebLink>
    : <a href={href} target="_blank" rel="noopener noreferrer" title={title} id={id}>{children}</a>;
}

function PreviewableWebLink({ href, url, children, title, id }: {
  href: string; url: URL; children: ReactNode; title?: string; id?: string;
}) {
  // Own hover before the rich card chunk has arrived. A fast first hover must
  // retire the previous path tip and survive the Suspense content replacement.
  const pop = useLinkPopover(180);
  const fallback = <><button type="button" className="message-link-icon" aria-label={`预览 ${url.hostname}`}
    onClick={() => pop.open(true)}><Icon name="globe" size={16} /></button>
    <a href={href} target="_blank" rel="noopener noreferrer" id={id}>{children}</a></>;
  return <span ref={pop.anchor} className="message-web-link" onPointerEnter={pop.enter} onPointerLeave={pop.leave}
    onFocus={pop.focus} onBlur={pop.leave}>
    <Suspense fallback={fallback}><PreviewWebLink pop={pop} href={href} url={url} title={title} id={id}>{children}</PreviewWebLink></Suspense>
  </span>;
}
