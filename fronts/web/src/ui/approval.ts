/** Approval-card timing (09 §3.4, 10 M1). All [가정] values, measured later (AS-8). */

import {valueText} from "./sanitize.js";

/** Buttons stay locked this long after a card appears or its content changes. */
export const APPEAR_LOCK_MS = 600;
/** The confirm button stays locked this long, so a double click cannot pass. */
export const CONFIRM_LOCK_MS = 300;
/** Remaining time at or below this shows in red. */
export const TTL_WARN_S = 10;
/** Values longer than this are folded until "전체 보기". */
export const FOLD_CHARS = 300;

/** Seconds left on an approval, or null when it has no TTL (F-009). Never negative. */
export function ttlLeft(item: {ttl_seconds: unknown; received_at: unknown}, now: number): number | null {
  if (typeof item.ttl_seconds !== "number" || typeof item.received_at !== "number") {
    return null;
  }
  return Math.max(0, Math.ceil((item.received_at + item.ttl_seconds * 1000 - now) / 1000));
}

export function formatLeft(seconds: number): string {
  const m = Math.floor(seconds / 60);
  return `${String(m).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Names (agent, tool, arg keys) longer than this are clipped on the card. */
export const LABEL_CHARS = 64;

/** The marker the BFF's ``clipArgs`` leaves on a value it shortened (bff/state.ts). */
const CLIPPED = /… \(\+\d+ chars\)$/;

/** Arg keys whose value is folded and not yet opened: Approve waits for them. */
export function foldedKeys(args: Record<string, unknown>, opened: ReadonlySet<string>): string[] {
  return Object.keys(args).filter((k) => valueText(args[k]).length > FOLD_CHARS && !opened.has(k));
}

/** True when part of the request can never be seen on the card (BFF clip or a clipped name). */
export function isTruncated(a: {agent_id: string; tool: string; args_preview: Record<string, unknown>}): boolean {
  return (
    a.agent_id.length > LABEL_CHARS ||
    a.tool.length > LABEL_CHARS ||
    Object.entries(a.args_preview).some(([k, v]) => k.length > LABEL_CHARS || CLIPPED.test(valueText(v)))
  );
}
