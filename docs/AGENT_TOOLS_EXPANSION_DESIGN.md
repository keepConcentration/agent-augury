# agent-augury — 에이전트 도구 확장 설계안 (Shell / 웹 검색 / 파일 편집)

> **Task:** agent-augury 에이전트들이 shell 실행·웹 검색 등을 사용할 수 없다 — 필요한 기능 파악 및 구현 설계 문서 작성
> **Date:** 2026-09 · agent-4 (메인 설계/통합) · 기여: agent-1(보안 검토), agent-2(아키텍처·config), agent-3(보안·구현 상세, Hermes 벤치마크)
> **Scope:** 설계 문서 (구현 코드 아님). DESIGN.md §3.5.1(도구 노출) 철학을 유지하며, 기존 파일 도구(D9 `allowed_roots`)의 보안 패턴을 shell/웹 도구로 확장.
> **Status:** v4.1 — **최종 확정** ✅ 사용자 결정: ① 범위 = (b) 권장 5종, ② 보안 = 기본 활성화. agent-1 보안 피드백 6건 + agent-3 Hermes 벤치마크 반영.

---

## 0. 확정 결정 요약

| # | 결정 | 값 |
|---|------|-----|
| D1 | **신규 도구 범위** | `run_command`, `web_search`, `fetch_url`, `edit_file`, `append_file` — **5종 확정** (사용자 2026-09) |
| D2 | **후순위 도구** | `glob` / `grep`(→`search_files` 통합 후보) / `apply_patch` / `execute_code` — v1.1 이후 (본 설계 범위 제외) |
| D3 | **보안 기본값** | **기본 활성화** + 안전장치 기본 내장 (사용자 2026-09) |
| D4 | **기존 도구** | 통신 4종 + 파일 3종 — 스키마 불변, `allowed_roots` 배선 복구 |
| D5 | **노출 도구 수** | 7 → **12** (기존 7 + 신규 5) — `tools:` 미설정 config에도 적용 (의도된 변경) |

---

## 1. 배경: 무엇이 부재한가

agent-augury는 **멀티에이전트 간 통신(패시브 어웨어니스)** 에 집중한 런타임이다. 현재 에이전트가 가진 도구는 통신 3종 + 사용자 질문 + 파일 읽기/목록/쓰기뿐이며, **실제 작업을 수행하는 도구(shell 실행, 웹 검색, URL 조회, 파일 편집)가 없다.**

사용자가 보고한 증상:

> "agent들이 shell 사용이나 웹 검색 등이 안 돼."

이는 우연이 아니라 **DESIGN.md §1.3의 명시적 비목표**였다:

> "코딩 도구(파일 읽기/쓰기/셸 실행)를 갖춘 완전한 코딩 에이전트 — **1단계는 협업/대화 중심**, 코딩 도구는 이후 플러그인."

현재 v0.6(파일 도구 3종이 이미 추가된 상태)에서 이 비목표는 **부분적으로 해소**되었다. 남은 갭은 다음과 같다.

### 1.1 현재 도구 현황 (코드 근거)

| 도구 | 구현 위치 | 상태 | 비고 |
|------|-----------|------|------|
| `create_thread` | `agent/tools.py` → `server.py` | ✅ | 통신 |
| `send_message` | `agent/tools.py` → `server.py` | ✅ | 통신 (fire-and-forget) |
| `read_resource` | `agent/tools.py` → `server.py` | ✅ | 상태 덤프 |
| `ask_user` | `agent/tools.py` → `server.py` | ✅ | HITL |
| `read_file` | `agent/tools.py` (로컬) | ✅ | D9 `allowed_roots` 검증 **※배선 누락(§1.2-6) + startswith 오매칭(§4.4.3)** |
| `list_directory` | `agent/tools.py` (로컬) | ✅ | D9 `allowed_roots` 검증 **※배선 누락(§1.2-6)** |
| `write_file` | `agent/tools.py` (로컬) | ✅ | D9 `allowed_roots` 검증 **※배선 누락(§1.2-6)** |
| **`run_command` (shell)** | ❌ 없음 | 🚫 → **D1 확정** | **핵심 갭** |
| **`web_search` (웹 검색)** | ❌ 없음 | 🚫 → **D1 확정** | **핵심 갭** |
| **`fetch_url` (URL 조회)** | ❌ 없음 | 🚫 → **D1 확정** | **핵심 갭** |
| **`edit_file` (부분 편집)** | ❌ 없음 | 🚫 → **D1 확정** | 코딩 작업 효율 갭 |
| **`append_file`** | ❌ 없음 | 🚫 → **D1 확정** | 로그/누적 기록 갭 |
| `search_files`(glob+grep) / `apply_patch` / `execute_code` | ❌ 없음 | ⏳ **D2 (v1.1 이후)** | 선택 — 본 설계 범위 제외 |

### 1.2 근본 원인 (아키텍처 레벨)

1. **비서버 로컬 도구가 `ToolBox`에 하드코딩** — `agent/tools.py`의 `specs()`/`execute()`가 파일 도구 3종을 직접 구현. 새 도구는 정책(config) 기반 노출이 필요.
2. **`AgentLoop`에 `LocalTool` 주입 경로가 있으나 배선 끊김** — `loop.py`의 `LocalTool` dataclass와 `local_tools` 파라미터는 존재하지만, `session.py` `from_config`에서 주입하지 않는다. **확장 지점은 이미 설계되어 있고 연결만 안 되어 있다.** (agent-1/2/3 공동 확인)
3. **config에 도구 활성화 스키마가 없음** — `config.py`에 `tools:` 섹션이 없다. 도구별 허용/타임아웃/cwd/검색 provider 구성이 불가능.
4. **시스템 프롬프트에 도구 사용 규칙이 파일 도구까지만 기술** — `agent/system_prompt.py`의 `SYSTEM_PROMPT_TEMPLATE`에 "Filesystem tools" 블록은 있으나 shell/웹 블록은 없다. 도구가 동적이 되면 프롬프트도 동적 렌더링이 필요.
5. **테스트가 도구 목록을 고정** — `tests/test_agent_loop.py::test_l3_exposes_seven_tools`가 정확히 7개 도구를 단언.
6. **⚠️ `allowed_roots` 배선 누락 (보안 허점)** — `Session.from_config(allowed_roots=...)` 파라미터와 `ToolBox(allowed_roots=...)`는 존재하지만, **`cli.py` `_run_repl()`이 값을 전달하지 않아 기본 None = 무제한 접근**이다. 즉 D9 검증은 테스트에서만 동작하고 **실제 CLI 세션에서는 파일 도구가 경로 제한 없이 동작**한다. (agent-1/2 공동 확인)

