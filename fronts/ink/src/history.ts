/** Shell-style ↑/↓ recall for the input line.
 *
 * Pure state in, pure state out — the component owns the React wiring, this
 * owns the bookkeeping that is easy to get subtly wrong (bounds, and getting
 * the half-typed draft back when you walk forward off the end).
 */

export type History = {
  /** Submitted lines, oldest first. */
  entries: string[];
  /** Entry being shown; ``entries.length`` means the live draft is shown. */
  index: number;
  /** What the human had typed before they started browsing. */
  draft: string;
};

export const emptyHistory: History = {entries: [], index: 0, draft: ""};

/** Record a submitted line and drop back to the live draft. */
export function remember(history: History, line: string): History {
  const trimmed = line.trim();
  // Blanks and an immediate repeat are noise, same as a shell.
  const entries =
    !trimmed || trimmed === history.entries[history.entries.length - 1]
      ? history.entries
      : [...history.entries, trimmed];
  return {entries, index: entries.length, draft: ""};
}

/** Step back (-1) or forward (+1). Null when there is nowhere to go. */
export function recall(
  history: History,
  step: -1 | 1,
  current: string,
): {history: History; value: string} | null {
  const browsing = history.index < history.entries.length;
  // Entering history: whatever is on the line right now is the draft to return
  // to. Already browsing: keep the draft we saved on the way in.
  const draft = browsing ? history.draft : current;
  const next = Math.min(
    history.entries.length,
    Math.max(0, history.index + step),
  );
  if (next === history.index) {
    return null;
  }
  const value = next < history.entries.length ? history.entries[next]! : draft;
  return {history: {...history, index: next, draft}, value};
}
