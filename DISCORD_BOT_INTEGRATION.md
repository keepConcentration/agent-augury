# Discord Bot Integration Research Report

> **Task:** agent-augury 에이전트별 Discord 봇 통합 방안 조사
> **Date:** 2026-09-03
> **Author:** 다해핑 (coder)

---

## 1. Executive Summary

agent-augury의 현재 단일 웹훅 미러(`DiscordWebhookMirror`)를 **에이전트별 별도 Discord 봇**으로 확장하기 위한 기술조사 결과입니다.

**권장 방향:** `py-cord` 라이브러리 + 단일 asyncio 루프 내 N개 `discord.Client` 인스턴스 실행 + 기존 `MessageServer.subscribe_events` 재사용.

난이도: **중간** — discord.py 생태계 숙지가 필요하지만, agent-augury의 이벤트 구조가 이미 깔끔하게 분리되어 있어 비교적 매끄러운 통합이 가능합니다.

---

## 2. Current Architecture (분석 근거)

### 2.1 이벤트 흐름

```
MessageServer (SSOT)
  ├── subscribe(callback)           # raw message dict
  ├── subscribe_events(callback)    # typed events (create_thread, send_message, tool, read_resource)
  └── _emit_event(event)            # best-effort, no raise

Session
  ├── server.subscribe(mirror.on_message)      # 웹훅 미러
  ├── server.subscribe_events(session._on_server_event)  # unified output queue
  └── _output_queue → _output_consumer() → on_step / on_tool_event callbacks

DiscordWebhookMirror (현재)
  ├── on_message(message) → enqueue formatted line
  ├── flush() → POST to webhook URL (httpx)
  └── format_line() → `[thread_id] author: content`
```

### 2.2 핵심 특성

- **단일 asyncio 이벤트 루프** — 락 없음, cooperative scheduling
- **에이전트 N개 = 독립 asyncio.Task** — 병렬 실행
- **미러는 반드시 죽지 않아야 함** — observation must not kill sessions
- **Discord는 읽기 전용 뷰** — 발신 전용, 수신 없음 (SSOT 원칙)

### 2.3 현재 미러의 한계

| 항목 | 현재 (웹훅) | 목표 (봇) |
|------|-------------|-----------|
| 인증 | 단일 URL | N개 토큰 |
| 에이전트 구분 | 없음 (단일 채널) | 에이전트별 별도 봇 |
| 이벤트 타입 | send_message만 | create_thread, tool, step 등 전이벤트 |
| 양방향 | 불가 | 불가 (발신 전용 유지) |
| Rate limit | 웹훅 30/60s | 글로벌 50/5s |

---

## 3. Discord Bot Library Comparison

### 3.1 후보 라이브러리

| 라이브러리 | 장점 | 단점 | 추천도 |
|------------|------|------|--------|
| **py-cord** | discord.py 포크, 슬래시 커맨드 내장, 활발한 유지보수, discord.py와 높은 호환성 | 문서화 약간 부족 | ★★★★★ |
| **discord.py** | 가장 널리 사용, 거대한 커뮤니티, 풍부한 문서 | 개발 중단 후 재개, 슬래시 커맨드 별도 확장 필요 | ★★★★☆ |
| **hikari** | 경량, REST/게이트웨이 분리, 확장성, 현대적 설계 | 생태계 작음, 학습 곡선 | ★★★☆☆ |
| **interactions.py** | 슬래시 커맨드에 특화, 선언적 스타일 | 프레임워크 무거움, 자유도 낮음 | ★★☆☆☆ |
| **nextcord** | discord.py 포크, 슬래시 지원 | 커뮤니티 축소 중 | ★★☆☆☆ |

### 3.2 권장: py-cord

**선정 이유:**

1. **discord.py 호환** — 향후 discord.py로 전환 시 마이그레이션 용이
2. **현대적 기능** — 슬래시 커맨드, 모달, 컴포넌트 내장
3. **활발한 유지보수** — discord.py 중단 이후 가장 활발한 포크
4. **단일 토큰 = 단일 Client** — 봇 N개 = Client N개 패턴과 자연스럽게 맞음
5. **Python 3.11+ 지원** — agent-augury 요구사항 충족

```python
# py-cord 기본 패턴
import discord
bot = discord.Client(intents=discord.Intents.default())
# 또는
bot = discord.Bot(intents=discord.Intents.default())
```

