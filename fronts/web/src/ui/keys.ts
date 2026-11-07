/** Keyboard rules (09 §5, N-012), as pure functions. */

export type EscContext = {
  dialogOpen: boolean;
  confirming: boolean;
  /** Ctrl+C is not used on the Web (CP-7); Esc interrupts only when nothing else claims it. */
  canInterrupt: boolean;
  composing: boolean;
  inComposer: boolean;
};

export type EscAction = "close-dialog" | "cancel-confirm" | "interrupt" | null;

export function escAction(c: EscContext): EscAction {
  if (c.composing) {
    return null; // IME candidate window: Esc belongs to the input method
  }
  if (c.dialogOpen) {
    return "close-dialog";
  }
  if (c.confirming) {
    return "cancel-confirm";
  }
  return c.canInterrupt && !c.inComposer ? "interrupt" : null;
}

/** Alt+Shift+A → jump to the card. By ``code``: on macOS ``key`` is "Å". */
export function isCardShortcut(e: {altKey: boolean; shiftKey: boolean; ctrlKey: boolean; metaKey: boolean; code: string}): boolean {
  return e.altKey && e.shiftKey && !e.ctrlKey && !e.metaKey && e.code === "KeyA";
}

/** A question-panel shortcut (1–9, s): never with Ctrl/Cmd/Alt (Cmd+S is "save") or a held key. */
export function isPanelKey(e: {ctrlKey: boolean; metaKey: boolean; altKey: boolean; repeat: boolean}): boolean {
  return !(e.ctrlKey || e.metaKey || e.altKey || e.repeat);
}