### 1.3 왜 필요한가

- **실질적 작업 수행:** 에이전트가 코드를 실행하거나(git status, pytest, 빌드), 최신 정보를 검색하거나(문서, API 스펙), 파일을 부분 수정해야 실제 코딩 에이전트로 기능한다.
- **협업의 질:** P1~P5 프로토콜에서 각 에이전트가 "실행 결과"를 근거로 검토/승인할 수 있다. (P4 교차검토의 "근거"가 shell 출력·검색 결과로 강화됨.)
- **신뢰:** 에이전트가 추측 대신 실제 시스템 상태(테스트 통과 여부, 네트워크 응답)를 확인할 수 있다.

---

## 2. 설계 목표와 원칙

### 2.1 목표

1. 에이전트가 **shell 명령을 실행**할 수 있다 (`run_command`).
2. 에이전트가 **웹을 검색**하고 **URL을 조회**할 수 있다 (`web_search`, `fetch_url`).
3. 코딩 작업을 위해 **파일 부분 편집/추가**를 지원한다 (`edit_file`, `append_file`). (탐색·검색·패치 도구는 v1.1)
4. **신규 도구는 기본 활성화**하되, **안전장치(deny list·타임아웃·출력/응답 상한·SSRF 방어·cwd 제한)를 기본 내장**한다. (D3)
5. 기존 **패시브 어웨어니스(L3)·SSOT 원칙을 깨지 않는다** — 도구는 전부 비동기, fire-and-forget 호출에 영향을 주지 않는다.
6. 기존 **`allowed_roots`(D9) 보안 패턴을 shell/웹으로 확장**하고, **배선 누락(cli.py→session.py)을 복구**하며, **경로 검증을 `Path.resolve()+relative_to()`로 견고화**한다.

### 2.2 원칙

| # | 원칙 | 근거 |
|---|------|------|
| P1 | **신규 도구는 기본 활성화** (`tools:` 섹션, 기본 `enabled: true`) — 단 안전장치는 기본 내장 | D3 (사용자 결정). "기본으로 쓸 수 있어야 한다" |
| P2 | **도구 구현은 `ToolBox`의 로컬 도구로 추가** (기존 `read_file` 패턴 재사용) | 구조 일관성, `allowed_roots` 검증 로직 재사용 |
| P3 | **shell은 `asyncio.create_subprocess_exec` + `shlex.split`** (셸 인젝션 방지) + 타임아웃 + 출력 크기 상한 | 문자열 셸(`create_subprocess_shell`)은 인젝션 위험, agent-3 제안 |
| P4 | **웹은 httpx 비동기 + 타임아웃 + 응답 크기 상한 + SSRF 방어(사설 IP 대역 차단 기본) + 리다이렉트 재검증** | 외부 요청 안전성 |
| P5 | **권한 경계는 config에서만 설정** (허용 명령어/도메인) — 에이전트 프롬프트 조작으로 우회 불가 | |
| P6 | **시스템 프롬프트에 도구별 사용 규칙을 동적으로 삽입** | 활성화된 도구만 설명 노출 |
| P7 | **실패는 JSON `{error: ...}`로 반환, 예외는 모델에 그대로 노출** | 기존 `_execute_tool`의 try/except 패턴 유지 |
| P8 | **통신 4종(create_thread/send_message/read_resource/ask_user)과 파일 도구 3종의 스키마는 불변** — 단, 기본 활성화로 **노출 도구 수는 7→12로 증가** (D5, 의도된 변경) | |
| P9 | **`allowed_roots` 배선 복구** — cli.py가 세션 cwd/프로젝트 루트를 기본 root로 전달 | 보안 허점 수정 (§1.2-6) |
| P10 | **도메인 매칭은 정확한 서픽스 기준** — `host == domain or host.endswith("." + domain)` | `endswith(domain)`의 오매칭(`notexample.com`) 방지 (agent-1) |
| P11 | **경로 검증은 `Path.resolve()` + `relative_to()`** — `startswith` 문자열 비교 금지 | `/root2`가 `/root`에 매칭되는 오매칭·symlink 우회 방지 (agent-3 Hermes 벤치마크) |

### 2.3 기존 config 호환성 (breaking change 명시)

기본 활성화 정책의 결과로 **`tools:` 섹션이 없는 기존 config에서도 신규 도구 5종이 노출**된다. 이는 **의도된 변경**(D5)이며:

- `test_l3_exposes_seven_tools`는 **갱신 필요** (기본 노출 도구 수가 7→12).
- 기존 세션의 동작(통신/파일 도구)은 불변. 신규 도구는 안전장치 기본값으로 보호.
- 완전히 끄려면 `tools.shell.enabled: false` 등 명시적 비활성화로 가능.
- v1.0 릴리스 노트에 breaking change로 명시한다 (USER_INTERVENTION_DESIGN.md §3.2.3 스타일).

---

## 3. 권장 아키텍처

```
                         config (YAML)
                              │
              ┌───────────────┼──────────────────┐
              ▼               ▼                  ▼
      tools:                 tools:             tools:
      shell:                 web:               file:
      enabled: true         enabled: true       edit_enabled: true
      allowed: [...]        allow_domains:...   allowed_roots:...
      blocked: [...]        block_private_ips: true
              │               │                  │
              ▼               ▼                  ▼
   ┌─────────────────────────────────────────────────────┐
   │              ToolBox (agent/tools.py)               │
   │  · 통신 도구 (항상): create_thread/send_message/... │
   │  · 로컬 도구 (정책 기반 노출):                       │
   │      read_file / list_directory / write_file        │  ← 기존 (D9, 배선 복구, P11)
   │      run_command / web_search / fetch_url           │  ← 신규 (D1, 기본 활성)
   │      edit_file / append_file                        │  ← 신규 (D1, 기본 활성)
   └───────────┬───────────────────┬─────────────────────┘
               │                   │
               ▼                   ▼
        subprocess / httpx    MessageServer (SSOT, 불변)
        (asyncio, 격리)        (도구 결과는 메시지와 무관)
```

### 3.1 핵심 통찰

기존 시스템에서 **로컬 도구는 `ToolBox`의 메서드로 이미 구현**되어 있고(`_read_file`/`_list_directory`/`_write_file`), **보안 정책(`allowed_roots`)은 `ToolBox.__init__`에서 주입**된다. 신규 도구도 동일하게:

