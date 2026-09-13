/** Parse human `@agent-id` surface syntax into Wire ``mentions``. */

export type ParsedMentions = {
  /** Agent ids to put on ``human.send`` / ``human.answer`` (empty = broadcast). */
  mentions: string[];
  /** `@token` values that did not match the known roster (when roster was provided). */
  unknown: string[];
};

/**
 * Extract ``@id`` tokens for directed human → agent delivery.
 *
 * Matching rules (agent surface parity):
 * - ``@`` must start a token (start of string or after whitespace / brackets)
 * - id chars: letters, digits, ``_``, ``-`` (e.g. ``@agent-2``, ``@a1``)
 * - does not treat ``user@email.com`` as a mention
 *
 * When ``knownAgents`` is non-empty, only roster members are returned in
 * ``mentions``; other ``@tokens`` land in ``unknown``.
 * When the roster is empty/unknown, every well-formed ``@id`` is kept.
 */
export function parseHumanMentions(
  text: string,
  knownAgents: readonly string[] = [],
): ParsedMentions {
  const re = /(?:^|[\s([{])@([A-Za-z][A-Za-z0-9_-]*)/g;
  const found: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const id = m[1];
    if (id && !found.includes(id)) {
      found.push(id);
    }
  }

  if (found.length === 0) {
    return {mentions: [], unknown: []};
  }

  if (knownAgents.length === 0) {
    return {mentions: found, unknown: []};
  }

  const roster = new Set(knownAgents);
  const mentions = found.filter((id) => roster.has(id));
  const unknown = found.filter((id) => !roster.has(id));
  return {mentions, unknown};
}
