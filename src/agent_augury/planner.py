"""Team planner — one LLM call turns a task into an agent roster.

TEAM_PLANNER_DESIGN.md: the user registers providers + one planner model;
per task, the planner picks agent count (1–10), roles, and provider/model
pairs from the live model catalog.  Output is plain ``agents[]`` entries for
the session YAML — the runtime itself is unchanged.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from .backend.base import ModelBackend
from .core.server import RESERVED_NAMES
from .model_listing import ModelInfo, is_general_purpose_model_id

MAX_AGENTS = 10
# ponytail: flat cap keeps the prompt bounded (~40 chars/model → 400 ≈ 16k
# chars). Nous lists ~230 today; rank/filter by capability if lists outgrow it.
MAX_MODELS_PER_PROVIDER = 400

# provider name → model id → pricing row (None when the provider gives ids only)
Catalog = dict[str, dict[str, ModelInfo | None]]


def unavailable_reason(name: str, spec: dict[str, Any]) -> str | None:
    """Why *spec* has no usable credentials in this process, or None (§4.1).

    OAuth is checked after :func:`ensure_oauth` already refreshed / signed in,
    so a missing token here means that sign-in failed.
    """
    if spec.get("type") == "nous_oauth":
        from .wizard import _has_valid_oauth_token

        return None if _has_valid_oauth_token() else "not signed in"
    env = spec.get("api_key_env")
    if env and os.environ.get(env):
        return None
    return f"API key env {env} is not set" if env else "no API key env configured"


async def ensure_oauth(
    providers: dict[str, dict[str, Any]],
    notify: Callable[[str], None],
    on_user_code: Callable[[str, str], None] | None,
) -> None:
    """Sign in to OAuth providers exactly as a session backend would.

    ``NousPortalOAuthBackend.get_access_token`` reuses a valid token, else
    refreshes, else runs the browser device-code flow (the wizard's flow).
    Failures are reported with their cause instead of a bare "not
    authenticated" (live 2026-09-29: an expired token whose refresh failed).
    """
    from .backend.nous_portal_oauth import NousPortalOAuthBackend

    for name, spec in providers.items():
        if spec.get("type") != "nous_oauth":
            continue
        url = {"base_url": spec["base_url"]} if spec.get("base_url") else {}
        backend = NousPortalOAuthBackend(model="", on_user_code=on_user_code, **url)
        try:
            await backend.get_access_token()
        except Exception as exc:  # noqa: BLE001 — report, skip this provider
            notify(
                f"  (skipping provider {name!r} — sign-in failed: "
                f"{type(exc).__name__}: {exc}. Try again, or run "
                "`agent-augury --reconfigure` to sign in from the wizard)"
            )


def build_catalog(
    providers: dict[str, dict[str, Any]],
    notify: Callable[[str], None] = print,
) -> Catalog:
    """List models for every available provider; skip the rest with a note.

    *notify* receives the skip notes (Ink passes a Wire ``log`` emitter —
    stdout there is JSONL and must not see bare prints).
    """
    from .wizard import _try_list_models

    catalog: Catalog = {}
    for name, spec in providers.items():
        reason = unavailable_reason(name, spec)
        if reason is not None:
            notify(f"  (skipping provider {name!r} — {reason})")
            continue
        models = _try_list_models(name, spec.get("base_url", ""), spec.get("api_key_env"))
        rows: dict[str, ModelInfo | None] = {}
        for m in models or []:
            if isinstance(m, ModelInfo):
                rows[m.id] = m
            elif is_general_purpose_model_id(m):
                rows[m] = None
            if len(rows) >= MAX_MODELS_PER_PROVIDER:
                break
        if not rows:
            notify(f"  (skipping provider {name!r} — model list unavailable)")
            continue
        catalog[name] = rows
    return catalog


def build_messages(task: str, catalog: Catalog) -> list[dict[str, Any]]:
    """Planner prompt: task + models grouped under ``provider:`` headers (§4.2).

    Grouped, not ``provider/model`` lines: model ids contain ``/`` themselves
    (``openai/gpt-5.2-codex``) and a flat join made the split ambiguous.
    """
    lines = []
    for provider, rows in catalog.items():
        lines.append(f"provider: {provider}")
        for model_id, info in rows.items():
            price = info.price_suffix() if info else None
            lines.append(f"  - {model_id}" + (f"  {price}" if price else ""))
    system = (
        "You design a team of LLM agents that will collaborate on a task.\n"
        f"Use the MINIMUM number of agents that can do the task well (1-{MAX_AGENTS}). "
        "Do not add agents just to fill slots; one agent is fine for small tasks.\n"
        "Pick each agent's model ONLY from the list below. Set \"provider\" to the "
        "provider header and \"model\" to the model id exactly as listed under it "
        "(model ids may contain '/'; copy them whole, do not add the provider).\n"
        "Every agent works by calling file/web tools and follows a propose → execute → "
        "review → submit protocol, so each one needs a model that handles tool calls reliably.\n"
        "The agent that writes or submits the final deliverable is a CORE role, never a "
        "supporting one. Where prices are shown, give core roles strong models; use cheaper "
        "models only for narrow helper roles, and avoid mini/nano/flash-class models for any "
        "agent that must read code or produce the deliverable.\n"
        "Write each role in the task's language: what the agent owns and how it works with the others.\n\n"
        "Available models:\n" + "\n".join(lines) + "\n\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"agents": [{"id": "short-kebab-id", "provider": "<provider header>", '
        '"model": "<model id as listed>", "role": "...", "reason": "why this model"}]}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]


def resolve_model(provider: Any, model: Any, catalog: Catalog) -> tuple[str, str] | None:
    """Map the planner's (provider, model) onto a catalog entry, or None.

    Model ids contain ``/``, so planners mis-split: ``provider="nous_oauth/openai",
    model="gpt-5.2-codex"`` or ``model="nous_oauth/openai/gpt-5.2-codex"``.
    Candidates come from ``provider/model`` and ``model`` alone, peeling one
    known-provider prefix at a time; the first that names a listed model under
    that provider wins (checked per step, so a model id's own ``openai/``
    is not peeled when an ``openai`` provider is also registered). Last
    resort: the id is listed under exactly one provider.
    """
    p = str(provider or "").strip().strip("/")
    m = str(model or "").strip().strip("/")
    candidates: list[tuple[str | None, str]] = []
    for path in (f"{p}/{m}" if p else m, m):
        hint: str | None = None
        while True:
            candidates.append((hint, path))
            prefix = next((n for n in catalog if path.startswith(n + "/")), None)
            if prefix is None:
                break
            hint, path = hint or prefix, path[len(prefix) + 1 :]
    for hint, path in candidates:
        if hint is not None and path in catalog[hint]:
            return hint, path
    for _hint, path in candidates:
        owners = [name for name, rows in catalog.items() if path in rows]
        if len(owners) == 1:
            return owners[0], path
    return None


def _suggest(model: Any, catalog: Catalog) -> str:
    """`` (did you mean: …)`` from the closest listed ids, for the retry prompt."""
    import difflib

    tail = str(model or "").rsplit("/", 1)[-1]
    ids = [m for rows in catalog.values() for m in rows]
    close = difflib.get_close_matches(tail, [m.rsplit("/", 1)[-1] for m in ids], n=3)
    picks = [m for m in ids if m.rsplit("/", 1)[-1] in close][:3]
    return f" (did you mean: {', '.join(picks)})" if picks else ""


def parse_plan(text: str | None, catalog: Catalog) -> list[dict[str, Any]]:
    """Validate the planner reply (§4.3). Raises ``ValueError`` with the reason."""
    if not text or "{" not in text or "}" not in text:
        raise ValueError("reply contains no JSON object")
    try:
        data = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, list) or not 1 <= len(agents) <= MAX_AGENTS:
        raise ValueError(f"'agents' must be a list of 1-{MAX_AGENTS} entries")

    seen: set[str] = set()
    plan: list[dict[str, Any]] = []
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise ValueError(f"agents[{i}] must be an object")  # noqa: TRY004 — one retryable error type
        agent_id = str(a.get("id") or "").strip()
        key = agent_id.lower()
        if not agent_id:
            raise ValueError(f"agents[{i}].id is empty")
        if key in RESERVED_NAMES:
            raise ValueError(f"agents[{i}].id {agent_id!r} is reserved")
        if key in seen:
            raise ValueError(f"agents[{i}].id {agent_id!r} is duplicated")
        seen.add(key)
        resolved = resolve_model(a.get("provider"), a.get("model"), catalog)
        if resolved is None:
            raise ValueError(
                f"agents[{i}] provider={a.get('provider')!r} model={a.get('model')!r} "
                f"is not in the list{_suggest(a.get('model'), catalog)}"
            )
        provider, model = resolved
        role = a.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"agents[{i}].role is empty")
        plan.append(
            {
                "id": agent_id,
                "provider": provider,
                "model": model,
                "role": role.strip(),
                "reason": str(a.get("reason") or "").strip(),
            }
        )
    return plan


async def plan_team(
    task: str,
    planner_backend: ModelBackend,
    catalog: Catalog,
    history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Ask the planner; on a bad reply retry once with the reason attached.

    *history* carries earlier proposals + user feedback (``revision_messages``).
    """
    messages = [*build_messages(task, catalog), *(history or [])]
    for attempt in range(2):
        completion = await planner_backend.complete(messages, [])
        if completion.error is not None:
            raise RuntimeError(f"planner call failed: {completion.error}")
        try:
            return parse_plan(completion.text, catalog)
        except ValueError as exc:
            if attempt == 1:
                raise ValueError(f"planner reply rejected: {exc}") from exc
            messages = [
                *messages,
                {"role": "assistant", "content": completion.text or ""},
                {"role": "user", "content": f"Invalid reply: {exc}. Reply with corrected JSON only."},
            ]
    raise AssertionError("unreachable")


def format_plan(plan: list[dict[str, Any]], catalog: Catalog) -> str:
    """Confirmation table (§5)."""
    id_w = max(len(a["id"]) for a in plan)
    names = [f"{a['provider']}/{a['model']}" for a in plan]
    name_w = max(len(n) for n in names)
    out = [f"Proposed team ({len(plan)} agent{'s' if len(plan) > 1 else ''}):"]
    for a, name in zip(plan, names, strict=True):
        info = catalog[a["provider"]][a["model"]]
        price = (info.price_suffix() if info else None) or ""
        out.append(f"  {a['id']:<{id_w}}  {name:<{name_w}}  {price}  {a['reason']}".rstrip())
    return "\n".join(out)


def to_session_agents(
    plan: list[dict[str, Any]], providers: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Plan rows → session YAML ``agents[]`` (``role_custom`` + backend spec)."""
    return [
        {
            "id": a["id"],
            "role_custom": a["role"],
            "backend": {**providers[a["provider"]], "model": a["model"]},
        }
        for a in plan
    ]


CONFIRM_HINT = "yes = start · no = cancel · anything else = describe changes and re-plan"

_YES = frozenset({"", "y", "yes", "ok", "네", "예", "응", "ㅇ", "ㅇㅇ", "좋아"})
_NO = frozenset({"n", "no", "cancel", "아니", "아니오", "아니요", "취소"})


def classify_answer(answer: str) -> str:
    """``"yes"`` / ``"no"`` / ``"revise"`` for a confirmation reply (§5)."""
    key = answer.strip().lower().rstrip(".!")
    if key in _YES:
        return "yes"
    if key in _NO:
        return "no"
    return "revise"


def revision_messages(plan: list[dict[str, Any]], feedback: str) -> list[dict[str, Any]]:
    """Previous proposal + user feedback, appended to the planner conversation."""
    return [
        {"role": "assistant", "content": json.dumps({"agents": plan}, ensure_ascii=False)},
        {
            "role": "user",
            "content": (
                f"Revise the team per this feedback: {feedback}\n"
                "Reply with the full corrected JSON only."
            ),
        },
    ]


async def plan_loop(
    task: str,
    providers: dict[str, dict[str, Any]],
    planner: dict[str, str],
    *,
    ask: Callable[[str], str],
    notify: Callable[[str], None] = print,
    on_user_code: Callable[[str, str], None] | None = None,
) -> list[dict[str, Any]] | None:
    """Catalog → plan → ask → (re-plan on feedback). Session agents, or None.

    ``ask`` is a blocking call (terminal ``input`` / Ink stdin read) — fine
    here because nothing else runs on this loop before the session exists.
    """
    from .backends_factory import build_backend

    if on_user_code is None:
        def on_user_code(code: str, uri: str) -> None:
            notify(
                "Nous Portal sign-in needed — a browser window was opened. "
                f"If it did not open, enter code {code} at {uri}"
            )

    await ensure_oauth(providers, notify, on_user_code)
    catalog = build_catalog(providers, notify)
    if not catalog:
        raise RuntimeError("no usable provider — see the skip reasons above")
    backend = build_backend(
        {**providers[planner["provider"]], "model": planner["model"]},
        on_user_code=on_user_code,
    )
    history: list[dict[str, Any]] = []
    try:
        while True:
            notify(f"Planning team with {planner['provider']}/{planner['model']} ...")
            plan = await plan_team(task, backend, catalog, history)
            answer = ask(format_plan(plan, catalog))
            verdict = classify_answer(answer)
            if verdict == "yes":
                return to_session_agents(plan, providers)
            if verdict == "no":
                return None
            history += revision_messages(plan, answer.strip())
    finally:
        aclose = getattr(backend, "aclose", None)
        if aclose is not None:
            await aclose()


def run_planner(
    task: str,
    providers: dict[str, dict[str, Any]],
    planner: dict[str, str],
    *,
    ask: Callable[[str], str],
) -> list[dict[str, Any]] | None:
    """Terminal entry point for :func:`plan_loop`."""
    import asyncio

    return asyncio.run(plan_loop(task, providers, planner, ask=ask))