1. `ToolBox`에 `_run_command`/`_web_search`/`_fetch_url`/`_edit_file`/`_append_file` 메서드 추가.
2. `ToolBox.__init__`에 신규 정책 객체(`ToolPolicy`) 주입.
3. `specs()`/`execute()`가 정책에 따라 도구를 노출/실행.
4. `session.py` `from_config`가 `tools:` 섹션을 파싱해 `ToolPolicy` 생성 → `AgentLoop`→`ToolBox`에 전달.
5. `cli.py`가 세션 루트(프로젝트 루트)를 `allowed_roots`로 전달해 **배선 복구** (P9).

이렇게 하면 **새 통신 메커니즘이 필요 없다.** 기존 "도구 spec + execute" 추상화에 정책 게이트만 얹으면 된다.

### 3.2 통합 트랙: ToolBox vs LocalTool (2트랙, agent-2/3 합의)

| 트랙 | 대상 | 이유 |
|------|------|------|
| **A. ToolBox 내장** | 공통·보안·파일 도구 (run_command, fetch_url, edit_file, append_file) | `allowed_roots`/정책 공유, 서버(SSOT)와 무관, 일관된 검증 |
| **B. LocalTool 주입** | provider 의존·커스텀 도구 (web_search — 검색 provider 교체 가능) | 에이전트별/역할별 도구 구성, 플러그인형 확장. `AgentLoop(local_tools=...)` 배선을 session.py에서 복구 |

- **web_search를 B로 두는 이유:** 검색 엔진(DDG/Serper/Tavily) 교체가 가능해야 하므로 provider 추상화와 함께 `LocalTool`로 주입. 단, `allowed_roots`류 검증이 필요한 경우 A로 이동.
- **현재 끊긴 배선 2곳을 이번에 복구:** ① `session.py` → `AgentLoop(local_tools=...)`, ② `cli.py` → `Session.from_config(allowed_roots=...)`.

---

## 4. 컴포넌트별 설계

### 4.1 정책 모델: `ToolPolicy` (신규 — `agent/policy.py`)

```python
@dataclass(frozen=True)
class ToolPolicy:
    """Config-derived tool enablement + security bounds. 기본값 = 전부 활성 + 안전장치 내장."""
    # shell
    shell_enabled: bool = True
    shell_allowed: tuple[str, ...] = ()      # 명령어 접두사 allowlist (비면 전부 허용)
    shell_blocked: tuple[str, ...] = (       # 기본 블랙리스트 (파괴적 명령)
        "rm -rf", "mkfs", "dd if=", ":(){", "sudo", "shutdown", "reboot",
        "chmod -R 777 /", "> /dev/sda", "git push --force", "pip uninstall",
    )
    shell_timeout: float = 30.0              # 초
    shell_max_output: int = 16_384           # 문자 수 (16KB, agent-1 제안)
    shell_cwd: str | None = None             # 기본: allowed_roots[0] (§4.2-결정표)
    # web
    web_enabled: bool = True
    web_allow_domains: tuple[str, ...] = ()  # 빈 튜플 = 제한 없음 (deny는 항상 적용)
    web_deny_domains: tuple[str, ...] = ("localhost", "127.0.0.1", "0.0.0.0",
                                         "169.254.169.254", "169.254.170.2",   # + AWS ECS 메타데이터
                                         "metadata.google.internal", "metadata.azure.internal",
                                         "100.100.100.200")                     # + Alibaba 메타데이터
    web_block_private_ips: bool = True       # SSRF: 사설/링크로컬/루프백/CGNAT 차단 (v0.7-2 포함)
    web_timeout: float = 15.0                # 초
    web_max_bytes: int = 65_536              # 응답 본문 상한 (64KB, agent-1 제안)
    web_max_results: int = 5                 # web_search 결과 수 상한
    web_search_provider: str = "duckduckgo"  # duckduckgo | serper | tavily
    # file (기존 D9 확장)
    allowed_roots: tuple[str, ...] = ()
    edit_enabled: bool = True                # edit_file/append_file
```

- **정책은 `AgentLoop`의 `allowed_roots` 파라미터를 확장**한다. 기존 `allowed_roots` 인자는 `ToolPolicy.allowed_roots`로 매핑해 하위호환 유지.
- **기본값 = 전부 활성 + 안전장치 내장** (D3). 끄려면 명시적 `enabled: false`.
- `ToolPolicy.from_config(tools_cfg, allowed_roots=...)` — 전역 `tools:` 섹션 + 에이전트별 `agent.tools` **딥 병합** (agent-2 제안, §4.7).

### 4.2 shell 도구: `run_command`

```python
# spec
{
    "name": "run_command",
    "description": (
        "Run a command asynchronously (no shell interpreter — argv parsing via shlex). "
        "Returns stdout, stderr, and exit code. Output truncated at 16KB. "
        "Blocked: destructive commands (rm -rf, mkfs, sudo, reboot, ...)."
    ),
    "schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "command line; parsed with shlex.split"},
            "timeout": {"type": "number", "description": "override timeout in seconds"},
        },
        "required": ["command"],
    },
}
```

구현 요점 (`_run_command`) — **`create_subprocess_exec` + `shlex.split`** (agent-3 제안 반영):

```python
async def _run_command(self, args: dict[str, Any]) -> str:
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
        argv = shlex.split(command)   # 셸 미경유 → 인젝션 방지 (P3)
    except ValueError as exc:
        return _json({"error": f"cannot parse command: {exc}"})
    try:
        cwd = self._resolve_shell_cwd()   # §4.2 결정표
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _json({"error": f"command timed out after {timeout}s", "command": command})
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

def _resolve_shell_cwd(self) -> str | None:
    """cwd 결정 규칙 (agent-1 피드백 4 반영):
    1) shell_cwd 명시 → 그 경로
    2) allowed_roots[0] 존재 → 그 경로 (기본 동작)
    3) 그 외 → 세션 시작 위치(기본 cwd)
    """
    if self.policy.shell_cwd:
        return self.policy.shell_cwd
    if self.policy.allowed_roots:
        return self.policy.allowed_roots[0]
    return None
```

**설계 결정:**

