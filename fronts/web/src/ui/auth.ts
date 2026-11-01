/** OAuth device-code link in an auth notice (09 B-1). */

/** Hosts whose verification URL may be clickable (auth/oauth.py:45). */
export const AUTH_HOSTS: ReadonlySet<string> = new Set(["portal.nousresearch.com"]);

export type AuthLink = {before: string; url: string; host: string; after: string};

/**
 * The first https URL in *text*, if its real host is allowlisted. The notice
 * comes from child stderr, which tool output can influence — anything else
 * stays plain text.
 */
export function authLink(text: string): AuthLink | null {
  const match = /(https:\/\/\S+)/.exec(text);
  if (!match) {
    return null;
  }
  let host: string;
  try {
    host = new URL(match[1]).hostname;
  } catch {
    return null;
  }
  if (!AUTH_HOSTS.has(host)) {
    return null;
  }
  return {before: text.slice(0, match.index), url: match[1], host, after: text.slice(match.index + match[1].length)};
}
