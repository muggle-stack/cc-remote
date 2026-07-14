export interface SubmitKeyEvent {
  key: string;
  shiftKey: boolean;
  isComposing?: boolean;
  keyCode?: number;
}

/**
 * An IME confirmation key is not a request to submit the surrounding form.
 * WebKit can report isComposing=false at the composition boundary while still
 * exposing the legacy 229 keyCode, so both signals are required.
 */
export function shouldSubmitTextKey(event: SubmitKeyEvent): boolean {
  return event.key === "Enter"
    && !event.shiftKey
    && event.isComposing !== true
    && event.keyCode !== 229;
}

/** Coordinate keyboard and pointer submit paths around an active IME session. */
export class ImeSubmitGuard {
  private composing = false;

  startComposition(): void {
    this.composing = true;
  }

  endComposition(): void {
    this.composing = false;
  }

  shouldSubmitKey(event: SubmitKeyEvent): boolean {
    return !this.composing && shouldSubmitTextKey(event);
  }

  /** Whether a button pointer-down should blur to commit the active IME first. */
  shouldCommitBeforeButtonSubmit(): boolean {
    return this.composing;
  }
}