| 항목 | 결정 | 이유 |
|------|------|------|
| 실행 방식 | `create_subprocess_exec` + `shlex.split` | **셸 인젝션 방지** (agent-3). 파이프/리다이렉트 등 셸 문법은 지원 안 함(문서화) |
| 타임아웃 | `wait_for` + 실패 시 `kill()` + `wait()` | 좀비/누수 방지 |
| 출력 상한 | `shell_max_output`(기본 **16KB**) 초과 시 절단 + `truncated` | 컨텍스트 폭증 방지 (agent-1: tool 결과가 conversation에 그대로 저장되므로 보수적 상한) |
| cwd 기본 | `shell_cwd` → `allowed_roots[0]` → 세션 시작 위치 | agent-1 피드백 4. allowed_roots 설정 시 첫 루트로 제한이 기본 동작 |
| 블랙리스트 | `shell_blocked` 기본 내장 | 파괴적 명령 2차 방어 |
| 화이트리스트 | `shell_allowed` 설정 시에만 적용 (기본 비면 전부 허용) | 기본 활성화 정책 + 유연성 |
| 실패 처리 | JSON `{error}` | 기존 패턴 |
| Windows | `create_subprocess_exec`는 Windows에서도 동작, `shlex.split`은 POSIX 규칙 | 경로 인코딩(CP949) 문서화 필요 (§7) |

> **셸 파이프 필요 시 (v1.1):** `shell: true` 옵션으로 `create_subprocess_shell` 사용을 명시적으로 허용하되, 기본은 exec. (보안 원칙 우선)

### 4.3 웹 도구: `web_search` / `fetch_url`

#### 4.3.1 `fetch_url` (직접 URL 조회)

```python
{
    "name": "fetch_url",
    "description": "Fetch an HTTP(S) URL and return its text content (truncated, HTML tags stripped). SSRF protection: private/link-local IPs and redirect targets are checked.",
    "schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "absolute http(s) URL"},
        },
        "required": ["url"],
    },
}
```

구현 요점 — **agent-1 피드백 1/2/6 + agent-3 Hermes 벤치마크 반영**:

1. **스킴 검사** — `http`/`https`만.
2. **호스트 검사 (P10 정확 서픽스):**
   - `host == domain or host.endswith("." + domain)` — `endswith(domain)`의 오매칭(`notexample.com`) 방지.
   - `web_block_private_ips: true`(기본) → **IP 리터럴이면 `ipaddress`로 사설/루프백/링크로컬/멀티캐스트/예약 대역 차단 (v0.7-2 포함)**. `http://2130706433/`(정수 IP) 같은 우회도 차단.
   - **CGNAT `100.64.0.0/10` 별도 차단**, **IPv4-mapped IPv6(`::ffff:x.x.x.x`) 처리** (agent-3 벤치마크 — v0.7-2 포함).
   - deny 도메인 확장: AWS ECS 메타데이터 `169.254.170.2`, Azure `metadata.azure.internal`, Alibaba `100.100.100.200` 등 클라우드 메타데이터 호스트 포함.
   - 도메인 이름은 v0.7-2에서 deny 목록 + 리터럴 차단, **v1.1에서 DNS 재조회 대역 검증 + connect-time 검증(DNS rebinding 차단)**.
3. **리다이렉트 재검증 (agent-1 피드백 1 — 우선순위 높음):**
   - `follow_redirects=False` + **수동 추적**. 각 hop의 호스트를 동일 규칙으로 재검증하고, 위반 시 `{error: "redirect target blocked"}` 반환. 최대 5 hop.
   - 초기 URL만 검사하고 최종 `resp.url`을 검사하지 않는 우회(`외부 URL → 169.254.169.254` 리다이렉트) 차단.
4. **타임아웃:** `web_timeout`(기본 15s). **크기 상한:** `web_max_bytes`(기본 **64KB**) + `truncated`.
5. **HTML→텍스트:** stdlib `html.parser` 기반 단순 변환 (추가 의존성 없음 — agent-3).
6. **User-Agent:** `agent-augury/<version>`.

```python
async def _fetch_url(self, args: dict[str, Any]) -> str:
    if not self.policy.web_enabled:
        return _json({"error": "web tools are disabled (tools.web.enabled=false)"})
    url = args.get("url", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return _json({"error": "only http/https URLs are allowed"})
    if self._is_blocked_host(parsed.hostname or ""):
        return _json({"error": f"host blocked by SSRF policy: {parsed.hostname}"})

    current_url = url
    try:
        async with httpx.AsyncClient(timeout=self.policy.web_timeout, follow_redirects=False) as client:
            for _hop in range(5):  # 최대 5 hop, 매 hop 재검증 (agent-1 피드백 1)
                resp = await client.get(current_url, headers={"User-Agent": "agent-augury/0.7"})
                if resp.is_redirect and "location" in resp.headers:
                    next_url = urllib.parse.urljoin(current_url, resp.headers["location"])
                    next_host = urllib.parse.urlparse(next_url).hostname or ""
                    if self._is_blocked_host(next_host):
                        return _json({"error": f"redirect target blocked by SSRF policy: {next_host}"})
                    current_url = next_url
                    continue
                break
        text = _html_to_text(resp.text)[: self.policy.web_max_bytes]
        return _json({
            "status_code": resp.status_code,
            "url": str(resp.url),
            "content_type": resp.headers.get("content-type", ""),
            "content": text,
            "truncated": len(resp.text) > self.policy.web_max_bytes,
        })
    except httpx.TimeoutException as exc:
        return _json({"error": f"fetch timed out: {exc}"})
    except httpx.RequestError as exc:
        return _json({"error": f"fetch failed: {exc}"})

def _is_blocked_host(self, host: str) -> bool:
    """deny 도메인(P10 정확 서픽스) + 사설/루프백/CGNAT/IPv4-mapped IPv6 차단 (agent-1 피드백 2/6, agent-3 벤치마크)."""
    if host in self.policy.web_deny_domains:
        return True
    if any(host.endswith("." + d) for d in self.policy.web_deny_domains):
        return True
    if self.policy.web_block_private_ips:
        try:
            ip = ipaddress.ip_address(host)
            if ":" in host and ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped                 # ::ffff:127.0.0.1 → 127.0.0.1
            return (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                    or _is_cgnat(ip))               # 100.64.0.0/10
        except ValueError:
            pass  # 도메인 이름 — v1.1에서 DNS 재조회 대역 검증
    return False

def _is_cgnat(ip) -> bool:
    """CGNAT 100.64.0.0/10 — Hermes url_safety.py 벤치마크 (v0.7-2 포함)."""
    return ip.version == 4 and (ipaddress.ip_address("100.64.0.0") <= ip <= ipaddress.ip_address("100.127.255.255"))
```

> **v1.1 강화 (SSRF 심화, Hermes url_safety.py 수준):** ① 도메인 → DNS 조회(`socket.getaddrinfo`) 후 resolved IP 전부 대역 검증, ② **connect-time 검증**(httpx transport에 네트워크 백엔드 주입 → TCP 연결 직전 재검증 = **DNS rebinding 차단**), ③ 프록시 환경 DNS 위임 정책. **IP 리터럴/CGNAT/IPv4-mapped IPv6 차단은 v0.7-2에 포함** (v1.1로 미루지 않음).

