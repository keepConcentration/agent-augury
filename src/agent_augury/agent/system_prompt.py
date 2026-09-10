"""Model-agnostic communication-rules prompt template (§3.5.1, §2.4).

v0.2: includes phase-aware instructions for P1~P5 collaboration protocol.
v0.3: includes language instruction for user-language-matched responses.
v0.7 (AGENT_TOOLS_EXPANSION_DESIGN.md v4.1): tool instructions are rendered
dynamically from the active tool specs (P6) — only enabled tools are described.
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

{tool_instructions}{role_instructions}{human_instructions}{phase_instructions}{language_instruction}
"""

# Dynamic tool-instruction sections (P6 — rendered from active ToolBox specs).
# Each section is included only when the corresponding tools are enabled.

_FILESYSTEM_INSTRUCTIONS = """\
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
"""

_SHELL_INSTRUCTIONS = """\
Shell tool:
- `run_command(command, timeout?)` — run a command asynchronously (no shell
  interpreter; argv parsing via shlex). Returns stdout, stderr, exit code.
  Output is truncated. Destructive commands (rm -rf, mkfs, sudo, reboot, ...)
  are blocked. Use for git, pytest, builds, and other CLI tools.
"""

_WEB_INSTRUCTIONS = """\
Web tools:
- `fetch_url(url)` — fetch an HTTP(S) URL and return its text content
  (HTML stripped, truncated). Private/link-local IPs and blocked redirect
  targets are rejected (SSRF protection).
- `web_search(query)` — search the web; returns title/url/snippet (limited).
  Use `fetch_url` to read the full pages of promising results.
"""

_EDIT_INSTRUCTIONS = """\
File edit tools:
- `edit_file(path, old_string, new_string)` — replace the FIRST exact
  occurrence of `old_string` with `new_string` (must occur exactly once).
- `append_file(path, content)` — append content to the end of a file.
"""


def render_tool_instructions(tool_specs: list[dict]) -> str:
    """Render the dynamic tool block (P6) from active tool specs.

    Only enabled tools are described; disabled tools are never mentioned
    (context economy + avoids model confusion). Returns "" when the specs
    list contains no known tools (defensive).
    """
    names = {spec.get("name") for spec in tool_specs}
    sections: list[str] = []

    if {"read_file", "list_directory", "write_file"} & names:
        sections.append(_FILESYSTEM_INSTRUCTIONS)
    if "run_command" in names:
        sections.append(_SHELL_INSTRUCTIONS)
    if {"fetch_url", "web_search"} & names:
        sections.append(_WEB_INSTRUCTIONS)
    if {"edit_file", "append_file"} & names:
        sections.append(_EDIT_INSTRUCTIONS)

    return "\n".join(sections).rstrip("\n")


_HUMAN_INSTRUCTIONS = """\
Human-in-the-loop rules:
- `human` (the user) is a member of this team.
- Use `ask_user` to ask questions or request confirmation. It is
  fire-and-forget — keep working; the reply arrives later as [radio].
- If the task is ambiguous or an important decision (submission, file
  write, direction change) is coming, ask the user FIRST.
- User replies appear as `from human: ...` in [radio] blocks.
- Match the user's language when asking.
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


def render_system_prompt(
    agent_id: str,
    phase: str = "",
    language: str = "",
    role_prompt: str = "",
    has_human: bool = False,
    tool_instructions: str = "",
) -> str:
    """Render the system prompt for an agent.

    Args:
        agent_id: The agent's identifier.
        phase: Current protocol phase (e.g. "P2_SPLIT"). If empty, no phase
            instructions are included.
        language: Detected user language (e.g. "Korean", "English").
            If non-empty, a language instruction is appended to the prompt.
        role_prompt: The agent's role definition text. If non-empty, a role
            block is prepended to the prompt.
        has_human: When True, human-in-the-loop rules are included so the
            agent knows how to ask the user via ``ask_user``.
        tool_instructions: Dynamically rendered tool block (P6). Empty when
            no tools are active (defensive).
    """
    role_instructions = ""
    if role_prompt:
        role_instructions = f"\nYour role:\n{role_prompt}\n"
    human_instructions = _HUMAN_INSTRUCTIONS if has_human else ""
    phase_instructions = _PHASE_INSTRUCTIONS.get(phase, "")
    language_instruction = ""
    if language:
        language_instruction = (
            f"\nLanguage instruction: Respond to the user and communicate "
            f"with teammates in {language}. Match the user's language in all messages.\n"
        )
    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_id=agent_id,
        tool_instructions=tool_instructions,
        role_instructions=role_instructions,
        human_instructions=human_instructions,
        phase_instructions=phase_instructions,
        language_instruction=language_instruction,
    )
