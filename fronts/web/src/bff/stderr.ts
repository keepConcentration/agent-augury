/** Gateway child stderr (WBS 2.8, SRS §3.5.3).
 *
 * Ink drops the OAuth device-code lines (``gateway.ts``); the Web shows them,
 * because a browser user has no terminal to read them from. Everything else is
 * logged and, unless it is known noise, surfaced as a warning notice.
 */

import type {Notice} from "./sse.js";
import {maskSensitive} from "./wire.js";

// session_stdio.py:76-77 and auth/oauth.py:196,201 print these before the
// gateway is bound (later ones arrive as Wire ``log`` events instead).
const AUTH_PREFIXES = ["To authenticate, enter code:", "Verification URL:"];
// core/agent/policy.py: a startup hint, not a failure.
const QUIET = ["tools.shell.enabled=true with empty"];

/** What to do with one stderr line: a notice for the tabs, or null (log only). */
export function classifyStderr(line: string): Notice | null {
  const text = line.trim();
  if (AUTH_PREFIXES.some((p) => text.startsWith(p))) {
    return {kind: "auth", level: "info", text};
  }
  if (QUIET.some((q) => text.includes(q))) {
    return null;
  }
  return {kind: "stderr", level: "warn", text};
}

/** Log every line (masked) and publish what the tabs should see. */
export function handleStderrLine(line: string, sinks: {notice: (n: Notice) => void; log: (line: string) => void}): void {
  if (!line.trim()) {
    return;
  }
  sinks.log(`[gateway] ${maskSensitive(line)}`);
  const notice = classifyStderr(line);
  if (notice) {
    sinks.notice(notice); // publishNotice masks and clips again
  }
}