#### 4.3.2 `web_search` (검색)

**설계 결정: 검색 백엔드 추상화** (agent-3 제안 + Hermes web_tools.py 벤치마크):

```python
class WebSearchProvider(ABC):
    """Search backend — DuckDuckGo HTML (기본, 키 불필요) / Serper / Tavily / SearXNG."""
    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[dict[str, Any]]: ...
```

- **기본 구현: DuckDuckGo HTML** (`https://html.duckduckgo.com/html/?q=...`) — API 키 불필요, 모델 무관 철학.
- **대안:** `SERPER_API_KEY`/`TAVILY_API_KEY`/SearXNG 환경변수 기반 어댑터 — `tools.web.search_provider`로 선택.
- 결과: `[{"title", "url", "snippet"}]` 최대 `web_max_results`(기본 5). **검색 결과는 메타데이터만 반환, 전문은 fetch_url로** (Hermes web_search/web_extract 분리와 동일 패턴 — 컨텍스트 절약).
- **Hermes `web_extract`의 head+tail 절단 + 전체 텍스트를 cache에 저장하고 read_file로 페이징** 패턴은 v1.1 자동 요약/절단 정책에 참고 (§7).
- **구현 위치:** provider 교체 가능성 때문에 **LocalTool 트랙 B**로 주입 (에이전트별 구성 가능).

```yaml
# config 예시
tools:
  web:
    enabled: true
    search_provider: duckduckgo   # duckduckgo | serper | tavily | searxng
    max_results: 5
    allow_domains: []             # 비면 전부 (deny/SSRF만 적용)
    block_private_ips: true
    timeout: 15
    max_bytes: 65536
```

### 4.4 파일 도구: `edit_file` / `append_file` + 경로 검증 견고화

기존 `write_file`은 **전체 덮어쓰기**라 대용량 파일·부분 수정에 비효율적. D1로 확정된 2종.

#### 4.4.1 `edit_file` (문자열 치환 기반, agent-2/3 합의)

```python
{
    "name": "edit_file",
    "description": (
        "Replace the FIRST exact occurrence of 'old_string' with 'new_string' in a file. "
        "Must occur exactly once in the file. Respects allowed_roots. Atomic write."
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
}
```

- `old_string`이 파일에 **정확히 1회** 등장해야 성공 (0회/2회 이상 → 오류 + 실제 등장 횟수 반환, 모델이 재시도).
- `allowed_roots` 검증 재사용 (D9) — **배선 복구 후 실제 적용** (P9).
- **원자적 쓰기:** 임시 파일에 쓰고 `os.replace()` (agent-2 제안).
- **정규식 금지** — 고정 문자열 치환만 (모델 환각·예측 불가 패턴 방지). (Hermes patch의 fuzzy matching은 50KB급 대형 도구라 채택하지 않음 — agent-3)
- 결과: `{"replaced": 1, "path": ..., "before_len": N, "after_len": M}`.

#### 4.4.2 `append_file`

```python
{
    "name": "append_file",
    "description": "Append content to the end of a file (creates it if missing). Respects allowed_roots.",
    "schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
}
```

#### 4.4.3 ⚠️ 경로 검증 견고화 (P11 — 기존 코드 버그 수정)

현재 `agent/tools.py`의 `allowed_roots` 검증은 **문자열 `startswith` 비교**다:

```python
# 현재 (버그): "/root2"가 "/root"에 매칭됨, symlink 우회 가능
abs_path.startswith(os.path.abspath(root))
```

**수정 (Hermes path_security.py 벤치마크):**

```python
def _path_within_roots(self, path: str) -> bool:
    """P11: Path.resolve() + relative_to() — startswith 문자열 비교 금지."""
    resolved = Path(path).resolve()
    for root in self.policy.allowed_roots:
        root_resolved = Path(root).resolve()
        try:
            resolved.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False
```

- `Path.resolve()`가 symlink·`..`를 정규화하므로 우회 차단.
- `relative_to()`가 접두사 오매칭(`/root2` ⊄ `/root`)을 원천 차단.
- **기존 파일 도구(read_file/list_directory/write_file)에도 동일 적용** — P11은 신규 도구만이 아니라 기존 D9 검증의 버그 수정.

#### 4.4.4 후순위 도구 (D2 — v1.1 이후, 본 설계 범위 제외)

- `search_files(query, path, recursive?)` — **glob+grep 통합** (Hermes `search_files` 벤치마크). 정규식 허용, 출력 상한 적용.
- `apply_patch(patch_text)` — unified diff 적용. 구현 복잡도 높아 후순위.
- `execute_code(code)` — Python 스크립트 실행 (턴 절약). **격리(제한된 네임스페이스/타임아웃) 필수 — v1.1 심층 설계 필요.**

> **왜 문자열 치환인가:** `write_file`(전체) + `edit_file`(부분) + `append_file`(추가) 3종이면 코드 편집 시나리오 대부분 커버. 라인 번호 기반 편집은 모델 환각 위험이 높아 채택하지 않음.

### 4.5 `ToolBox` 통합 (정책 기반 노출)

```python
class ToolBox:
    def __init__(self, server, allowed_roots=None, policy: ToolPolicy | None = None) -> None:
        self.server = server
        # 배선 복구: allowed_roots가 주어지면 policy 기본값에 반영 (P9)
        self.policy = policy or ToolPolicy(allowed_roots=tuple(allowed_roots or ()))

    def specs(self) -> list[dict]:
        specs = [통신 4종]                          # 항상
        specs += [read_file, list_directory, write_file]   # 항상 (기존)
        if self.policy.edit_enabled:
            specs += [edit_file, append_file]       # 기본 활성 (D1)
        if self.policy.shell_enabled:
            specs += [run_command]                  # 기본 활성 (D1)
        if self.policy.web_enabled:
            specs += [fetch_url]                    # 기본 활성 (D1)
            specs += [web_search] if self._search_provider_tool else []
        return specs
```

- **실행 가드:** `execute()`에서도 `specs()`와 동일 조건으로 미노출 도구 호출 시 `{error: disabled}` 반환.
- **web_search는 `AgentLoop.local_tools`로 주입** (트랙 B). `tool_specs` 프로퍼티가 `ToolBox.specs()` + local_tools를 합산하므로 그대로 노출됨.

### 4.6 시스템 프롬프트 확장 (`system_prompt.py`)

`SYSTEM_PROMPT_TEMPLATE`의 "Filesystem tools" 블록을 **동적 도구 블록**으로 일반화 (P6):