---

## 4. Bot N개 통합 구조

### 4.1 설계 원칙

1. **단일 asyncio 루프 유지** — agent-augury의 동시성 모델 그대로
2. **기존 `subscribe_events` 재사용** — 새 이벤트 시스템 불필요
3. **에이전트별 이벤트 라우팅** — 각 봇은 해당 에이전트의 이벤트만 처리
4. **미러의 안전성 유지** — 봇 실패가 세션 죽이지 않음

### 4.2 구조도

```
Session
  ├── MessageServer (SSOT)
  │     ├── subscribe_events(global_handler)  ← 기존
  │     └── subscribe_events(per_agent_handler)  ← 신규 (에이전트별 라우팅)
  │
  ├── DiscordBotManager
  │     ├── bots: dict[agent_id, discord.Client]
  │     ├── start_all() → asyncio.gather(*[bot.start(token) ...])
  │     ├── stop_all() → asyncio.gather(*[bot.close() ...])
  │     └── route_event(event) → 해당 agent_id의 bot.send()
  │
  └── AgentLoop (N개, 병렬)
        └── step() → tool call → server._emit_event → bot route
```

### 4.3 구현 방식: 단일 클라이언트 vs N개 클라이언트

| 방식 | 장점 | 단점 |
|------|------|------|
| **N개 Client** (권장) | 에이전트 격리, 토큰 분리, 독립 재연결 | 메모리 약간 증가 (~10MB/봇) |
| 1개 Client + 토큰 전환 | 메모리 효율 | Discord API에서 토큰마다 별도 게이트웨이 연결 필요, 전환 복잡 |

**결론: N개 Client** — Discord 게이트웨이 프로토콜에서 연결마다 고유 토큰이 필요하므로, 1개 클라이언트로 토큰 전환은 불가능에 가깝습니다.

### 4.4 코드 스니펫 (개념 검증용)

```python
# channel/discord_bot.py (신규)

import asyncio
import logging
from typing import Any, Callable

import discord

log = logging.getLogger(__name__)


class DiscordBotAdapter:
    """단일 Discord 봇 클라이언트 + 에이전트 바인딩."""

    def __init__(
        self,
        agent_id: str,
        token: str,
        channel_id: int,
        *,
        intents: discord.Intents | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.channel_id = channel_id
        self._client = discord.Client(
            intents=intents or discord.Intents.default(),
        )
        self._token = token
        self._ready = asyncio.Event()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._sender_task: asyncio.Task | None = None

        @self._client.event
        async def on_ready() -> None:
            self._ready.set()
            log.info("bot %s ready as %s", self.agent_id, self._client.user)

    async def start(self) -> None:
        self._sender_task = asyncio.create_task(self._sender_loop())
        await self._client.start(self._token)

    async def close(self) -> None:
        if self._sender_task:
            self._sender_task.cancel()
        await self._client.close()

    async def wait_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=timeout)

    def enqueue(self, content: str) -> None:
        """이벤트 루프에서 안전한 enqueue."""
        self._outbox.put_nowait(content)

    async def _sender_loop(self) -> None:
        """Rate limit 고려한 전송 루프."""
        await self._ready.wait()
        channel = self._client.get_channel(self.channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(self.channel_id)

        while True:
            content = await self._outbox.get()
            try:
                await channel.send(content)
            except discord.HTTPException as exc:
                log.warning("bot %s send failed: %s", self.agent_id, exc)
            except Exception:
                log.exception("bot %s unexpected error", self.agent_id)


class DiscordBotManager:
    """여러 봇 관리 + 이벤트 라우팅."""

    def __init__(self) -> None:
        self._bots: dict[str, DiscordBotAdapter] = {}

    def register(self, bot: DiscordBotAdapter) -> None:
        self._bots[bot.agent_id] = bot

    def route_event(self, agent_id: str, content: str) -> None:
        bot = self._bots.get(agent_id)
        if bot:
            bot.enqueue(content)

    async def start_all(self) -> None:
        await asyncio.gather(*(bot.start() for bot in self._bots.values()))

    async def stop_all(self) -> None:
        await asyncio.gather(*(bot.close() for bot in self._bots.values()))
```

---

## 5. 토큰 관리 방안

### 5.1 옵션 환경

