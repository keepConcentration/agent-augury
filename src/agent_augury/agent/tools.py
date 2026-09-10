"""Agent-facing tools backed by the internal message server (§3.5.1).

AGENT_TOOLS_EXPANSION_DESIGN.md (v4.1) 기준 확장:

- 기존: create_thread, send_message, read_resource, ask_user,
  read_file, list_directory, write_file  (통신 4종 + 파일 3종, 스키마 불변 P8)
- 신규 (D1, 기본 활성 D3):
  - run_command   — 셸 실행 (create_subprocess_exec + shlex, P3)
  - fetch_url     — URL 조회 (SSRF 방어, P4 — agent/web.py fetch_url_safe 위임)
  - edit_file     — 파일 부분 수정 (고정 문자열 치환, 원자적 쓰기)
  - append_file   — 파일 끝에 추가
  - web_search    — ToolBox 에는 없음. LocalTool 트랙 B 로 session.py(agent-2) 가
                    agent/web.py build_search_provider 로 주입 (중복 노출 방지)
- 보안:
  - P9: allowed_roots 배선 복구 (cli.py → session.py → ToolBox)
  - P11: 경로 검증 = Path.resolve() + relative_to() (startswith 금지)
  - SSRF: web.py is_blocked_host (정수·hex IP 우회 포함, v0.7-2)
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any

from ..server import MessageServer
from .policy import ToolPolicy


def _json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


class ToolBox:
    """Binds server operations into model-callable tools."""

    def __init__(
        self,
        server: MessageServer,
        allowed_roots: list[str] | None = None,
        policy: ToolPolicy | None = None,
    ) -> None:
        self.server = server
        # P9 배선 복구: allowed_roots가 주어지면 policy 기본값에 반영.
        # policy 미지정 시 기본값(전부 활성 + 안전장치) + allowed_roots.
        self.policy = policy or ToolPolicy.from_config(None, allowed_roots=allowed_roots)

    # -- path security (P11) -------------------------------------------------

    def _path_within_roots(self, path: str) -> bool:
        """P11: Path.resolve() + relative_to() — startswith 문자열 비교 금지.

        allowed_roots 가 비어 있으면 무제한(하위호환). 단, cli.py 가 P9 로
        프로젝트 루트를 전달하므로 실제 세션에서는 항상 제한된다.
        """
        if not self.policy.allowed_roots:
            return True
        resolved = Path(path).resolve()
        for root in self.policy.allowed_roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False

    def _check_root(self, path: str) -> str | None:
        """Return error JSON when *path* is outside allowed roots, else None."""
        if not self._path_within_roots(path):
            return _json({"error": f"path outside allowed roots: {path}"})
        return None

    # -- tool specs ----------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        """JSON-schema tool specs. 신규 도구는 policy 기반 조건부 노출 (§4.5).

        web_search 는 LocalTool 트랙 B 로 session.py 에서 주입되므로
        ToolBox.specs() 에 포함하지 않는다 (중복 노출 방지, §3.2/§4.5).
        """
        specs = [
            {
                "name": "create_thread",
                "description": "Open a named conversation thread with the given participants and return its id.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "participants": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["name", "participants"],
                },
            },
            {
                "name": "send_message",
                "description": (
                    "Post a message to a thread. Fire-and-forget: returns immediately. "
                    "Empty mentions broadcasts to the thread's participants (except you). "
                    'Prefix content with "(FYI)" or "(URGENT)" when appropriate.'
                ),
                "schema": {
                    "type": "object",
                    "properties": {
                        "thread": {"type": "string", "description": "thread id"},
                        "content": {"type": "string"},
                        "mentions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "agent ids to address; empty = broadcast",
                        },
                    },
                    "required": ["thread", "content"],
                },
            },
            {
                "name": "read_resource",
                "description": "Explicit full state dump of threads/messages for recovery or aggregation. Never injected automatically.",
                "schema": {"type": "object", "properties": {}},
            },
            {
                "name": "ask_user",
                "description": (
                    "Ask the human user a question or request confirmation. "
                    "Fire-and-forget: returns immediately; the user's reply arrives "
                    "later as a [radio] message from 'human'. Use options to give "
                    "clear choices. Prefix important requests with REQUEST_APPROVAL: "
                    "when a human gate is configured."
                ),
                "schema": {
                    "type": "object",
                    "properties": {
                        "thread": {"type": "string", "description": "thread id"},
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "optional answer choices",
                        },
                    },
                    "required": ["thread", "question"],
                },
            },
            {
                "name": "read_file",
                "description": "Read a file's content from the filesystem. Returns the file content as text.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path to read"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_directory",
                "description": "List contents of a directory. Returns entries with name, is_dir, and size.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "directory path to list", "default": "."},
                    },
                },
            },
            {
                "name": "write_file",
                "description": "Write content to a file on the filesystem. Creates parent directories as needed.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "file path to write"},
                        "content": {"type": "string", "description": "content to write"},
                    },
                    "required": ["path", "content"],
                },
            },
        ]
        # 신규 도구 (D1, 기본 활성 — policy 로 끌 수 있음)
        if self.policy.shell_enabled:
            specs.append(
                {
                    "name": "run_command",
                    "description": (
                        "Run a command asynchronously (no shell interpreter — argv parsing via shlex). "
                        "Returns stdout, stderr, and exit code. Output truncated at "
                        f"{self.policy.shell_max_output} chars. "
                        "Blocked: destructive commands (rm -rf, mkfs, sudo, reboot, ...)."
                    ),
                    "schema": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "command line; parsed with shlex.split",
                            },
                            "timeout": {
                                "type": "number",
                                "description": "override timeout in seconds",
                            },
                        },
                        "required": ["command"],
                    },
                }
            )
        if self.policy.web_enabled:
            specs.append(
                {
                    "name": "fetch_url",
                    "description": (
                        "Fetch an HTTP(S) URL and return its text content "
                        "(truncated, HTML tags stripped). SSRF protection: "
                        "private/link-local IPs, integer-IP obfuscation, and "
                        "redirect targets are checked."
                    ),
                    "schema": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "absolute http(s) URL",
                            },
                        },
                        "required": ["url"],
                    },
                }
            )
        if self.policy.edit_enabled:
            specs.extend(
                [
                    {
                        "name": "edit_file",
                        "description": (
                            "Replace the FIRST exact occurrence of 'old_string' with 'new_string' "
                            "in a file. Must occur exactly once in the file. "
                            "Respects allowed_roots. Atomic write."
                        ),
                        "schema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                            },
                            "required": ["path", "old_string", "new_string"],
                        },
                    },
                    {
                        "name": "append_file",
                        "description": (
                            "Append content to the end of a file (creates it if missing). "
                            "Respects allowed_roots."
                        ),
                        "schema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                ]
            )
        return specs

    # -- execution -----------------------------------------------------------

    async def execute(self, agent_id: str, name: str, args: dict[str, Any]) -> str:
        if name == "create_thread":
            tid = await self.server.create_thread(
                args["name"], participants=list(args["participants"])
            )
            return _json({"thread_id": tid})
        if name == "send_message":
            mid = await self.server.send_message(
                args["thread"],
                author=agent_id,
                content=args["content"],
                mentions=list(args.get("mentions") or []),
            )
            return _json({"message_id": mid, "status": "sent"})
        if name == "read_resource":
            snap = self.server.snapshot()
            self.server._emit_event({
                "type": "read_resource",
                "agent_id": agent_id,
                "threads": len(snap["threads"]),
                "messages": len(snap["messages"]),
                "timestamp": int(__import__("time").time()),
            })
            return _json(snap)
        if name == "ask_user":
            content = f"[ask-user] {args['question']}"
            options = args.get("options")
            if options:
                content += "  (옵션: " + " / ".join(options) + ")"
            mid = await self.server.send_message(
                args["thread"],
                author=agent_id,
                content=content,
                mentions=["human"],
            )
            return _json({"message_id": mid, "status": "question_delivered"})
        if name == "read_file":
            return await self._read_file(args)
        if name == "list_directory":
            return await self._list_directory(args)
        if name == "write_file":
            return await self._write_file(args)
        if name == "run_command":
            return await self._run_command(args)
        if name == "fetch_url":
            return await self._fetch_url(args)
        if name == "edit_file":
            return await self._edit_file(args)
        if name == "append_file":
            return await self._append_file(args)
        raise ValueError(f"unknown tool: {name}")

    # -- filesystem tools (기존 3종, P11 검증 적용) ----------------------------

    async def _read_file(self, args: dict[str, Any]) -> str:
        """Read a file's content."""
        path = args.get("path", "")
        if not path:
            return _json({"error": "path is required"})
        err = self._check_root(path)
        if err:
            return err
        abs_path = os.path.abspath(path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:  # noqa: ASYNC230
                content = f.read()
            return _json({"path": abs_path, "content": content, "size": len(content)})
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"failed to read {path}: {exc}"})

    async def _list_directory(self, args: dict[str, Any]) -> str:
        """List directory contents."""
        path = args.get("path", ".")
        err = self._check_root(path)
        if err:
            return err
        abs_path = os.path.abspath(path)
        try:
            entries = []
            for entry in os.listdir(abs_path):
                full = os.path.join(abs_path, entry)
                stat = os.stat(full)
                entries.append({
                    "name": entry,
                    "is_dir": os.path.isdir(full),
                    "size": stat.st_size,
                })
            return _json({"path": abs_path, "entries": entries})
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"failed to list {path}: {exc}"})

    async def _write_file(self, args: dict[str, Any]) -> str:
        """Write content to a file (creates parent dirs)."""
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return _json({"error": "path is required"})
        err = self._check_root(path)
        if err:
            return err
        abs_path = os.path.abspath(path)
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:  # noqa: ASYNC230
                f.write(content)
            return _json({"path": abs_path, "size": len(content), "status": "written"})
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"failed to write {path}: {exc}"})

    # -- shell tool: run_command (P3) ----------------------------------------

    def _resolve_shell_cwd(self) -> str | None:
        """cwd 결정 규칙 (§4.2): shell_cwd → allowed_roots[0] → 세션 시작 위치."""
        if self.policy.shell_cwd:
            return self.policy.shell_cwd
        if self.policy.allowed_roots:
            return self.policy.allowed_roots[0]
        return None

    async def _run_command(self, args: dict[str, Any]) -> str:
        """Shell 실행 — create_subprocess_exec + shlex.split (셸 인젝션 방지)."""
        if not self.policy.shell_enabled:
            return _json({"error": "shell tool is disabled (tools.shell.enabled=false)"})
        command = args.get("command", "").strip()
        if not command:
            return _json({"error": "command is required"})
        # 블랙리스트 (기본 내장 + 사용자 추가)
        if any(pat in command for pat in self.policy.shell_blocked):
            return _json({"error": "command matches shell_blocked blocklist"})
        # 화이트리스트 (설정 시에만)
        if self.policy.shell_allowed and not any(
            command.startswith(pfx) for pfx in self.policy.shell_allowed
        ):
            return _json({"error": f"command not in shell_allowed: {command}"})

        timeout = float(args.get("timeout", self.policy.shell_timeout))
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return _json({"error": f"cannot parse command: {exc}"})
        try:
            cwd = self._resolve_shell_cwd()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return _json(
                    {"error": f"command timed out after {timeout}s", "command": command}
                )
            max_out = self.policy.shell_max_output
            out_text = stdout.decode("utf-8", errors="replace")
            err_text = stderr.decode("utf-8", errors="replace")
            return _json({
                "exit_code": proc.returncode,
                "stdout": out_text[:max_out],
                "stderr": err_text[:max_out],
                "truncated": len(out_text) > max_out or len(err_text) > max_out,
            })
        except FileNotFoundError as exc:
            return _json({"error": f"command not found: {exc}"})
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"failed to run command: {exc}"})

    # -- web tool: fetch_url (P4 SSRF, web.py 위임) ---------------------------

    async def _fetch_url(self, args: dict[str, Any]) -> str:
        """URL 조회 — SSRF-safe fetch (agent/web.py fetch_url_safe 위임).

        SSRF: deny 도메인(P10 정확 서픽스) + IP 대역(사설/루프백/링크로컬/
        CGNAT/IPv4-mapped IPv6) + 정수·hex IP 우회 차단 + 리다이렉트 재검증.
        v0.7-2 통과 기준 케이스 전부 web.py is_blocked_host 가 처리.
        """
        if not self.policy.web_enabled:
            return _json({"error": "web tools are disabled (tools.web.enabled=false)"})
        url = args.get("url", "")
        from .web import fetch_url_safe

        result = await fetch_url_safe(
            url,
            deny_domains=self.policy.web_deny_domains,
            block_private_ips=self.policy.web_block_private_ips,
            timeout=self.policy.web_timeout,
            max_bytes=self.policy.web_max_bytes,
        )
        return _json(result)

    # -- file edit tools (v0.7-3) ----------------------------------------------

    async def _edit_file(self, args: dict[str, Any]) -> str:
        """파일 부분 수정 — 고정 문자열 1회 치환 + 원자적 쓰기 (os.replace)."""
        if not self.policy.edit_enabled:
            return _json({"error": "edit tools are disabled (tools.file.edit_enabled=false)"})
        path = args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if not path:
            return _json({"error": "path is required"})
        if not old_string:
            return _json({"error": "old_string is required"})
        err = self._check_root(path)
        if err:
            return err
        abs_path = os.path.abspath(path)
        try:
            with open(abs_path, "r", encoding="utf-8") as f:  # noqa: ASYNC230
                content = f.read()
            count = content.count(old_string)
            if count != 1:
                return _json(
                    {
                        "error": (
                            f"old_string must occur exactly once in the file "
                            f"(found {count} occurrences)"
                        )
                    }
                )
            new_content = content.replace(old_string, new_string, 1)
            # 원자적 쓰기: 임시 파일 → os.replace
            tmp_path = abs_path + ".augury-tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:  # noqa: ASYNC230
                f.write(new_content)
            os.replace(tmp_path, abs_path)
            return _json({
                "replaced": 1,
                "path": abs_path,
                "before_len": len(content),
                "after_len": len(new_content),
            })
        except FileNotFoundError:
            return _json({"error": f"file not found: {path}"})
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"failed to edit {path}: {exc}"})

    async def _append_file(self, args: dict[str, Any]) -> str:
        """파일 끝에 추가 (없으면 생성)."""
        if not self.policy.edit_enabled:
            return _json({"error": "edit tools are disabled (tools.file.edit_enabled=false)"})
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return _json({"error": "path is required"})
        err = self._check_root(path)
        if err:
            return err
        abs_path = os.path.abspath(path)
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "a", encoding="utf-8") as f:  # noqa: ASYNC230
                f.write(content)
            return _json({"path": abs_path, "size": len(content), "status": "appended"})
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"failed to append {path}: {exc}"})