```
{tool_instructions}
```

`render_system_prompt()`에 `tool_instructions` 파라미터 추가, `AgentLoop._update_phase_in_prompt()`에서 활성 도구 목록 기반 렌더링.

```
Shell tool:
- `run_command(command, timeout?)` — run a command (no shell interpreter; shlex parsing).
  stdout/stderr/exit code returned. Output truncated at 16KB.
  Destructive commands are blocked (rm -rf, mkfs, sudo, reboot, ...).
Web tools:
- `fetch_url(url)` — fetch an HTTP(S) URL. HTML tags stripped, truncated at 64KB.
  Private/link-local IPs and blocked redirect targets are rejected (SSRF protection).
- `web_search(query)` — search the web; returns title/url/snippet (max 5).
  Use fetch_url to read full pages.
File edit tools:
- `edit_file(path, old_string, new_string)` — replace first exact occurrence (must be unique).
- `append_file(path, content)` — append to a file.
```

- **비활성 도구는 프롬프트에 언급하지 않는다** (컨텍스트 절약 + 혼동 방지).

### 4.7 config 스키마 확장 (`config.py`) — 전역 + 에이전트별 딥 병합 (agent-2 제안)

```yaml
# 전역 tools: 섹션 (기본값 = 활성 + 안전장치 내장)
tools:
  shell:
    enabled: true
    allowed: []                  # 비면 전부 허용 (블랙리스트만) — 단, 로드 시 경고 (§4.7-2)
    blocked: []                  # 기본 블랙리스트에 추가할 패턴
    timeout_seconds: 30
    max_output_chars: 16384
    cwd: null                    # 기본: allowed_roots[0] (§4.2)
  web:
    enabled: true
    search_provider: duckduckgo
    allow_domains: []
    deny_domains: []
    block_private_ips: true
    timeout_seconds: 15
    max_bytes: 65536
    max_results: 5
  file:
    edit_enabled: true
    allowed_roots: []            # 전역 roots (CLI/session 파라미터와 합집합)

agents:
  - id: agent-1
    tools:                       # 에이전트별 오버라이드 (전역과 딥 병합)
      shell:
        enabled: false           # agent-1만 shell 비활성
```

검증 규칙 (`config.py` 추가):

1. `tools:` 없으면 → 전부 기본값 (**활성 + 안전장치**) — 신규 도구 자동 노출 (§2.3 breaking change, D5).
2. **`shell.enabled: true` + `allowed: []`(빈 화이트리스트) → 로드 시 경고 로그** (agent-1 피드백 5). 운영 가이드에 강조.
3. `tools.web.allow_domains`에 IP 리터럴/`localhost` 입력 시 경고.
4. `tools.file.allowed_roots`와 CLI/session `allowed_roots`는 **합집합**.
5. `search_provider`는 `duckduckgo|serper|tavily|searxng` 중 하나, 아니면 로드 실패.
6. 에이전트별 `tools:`는 전역과 **딥 병합** (에이전트에 없는 키는 전역값 상속).
7. **`allowed_roots` 배선 검증:** cli.py가 roots를 반드시 전달하도록 (기본: 프로젝트 루트). 미전달 시 경고 + 프로젝트 루트 자동 사용 (P9).

`Session.from_config` 변경:

```python
policy = ToolPolicy.from_config(cfg.get("tools", {}), allowed_roots=allowed_roots)
for spec in cfg["agents"]:
    agent_policy = policy.merge(spec.get("tools", {}))   # 에이전트별 딥 병합
    agent = AgentLoop(..., allowed_roots=..., policy=agent_policy, local_tools=build_local_tools(agent_policy))
```

- `AgentLoop.__init__`의 `allowed_roots` 파라미터는 유지(하위호환), 내부에서 `ToolBox(server, policy=...)`로 변환.

### 4.8 CLI 배선 복구 (보안 필수, P9)

```python
# cli.py _run_repl(): 프로젝트 루트를 기본 allowed_roots로 전달
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # agent_augury/cli.py → 프로젝트 루트
session = Session.from_config(cfg, on_step=on_step, on_tool_event=on_tool_event,
                              allowed_roots=[str(PROJECT_ROOT)])
```

- 기본값: **프로젝트 루트**. 사용자가 config `tools.file.allowed_roots`로 확장/축소 가능.
- 이로써 기존 파일 도구의 D9 검증이 **실제 세션에서도 동작**하게 된다 (현재 허점 수정).

### 4.9 TUI 렌더러 (`tui/renderer.py`)

- `_TOOL_ICONS`에 `run_command`/`web_search`/`fetch_url`/`edit_file`/`append_file` 아이콘 추가 (도구 로그 표시용).
- (agent-3 제안 반영 — 도구 로그가 TUI에 그대로 보이므로 누락 시 raw 이름 표시).

---

## 5. 구현 로드맵

| 단계 | 범위 | 산출물 | 통과 기준 |
|------|------|--------|-----------|
| **v0.7-1** | `ToolPolicy` + `run_command` + **allowed_roots 배선 복구 + P11 경로 검증** | `agent/policy.py`, `agent/tools.py`, `session.py`, `config.py`, `cli.py` | Fake 백엔드 E2E: `run_command("python -c 'print(1+1)'")` → exit_code 0/`stdout="2"`; blocked 명령 차단; cwd=allowed_roots[0]; cli.py가 roots 전달; **`/root2`가 `/root`에 비차단(P11)** |
| **v0.7-2** | `fetch_url` + `web_search` (DuckDuckGo) + **SSRF(IP 리터럴·CGNAT·IPv4-mapped IPv6 차단 + 리다이렉트 재검증)** | `agent/web.py`, `agent/tools.py`, `agent/loop.py`(local_tools 배선) | MockTransport: 정상 파싱, 사설 IP(`169.254.169.254`/`10.x`/`2130706433`/`100.64.x.x`/`::ffff:127.0.0.1`) 차단, **리다이렉트 우회 차단**, endswith 오매칭(`notexample.com`) 비차단, 크기 절단 |
| **v0.7-3** | `edit_file` + `append_file` (원자적 쓰기) | `tools.py` | 1회 치환 성공/0회·2회 실패, allowed_roots 경계(P11), append 생성 |
| **v0.8** | 시스템 프롬프트 동적 블록 + 테스트 갱신 | `system_prompt.py`, `loop.py`, `tests/` | 활성 도구만 프롬프트에 존재, `test_l3_exposes_seven_tools` → 12종 검증으로 갱신 |
| **v0.9** | web provider 확장(Serper/Tavily/SearXNG) + **SSRF DNS 재조회·connect-time 검증** | `agent/web.py` | 선택 provider 어댑터 테스트, DNS 리졸브 후 대역 차단, DNS rebinding 시나리오 |
| **v1.0** | (D2) `search_files`, `apply_patch`, `execute_code`, 위저드 도구 단계 | `tools.py`, `wizard.py` | search_files E2E, patch 적용/롤백, execute_code 격리 |