| 방식 | 예시 | 장점 | 단점 |
|------|------|------|------|
| **YAML 리스트** (권장) | `bots: [{agent_id, token_env, channel_id}]` | 명시적, 검증 용이 | YAML 파일에 구조 |
| 환경변수 접두사 | `BOT_TOKEN_agent-1=xxx` | 기존 패턴 유사 | 루프 필요, 에이전트 수 많으면 번거로움 |
| .env 파일 | `BOT_TOKEN_agent-1=xxx` | 개발 편의 | 배포 시 추가 관리 |

### 5.2 권장: YAML 리스트

```yaml
# examples/multi_bot_demo.yaml

bots:
  - agent_id: agent-1
    token_env: BOT_TOKEN_AGENT_1    # 환경변수명 (실제 토큰은 env에서)
    channel_id: 123456789012345678

  - agent_id: agent-2
    token_env: BOT_TOKEN_AGENT_2
    channel_id: 123456789012345678
```

**이유:**
- 기존 `mirror.url_env` 패턴과 일관 (토큰 자체는 env, 매핑은 YAML)
- 에이전트 추가 시 YAML에 한 줄 추가
- 환경변수명을 YAML에 명시 → 어떤 봇이 어떤 토큰을 쓰는지 추적 가능

---

## 6. 이벤트 포맷 매핑

### 6.1 CLI → Discord 변환

| CLI 아이콘 | 이벤트 타입 | Discord 포맷 (예시) |
|------------|-------------|---------------------|
| 🧵 | create_thread | `[agent-1] 🧵 create_thread "plan" (agent-1, agent-2)` |
| 💬 | send_message | `[agent-1 → agent-2] 💬 PROPOSE: split evenly` |
| 💭 | step (text) | `[agent-1] 💭 I'll start by exploring...` |
| 📊 | read_resource | `[agent-1] 📊 read_resource (threads=3, messages=12)` |
| 📖 | read_file | `[agent-1] 📖 read_file src/main.py` |
| 📝 | write_file | `[agent-1] 📝 write_file output/result.md` |
| 📁 | list_directory | `[agent-1] 📁 list_directory src/` |

### 6.2 에이전트별 색상 구분

Discord Embed를 활용하면 에이전트별 색상을 구분할 수 있습니다:

```python
AGENT_COLORS = {
    "agent-1": discord.Color.blue(),
    "agent-2": discord.Color.green(),
    "agent-3": discord.Color.gold(),
}
```

### 6.3 이벤트 핸들러 구현

```python
# session.py 확장

def _on_server_event(self, event: dict[str, Any]) -> None:
    """기존 + 봇 라우팅."""
    event_type = event.get("type")

    # 기존: unified output queue
    self._enqueue_for_display(event)

    # 신규: 봇 라우팅
    if self.bot_manager is None:
        return

    agent_id = event.get("agent_id")
    if agent_id is None:
        return

    content = self._format_for_discord(event)
    if content:
        self.bot_manager.route_event(agent_id, content)


def _format_for_discord(self, event: dict[str, Any]) -> str | None:
    """이벤트 → Discord 메시지 문자열."""
    event_type = event.get("type")

    if event_type == "create_thread":
        return (
            f"🧵 create_thread **{event['name']}** "
            f"({', '.join(event['participants'])})"
        )
    elif event_type == "send_message":
        content = event.get("content", "")
        if len(content) > 1800:  # Discord 2000자 제한 고려
            content = content[:1800] + "…"
        return f"💬 {content}"
    elif event_type == "tool":
        tool = event.get("tool", "?")
        icons = {
            "read_file": "📖", "write_file": "📝",
            "list_directory": "📁", "search": "🔍",
        }
        icon = icons.get(tool, "🔧")
        return f"{icon} {tool}(...)"
    # ... 나머지 이벤트 타입

    return None
```

---

## 7. 제약 및 리스크

### 7.1 Rate Limit

| 제한 | 값 | 대응 |
|------|-----|------|
| 글로벌 | 50 msg/5s | 단일 루프에서 순차 전송이므로 자연 회피 |
| 채널 | 5 msg/5s (non-moderated) | 에이전트별 채널 분리 시 영향 적음 |
| 웹훅 | 30 msg/60s | 봇 방식은 웹훅 아니므로 해당 없음 |

**결론:** 에이전트 3~5개 수준에서는 rate limit 걱정 불필요. 10개 이상 시 전송 큐에서 버킷 알고리즘 적용 검토.

