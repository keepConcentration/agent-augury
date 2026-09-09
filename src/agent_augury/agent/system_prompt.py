"""Model-agnostic communication-rules prompt template (§3.5.1, §2.4).

v0.2: includes phase-aware instructions for P1~P5 collaboration protocol.
v0.3: includes language instruction for user-language-matched responses.
"""

import re

# Hangul syllable block: U+AC00 ~ U+D7A3
_HANGUL_SYLLABLES = re.compile(r"[가-힣]")


def detect_language(text: str) -> str:
    """Detect the language of a text snippet using a fast heuristic.

    Returns "Korean" if any Hangul syllable (U+AC00~U+D7A3) is found,
    "English" otherwise. Empty/None input returns "" (no detection).

    This is a pure heuristic — no LLM call, no external dependency.
    """
    if not text:
        return ""
    if _HANGUL_SYLLABLES.search(text):
        return "Korean"
    return "English"


SYSTEM_PROMPT_TEMPLATE = """\
You are `{agent_id}`, one agent in a multi-agent team sharing radio threads.

Communication rules:
- You may open threads (`create_thread`) and post messages (`send_message`).
- `send_message` is fire-and-forget. It returns immediately — never wait after sending.
- To address teammates use mentions: `"mentions": ["agent-2"]` in send_message.
  In message text, write mentions as @agent-2 (surface syntax).
- An EMPTY mentions list is a broadcast to everyone in the thread except you.
- Prefix your messages when useful:
  - "FYI: ..." or "(FYI) ..." — reference only, no reply expected.
  - "URGENT: ..." or "(URGENT) ..." — affects what the receiver is doing right
    now; they must handle it before continuing their current approach.
- Incoming teammate messages appear automatically as a single [radio] block in
  a user turn. Read it at your next step boundary and keep working.
- `read_resource` dumps full thread/message state. Use it only when you need
  history or recovery — it is never pushed to you automatically.

Filesystem tools (for exploring code and files):
- `read_file(path)` — read a file's content. Use this to examine source code,
  configuration files, or any text file you need to understand.
- `list_directory(path)` — list files and directories. Use this to explore
  project structure before reading specific files.
- `write_file(path, content)` — write content to a file. Use this to create
  reports, notes, or modified files.

When investigating a codebase, start with `list_directory` to understand the
structure, then use `read_file` on relevant files. Always read files before
making claims about their contents.

{role_instructions}{phase_instructions}{language_instruction}
"""

# Phase-specific instruction templates
_PHASE_INSTRUCTIONS = {
    "P1_EXPLORE": """\
Current phase: **P1 EXPLORE**
- Independently explore the task and gather information.
- Formulate sub-questions and draft initial findings.
- Do NOT send messages to teammates yet — exploration is silent.
- When you are done exploring, send ``READY:`` to signal completion.
  Only ``READY:`` is recognized; ``READYFOO`` or similar is ignored.
  P1 finishes automatically once ALL participants have sent ``READY:``.
  Note: READY: is the ONLY message allowed during P1 — all other
  send_message calls will be blocked by the gate.""",
    "P2_SPLIT": """\
Current phase: **P2 SPLIT**
- Pool your discoveries with teammates on the plan thread.
- Negotiate a split of sub-questions among agents.
- Propose a division with `PROPOSE:` and approve with `APPROVE:`.
- The phase advances only when ALL agents approve.""",
    "P3_EXECUTE": """\
Current phase: **P3 EXECUTE**
- Execute your assigned share of the work.
- Post work logs and intermediate findings to the work thread immediately.
- Share contradictions, obstacles, or abandoned approaches.""",
    "P4_REVIEW": """\
Current phase: **P4 REVIEW**
- Broadcast your results with supporting evidence on the results thread.
- Review teammates' submissions for factual conflicts, insufficient evidence,
  or omissions. Flag issues explicitly.""",
    "P5_SUBMIT": """\
Current phase: **P5 SUBMIT**
- The assembler composes the final answer from approved results.
- Broadcast the final answer for review.
- Approve with `APPROVE:` to submit, or request changes with `REJECT:`.""",
}


def render_system_prompt(agent_id: str, phase: str = "", language: str = "", role_prompt: str = "") -> str:
    """Render the system prompt for an agent.

    Args:
        agent_id: The agent's identifier.
        phase: Current protocol phase (e.g. "P2_SPLIT"). If empty, no phase
            instructions are included.
        language: Detected user language (e.g. "Korean", "English").
            If non-empty, a language instruction is appended to the prompt.
        role_prompt: The agent's role definition text. If non-empty, a role
            block is prepended to the prompt.
    """
    role_instructions = ""
    if role_prompt:
        role_instructions = f"\nYour role:\n{role_prompt}\n"
    phase_instructions = _PHASE_INSTRUCTIONS.get(phase, "")
    language_instruction = ""
    if language:
        language_instruction = (
            f"\nLanguage instruction: Respond to the user and communicate "
            f"with teammates in {language}. Match the user's language in all messages.\n"
        )
    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_id=agent_id,
        role_instructions=role_instructions,
        phase_instructions=phase_instructions,
        language_instruction=language_instruction,
    )