### v0.7-1 통과 기준 (자동 검증)

```
시나리오: 기본 config (tools 미설정 = 기본 활성).
- Fake ModelBackend tool 시퀀스: run_command("python -c 'print(1+1)'")

단언:
  assert run_command 결과 JSON["exit_code"] == 0
  assert "2" in 결과["stdout"]
  assert blocked("rm -rf /") → {"error": "matches shell_blocked"}
  assert shell 비활성 config → {"error": "disabled"}
  assert 타임아웃 초과(sleep 60, timeout 1) → {"error": "timed out"}
  assert cwd == allowed_roots[0]  (shell_cwd 미지정 시)
  assert cli.py 세션에서 allowed_roots == [프로젝트 루트]  (배선 복구)
  assert read_file(프로젝트 루트 밖 경로) → {"error": "outside allowed roots"}
  assert read_file("/root2/...") → allowed_roots=["/root"]일 때 차단  (P11 오매칭 방지)
```

### v0.7-2 통과 기준 (SSRF — agent-1 피드백 1/2/6 + agent-3 벤치마크 반영)

```
단언:
  assert fetch_url("http://169.254.169.254/latest/meta-data/") → {"error": "host blocked"}
  assert fetch_url("http://2130706433/")                    → {"error": "host blocked"}  (정수 IP 우회 차단)
  assert fetch_url("http://100.64.0.1/")                    → {"error": "host blocked"}  (CGNAT)
  assert fetch_url("http://[::ffff:127.0.0.1]/")            → {"error": "host blocked"}  (IPv4-mapped IPv6)
  assert fetch_url("http://example.com") → redirect to "http://169.254.169.254/"
          → {"error": "redirect target blocked"}              (리다이렉트 재검증)
  assert deny "example.com" 일 때 "http://notexample.com/" 는 차단 안 됨 (endswith 오매칭 방지)
  assert fetch_url("http://sub.example.com") 는 deny 시 차단    (P10 정확 서픽스)
```

---

## 6. 테스트 계획

| 테스트 파일 | 범위 |
|-------------|------|
| `tests/test_tool_policy.py` (신규) | ToolPolicy 기본값(전부 활성)/from_config 딥 병합, allowlist/blocklist 검증, 빈 allow 경고 |
| `tests/test_run_command.py` (신규) | 실행/타임아웃/출력 절단/shlex 파싱 오류/blocked/비활성, cwd 결정 규칙, Windows 경로 |
| `tests/test_web_tools.py` (신규) | fetch_url 정상/**SSRF(사설 IP·정수 IP·CGNAT·IPv4-mapped IPv6·리다이렉트 우회·endswith 오매칭)**/deny 도메인/크기 절단/타임아웃, web_search 파싱 |
| `tests/test_file_edit.py` (신규) | edit_file 1회/0회·2회 실패, 원자적 쓰기, append 생성, allowed_roots 경계(P11) |
| `tests/test_agent_loop.py` (수정) | `test_l3_exposes_seven_tools` → 기본 활성 기준 **12종** 목록 검증 |
| `tests/test_config_tools.py` (신규) | tools: 검증, 잘못된 search_provider 로드 실패, 에이전트별 병합, 빈 allow 경고 |
| `tests/test_cli_roots.py` (신규) | cli.py가 allowed_roots 전달, 미전달 시 프로젝트 루트 기본 |
| `tests/test_path_security.py` (신규) | P11: `/root2`⊄`/root`, symlink 우회, `..` 정규화 |

---

## 7. 리스크와 대가

| 리스크 | 완화 |
|--------|------|
| **shell 도구가 임의 코드 실행** (악성 프롬프트/모델 오판) | `create_subprocess_exec`+`shlex`(인젝션 방지) + 블랙리스트 기본 내장 + 타임아웃 + cwd=allowed_roots[0]. 화이트리스트 옵션. **기본 활성화라 운영 가이드 문서 필수** |
| **SSRF** — 내부 서비스(메타데이터 등) 조회 | `web_block_private_ips: true`(기본, v0.7-2 포함: 사설/루프백/링크로컬/CGNAT/IPv4-mapped IPv6) + deny 도메인(클라우드 메타데이터 호스트 확장, P10 정확 서픽스) + **리다이렉트 재검증** + v1.1 DNS 재조회·connect-time 검증(DNS rebinding 차단) |
| **IP 리터럴 우회** (`http://2130706433/`, `::ffff:127.0.0.1`, 16진수 등) | IP 리터럴은 `ipaddress` 대역 검증으로 **v0.7-2에 차단** (v1.1로 미루지 않음) |
| **무한 출력 → 컨텍스트 폭증** | `max_output_chars`(**16KB**)/`max_bytes`(**64KB**) 절단 + `truncated` 플래그. v1.1에 Hermes web_extract식 head+tail 절단·cache 페이징·자동 요약 정책 + read_file max_bytes 옵션 (agent-1 피드백 3, agent-3 벤치마크) |
| **프로세스 좀비/타임아웃 누수** | `wait_for` + `proc.kill()` + `await proc.wait()` |
| **Windows 호환성** — 경로 인코딩(CP949), `shlex.split` POSIX 규칙 | `create_subprocess_exec` 사용, 인코딩 정책 문서화, CI Windows 매트릭스 |
| **기존 테스트 깨짐** (`test_l3_exposes_seven_tools`) | **의도된 breaking change** — 기본 활성화로 도구 수 7→12, 테스트 갱신 (D5, §2.3) |
| **기존 config에 신규 도구가 갑자기 노출** | 안전장치 기본값으로 보호, 릴리스 노트에 breaking change 명시, 끄는 방법 문서화 |
| **모델이 미노출 도구를 호출** | `execute()`에서도 `{error: disabled}` 가드 |
| **DuckDuckGo HTML 파싱 깨짐** | Provider 추상화로 백엔드 교체, 파싱 실패 시 `{error}` 반환 |
| **allowed_roots 배선 누락이 남아 파일 도구 무제한** | **P9 필수** — cli.py가 프로젝트 루트 전달, 미전달 시 경고+자동 적용, 테스트 고정 |
| **경로 검증 `startswith` 오매칭(`/root2`⊄`/root`)/symlink 우회** | **P11 필수** — `Path.resolve()`+`relative_to()`로 교체 (Hermes path_security.py 벤치마크), `test_path_security.py` 고정 |
| **web_search가 에이전트별 구성 안 됨** | LocalTool 트랙 B로 주입, 에이전트별 `tools:` 딥 병합 |
| **`enabled: true` + 빈 화이트리스트로 무방비 실행** | config 로드 시 경고 로그 + 운영 가이드 강조 (agent-1 피드백 5) |