### 7.2 게이트웨이 연결 수명

- discord.py/py-cord는 자동 heartbeat + 재연결 내장
- 네트워크 끊김 시 `on_disconnect` → 자동 재연결 (지수 백오프)
- 세션 종료 시 `bot.close()`로 정리

### 7.3 Windows 환경

- discord.py/py-cord는 Windows에서 정상 동작
- asyncio 이벤트 루프 정책: Windows에서는 `ProactorEventLoop` 기본 (Python 3.8+)
- 별도 설정 불필요

### 7.4 기존 `DiscordWebhookMirror`와의 관계

| 옵션 | 설명 |
|------|------|
| **대체** (권장) | 웹훅 미러 제거, 봇 방식으로 전면 교체 |
| 병행 | 웹훅은 서버 전체 로그, 봇은 에이전트별 뷰 |

**권장: 대체** — 웹훅은 단일 URL + 단일 채널 제약이 있어 에이전트별 분리 불가. 봇 방식이 상위 호환.

---

## 8. 예상 구현 설계

### 8.1 파일 구조

```
src/agent_augury/
  channel/
    __init__.py
    discord_mirror.py      # 기존 (호환 유지 또는 제거)
    discord_bot.py         # 신규: DiscordBotAdapter + DiscordBotManager
  session.py               # 확장: bot_manager 통합
  cli.py                   # 확장: --bot-config 옵션
```

### 8.2 클래스 설계

```
DiscordBotAdapter
  ├── __init__(agent_id, token, channel_id, intents)
  ├── start() → asyncio.Task
  ├── close()
  ├── wait_ready(timeout)
  ├── enqueue(content)
  └── _sender_loop()  # rate limit 고려 전송

DiscordBotManager
  ├── register(bot: DiscordBotAdapter)
  ├── route_event(agent_id, content)
  ├── start_all()
  └── stop_all()

Session (확장)
  ├── bot_manager: DiscordBotManager | None
  ├── _on_server_event() → bot_manager.route_event()
  └── _format_for_discord(event) → str | None
```

### 8.3 설정 스키마 (YAML)

```yaml
bots:
  - agent_id: agent-1
    token_env: BOT_TOKEN_AGENT_1
    channel_id: 123456789012345678
    color: "#3498db"          # 선택: 에이전트 색상

  - agent_id: agent-2
    token_env: BOT_TOKEN_AGENT_2
    channel_id: 123456789012345678
    color: "#2ecc71"
```

### 8.4 구현 태스크 분해 (예상)

| # | 태스크 | 난이도 | 설명 |
|---|--------|--------|------|
| 1 | `channel/discord_bot.py` 구현 | 중 | DiscordBotAdapter + DiscordBotManager |
| 2 | `session.py` 통합 | 하 | bot_manager 초기화 + 이벤트 라우팅 |
| 3 | YAML 스키마 확장 | 하 | bots 섹션 파싱 |
| 4 | CLI 연동 | 하 | `--bot-config` 또는 자동 감지 |
| 5 | 예시 YAML 작성 | 하 | `examples/multi_bot_demo.yaml` |
| 6 | 테스트 작성 | 중 | MockTransport 기반 단위 테스트 |
| 7 | 통합 테스트 | 중 | 실제 Discord 서버에서 E2E 검증 |

---

## 9. 확인 필요 사항

1. **봇 토큰 발급 주체** — 사용자가 직접 Discord Developer Portal에서 생성? 자동화 가능?
2. **채널 ID 확인 방법** — 개발자 모드에서 수동 확인? 봇가입 시 자동 감지?
3. **에이전트 수 상한** — 현재 몇 개까지 예상? (rate limit 설계에 영향)
4. **양방향 통신 여부** — 현재는 발신 전용이지만, 후속에서 Discord 명령어로 에이전트 제어 고려?

---

## 10. 결론

- **권장 라이브러리:** py-cord
- **통합 구조:** 단일 asyncio 루프 + N개 discord.Client
- **토큰 관리:** YAML 리스트 + 환경변수 참조
- **난이도:** 중간 (1~2일 예상)
- **기존 코드 영향:** 최소 (subscribe_events 확장, 기능 추가 방식)
- **DiscordWebhookMirror 처리:** 제거 (봇이 상위 호환)

다 해냈습니다. 구현 태스크는 이 보고서를 바탕으로 생성해주세요.
