/** A reading block's content coordinate, independent of native scroll motion.
 * Keep this separate from follow-output and explicit history/detail anchors. */
interface ReadingBlock {
  node: HTMLElement;
  /** Block top in scroll-content coordinates. */
  top: number;
  answer?: AnswerReadingAnchor;
}

interface AnswerReadingAnchor {
  turnId: string;
  messageIds: string[];
  tag: string;
  text: string;
}

export interface ReadingPosition extends ReadingBlock {
  scope: string;
  /** Other visible blocks can survive an automatic process collapse. */
  fallbacks: ReadingBlock[];
  expandedProcesses: HTMLElement[];
}

// The outer media node survives pending -> ready. Anchoring its source <pre>
// loses the reading position when Mermaid replaces that node with an SVG.
const READING_BLOCKS = ".prose :is(p,li,pre,table,h1,h2,h3,h4,h5,h6),.mermaid-block,.generated-output,.message-image-frame,.user-bubble,.turn-process-head";

const readingText = (node: HTMLElement) => (node.textContent ?? "").trimStart().slice(0, 160);
const messageIds = (answer: HTMLElement) =>
  [answer.dataset.messageId, answer.dataset.streamMessageId].filter((id): id is string => !!id);

function captureAnswerAnchor(node: HTMLElement): AnswerReadingAnchor | undefined {
  const answer = node.closest<HTMLElement>(".assistant-answer-segment");
  const turnId = answer?.closest<HTMLElement>("[data-turn-id]")?.dataset.turnId;
  const text = readingText(node);
  if (!answer || !turnId || !text) return;
  return { turnId, messageIds: messageIds(answer), tag: node.tagName, text };
}

/** A Markdown/tree replacement can detach every stored DOM node. Resolve only
 * inside the same native/stream message and turn, never by similar prose in
 * another answer. Ambiguous repeated paragraphs deliberately do not match. */
function resolveAnswerAnchor(viewport: HTMLElement, anchor?: AnswerReadingAnchor): HTMLElement | undefined {
  if (!anchor) return;
  const turn = [...viewport.querySelectorAll<HTMLElement>("[data-turn-id]")]
    .find(node => node.dataset.turnId === anchor.turnId);
  if (!turn) return;
  const answers = [...turn.querySelectorAll<HTMLElement>(".assistant-answer-segment")]
    .filter(node => messageIds(node).some(id => anchor.messageIds.includes(id)));
  if (answers.length !== 1) return;
  const candidates = [...answers[0].querySelectorAll<HTMLElement>(READING_BLOCKS)]
    .filter(node => node.tagName === anchor.tag && readingText(node) === anchor.text);
  return candidates.length === 1 ? candidates[0] : undefined;
}

export function captureReadingPosition(viewport: HTMLElement, scope: string): ReadingPosition | null {
  const top = viewport.getBoundingClientRect().top;
  const bottom = top + viewport.clientHeight;
  const firstVisible = (nodes: NodeListOf<HTMLElement>) => {
    for (const node of nodes) {
      const rect = node.getBoundingClientRect();
      if (rect.height > 0 && rect.bottom > top && rect.top < bottom) return node;
    }
  };
  const turn = firstVisible(viewport.querySelectorAll<HTMLElement>("[data-turn-id]"));
  if (!turn) return null;
  // Only inspect blocks in a visible turn. A whole-turn anchor cannot keep
  // a paragraph in place when text earlier in that same answer reflows.
  const visible: ReadingBlock[] = [];
  for (const node of turn.querySelectorAll<HTMLElement>(READING_BLOCKS)) {
    const rect = node.getBoundingClientRect();
    if (rect.height <= 0) continue;
    if (rect.top >= bottom) break;
    if (rect.bottom > top) visible.push({
      node, top: rect.top - top + viewport.scrollTop, answer: captureAnswerAnchor(node),
    });
  }
  const primary = visible[0]
    ?? { node: turn, top: turn.getBoundingClientRect().top - top + viewport.scrollTop };
  const answer = visible.find(block => block.node.closest(".assistant-answer-segment"));
  return { scope, ...primary, fallbacks: visible.filter(block => block !== primary),
    expandedProcesses: answer
      ? [...turn.querySelectorAll<HTMLElement>(".turn-process.open")].filter(process =>
          process.getBoundingClientRect().bottom - top + viewport.scrollTop <= answer.top)
      : [],
  };
}

/** Returns only layout displacement. Adding scrollTop cancels any wheel/key
 * movement since capture, so correcting this delta cannot undo that movement. */
export function readingPositionDelta(
  viewport: HTMLElement, position: ReadingPosition,
): number | null {
  const viewportTop = viewport.getBoundingClientRect().top;
  const retained = [position, ...position.fallbacks].flatMap(block => {
    const node = viewport.contains(block.node) ? block.node : resolveAnswerAnchor(viewport, block.answer);
    if (!node) return [];
    const rect = node.getBoundingClientRect();
    if (!rect.height) return [];
    return [{ node, previousTop: block.top, answer: captureAnswerAnchor(node),
      top: rect.top - viewportTop + viewport.scrollTop }];
  });
  const collapsed = position.expandedProcesses.some(process =>
    viewport.contains(process) && process.dataset.processAutoCollapsed === "true");
  position.expandedProcesses = position.expandedProcesses.filter(process =>
    viewport.contains(process) && process.classList.contains("open"));
  // Only automatic collapse transfers the anchor to the visible answer.
  // Manual toggles/detail loading still retain the first visible block.
  // A surviving process header cannot anchor the answer below a closed body.
  const primary = (collapsed
    ? retained.find(block => block.node.closest(".assistant-answer-segment")) : undefined)
    ?? retained[0];
  if (!primary) return null;
  const delta = primary.top - primary.previousTop;
  position.node = primary.node;
  position.top = primary.top;
  position.answer = primary.answer;
  position.fallbacks = retained.filter(block => block !== primary).map(({ node, top, answer }) => ({ node, top, answer }));
  return delta;
}

/** A semantic bookmark survives unmount; raw scrollTop alone does not survive
 * virtual-row estimates, a resized sidebar, or new output above the reader. */
export function captureReadingBookmark(viewport: HTMLElement) {
  const reading = captureReadingPosition(viewport, "");
  const turn = reading?.node.closest<HTMLElement>("[data-turn-id]");
  if (!reading || !turn?.dataset.turnId) return null;
  const top = viewport.getBoundingClientRect().top;
  return {
    turnId: turn.dataset.turnId,
    blockIndex: [...turn.querySelectorAll(READING_BLOCKS)].indexOf(reading.node),
    offset: reading.node.getBoundingClientRect().top - top,
    turnOffset: turn.getBoundingClientRect().top - top,
    scrollTop: viewport.scrollTop,
  };
}

export function bookmarkedReadingNode(turn: HTMLElement, blockIndex: number): HTMLElement {
  return turn.querySelectorAll<HTMLElement>(READING_BLOCKS)[blockIndex] ?? turn;
}