---

## 8. 변경 파일 요약

```
src/agent_augury/
  agent/policy.py            # (신규) ToolPolicy dataclass + from_config() + merge()
  agent/tools.py             # ToolBox: policy 파라미터, run_command/fetch_url/edit_file/append_file,
                             #   조건부 specs()/execute(), _is_blocked_host()(P10 정확 서픽스),
                             #   _path_within_roots()(P11), _resolve_shell_cwd()
  agent/web.py               # (신규) WebSearchProvider 추상화 + DuckDuckGo/Serper/Tavily/SearXNG, _html_to_text()
  agent/system_prompt.py     # 동적 tool_instructions 블록 (활성 도구만)
  agent/loop.py              # AgentLoop: policy 전달, local_tools 배선(web_search), 프롬프트 동적 렌더링
  session.py                 # Session.from_config: tools: → ToolPolicy(전역+에이전트별 병합) → AgentLoop
  config.py                  # tools: 섹션 검증 (shell/web/file), 병합 규칙, allowed_roots 규칙, 빈 allow 경고
  cli.py                     # allowed_roots 배선 복구 (프로젝트 루트 기본) — P9
  tui/renderer.py            # _TOOL_ICONS에 신규 도구 아이콘 추가
examples/
  tools_demo.yaml            # 도구 활성화 예시 (기본값 그대로 + 커스텀 예시)
tests/
  test_tool_policy.py        # (신규)
  test_run_command.py        # (신규)
  test_web_tools.py          # (신규) — SSRF(리다이렉트/IP 리터럴/CGNAT/IPv4-mapped IPv6/endswith) 케이스 포함
  test_file_edit.py          # (신규)
  test_config_tools.py       # (신규)
  test_cli_roots.py          # (신규)
  test_path_security.py      # (신규) — P11
  test_agent_loop.py         # (수정) 기본 활성 12종 도구 목록 검증
docs/
  AGENT_TOOLS_EXPANSION_DESIGN.md   # 본 문서
  TOOLS_OPERATION_GUIDE.md          # (v1.0) 운영 가이드 (보안 기본값, 화이트리스트 예시, 기본 활성화 경고)
```

---

## 9. 결론

현재 에이전트가 shell/웹을 못 쓰는 것은 **의도된 비목표의 잔재**이며, 해결은 새 통신 메커니즘이 아니라 **기존 "도구 spec + execute + allowed_roots 정책" 추상화에 정책 게이트를 얹는 문제**다.

- **사용자 결정 확정 (D1/D3):** 신규 도구 **5종**(`run_command`, `web_search`, `fetch_url`, `edit_file`, `append_file`)을 **기본 활성화** + **안전장치 기본 내장** (`create_subprocess_exec`+`shlex`, SSRF 사설 IP·CGNAT·IPv4-mapped IPv6 차단 + 리다이렉트 재검증 + 정확 서픽스 매칭, 타임아웃, 16KB/64KB 출력 상한, 블랙리스트).
- **배선 2곳 복구 + 버그 1건 수정:** ① `session.py`→`AgentLoop(local_tools=...)`, ② `cli.py`→`Session.from_config(allowed_roots=...)` (**기존 파일 도구 보안 허점 수정**), ③ **P11 경로 검증**(`startswith`→`resolve()+relative_to()`, `/root2` 오매칭·symlink 우회 수정).
- **Hermes 벤치마크 반영 (agent-3):** SSRF(CGNAT/IPv4-mapped IPv6/클라우드 메타데이터/connect-time 검증 로드맵), path_security(P11), tool_output_limits(출력 중앙 제한), web_search/web_extract 분리 패턴.
- `run_command`(인젝션 방지), `fetch_url`/`web_search`(SSRF 방어), `edit_file`/`append_file`(원자적 부분 편집)로 **실질적 코딩/조사 에이전트**가 된다.
- P1~P5 프로토콜·패시브 어웨어니스·SSOT 메시지 서버는 **전혀 건드리지 않는다.**

**v0.7-1(shell+배선 복구+P11) → v0.7-2(web+SSRF) → v0.7-3(편집) → v0.8(프롬프트/테스트) → v0.9(provider/SSRF 강화) → v1.0(search_files/patch/execute_code)** 순서로 검증하며 진행하는 것을 권장한다.

---

## 부록 A. Hermes Agent 도구 벤치마크 (agent-3 조사 요약)

| Hermes 도구 | agent-augury 대응 | 채택 여부 |
|-------------|-------------------|-----------|
| `terminal` / `process` | `run_command` (+프로세스 관리 분리) | ✅ v0.7-1 |
| `web_search(query, limit=5)` → 메타데이터만 | `web_search` (DDG 기본) | ✅ v0.7-2 |
| `web_extract(urls, format, char_limit)` → head+tail 절단+cache | `fetch_url` 단순화 (64KB 절단) | ✅ v0.7-2 (cache/페이징은 v1.1) |
| `read_file` / `write_file` / `patch`(fuzzy) / `search_files` | `read_file`/`write_file` / `edit_file`(고정 문자열) / `search_files` | ✅ v0.7-3 / ⏳ v1.0 |
| `execute_code` | (후보) | ⏳ v1.0 (격리 필수) |
| `todo` / `memory` / `delegate_task` | (후보) | ⏳ 장기 |
| `browser_*` / `vision_*` / `image_generate` / `computer_use` 등 | (범위 밖) | ❌ |

**벤치마킹한 보안 구현:**
- `url_safety.py`: DNS 해석 후 `ipaddress` 대역 검증 + CGNAT `100.64.0.0/10` + IPv4-mapped IPv6 + 클라우드 메타데이터 호스트 + **connect-time 검증(DNS rebinding 차단)** → §4.3.1 (v0.7-2: 리터럴/CGNAT/mapped-IPv6, v1.1: DNS/connect-time)
- `path_security.py`: `validate_within_dir = Path.resolve() + relative_to()` → **P11** (§4.4.3)
- `tool_output_limits.py`: `tool_output: {max_bytes, max_lines, max_line_length}` 중앙 제한 → `ToolPolicy` (§4.1)
