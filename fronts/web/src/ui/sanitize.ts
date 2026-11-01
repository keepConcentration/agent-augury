/** Approval-card text hygiene (09 §6, 10 M2): nothing may hide or forge a line.
 *
 * Ink's ``previewApprovalArgs`` replaces C0+DEL only. The Web is stricter:
 * C1 controls too, and invisible direction/width marks are not deleted but
 * *shown* (``⟨U+202E⟩``), so a reversed or padded command is visibly odd.
 */

const CONTROL = /[\u0000-\u001f\u007f-\u009f]/g;
const INVISIBLE = /[\u061c\u200b-\u200f\u2028-\u202e\u2060\u2066-\u2069\ufeff]/g;

/** Make every character in *text* visible on one line. */
export function visible(text: string): string {
  return text
    .replace(INVISIBLE, (c) => `⟨U+${c.charCodeAt(0).toString(16).toUpperCase().padStart(4, "0")}⟩`)
    .replace(CONTROL, "·");
}

/** Shorten *text* to *max* characters with a visible marker. */
export function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}… (+${text.length - max} chars)` : text;
}

/** One approval argument value as display text (strings as-is, others as JSON). */
export function valueText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value) ?? String(value);
}
