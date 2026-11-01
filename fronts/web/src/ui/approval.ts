/** Approval-card timing (09 §3.4, 10 M1). All [가정] values, measured later (AS-8). */

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
