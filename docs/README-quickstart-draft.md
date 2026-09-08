# README 퀵스타트 초안 (agent-4) — README.md 상단 교체/삽입용

> OSS_STRATEGY.md §5 Phase 1: "README 재작성 — 5분 퀵스타트(설치 → YAML → 실행)를
> 맨 위로, AgentRadio 포지셔닝 표 포함".
> 기존 README의 "Core idea / Three primitives / Status" 섹션을 유지하되,
> **이 초안을 맨 위(제목 직후)에 삽입**하고 기존 내용은 아래로 이동.
>
> ⚠️ **갭 메모 (agent-4, 2026-08):**
> - **extras 갭:** 아래 퀵스타트는 OSS_STRATEGY §2.2의 `extras` 분리(`[discord]`, `[all]`)를
>   **전제**로 작성. 현재 `pyproject.toml`은 `py-cord`를 필수 deps로 포함 → extras 아직 없음.
>   `pip install agent-augury` 한 번 = 전부 설치. agent-2 지적: extras 전환엔 lazy import 3단계
>   (session.py → discord_bot.py → import discord) 선행 필요. **첫 릴리스는 py-cord 유지 권장
>   (OSS_STRATEGY §2.2)** → 설치 절은 "`pip install agent-augury` (전부 포함)"로 단순화하는 게 맞음.
> - **버전 갭:** pyproject 0.2 vs 코드 "as of v0.3" (config.py/session.py/DESIGN.md). 0.3.0 정렬 권장.
> - **NOTICE 갭:** LICENSE(Apache-2.0)는 있으나 NOTICE 없음 → Phase 1에서 AgentRadio 인용 NOTICE 추가.
> - **examples 채널 ID 갭:** `multi_bot_demo.yaml`에 실제 Discord 채널 ID 하드코딩(4회) → env/변수로 빼야 함.
> - **오프라인 데모 결함 (OSS_STRATEGY §9 결함 1):** `agent-augury --config examples/*.yaml`(fake 백엔드)
>   는 현재 `load_config`가 `type: fake`를 거부해서 **실패**한다. 결함 1 수정(권장: fake 재허용 +
>   `allow_fake`/`--demo` 플래그) 후에만 이 절의 CLI 명령이 유효. 수정 전에는 `python examples/p1_to_p5_demo.py`
>   (직접 실행)와 `pytest tests/ -q`만 유효.
> - **벤치마크 상태 (agent-4 갱신, 2026-08 — agent-2 독립 재현):**
>   `examples/benchmark/`는 **README.md만 존재**하고 `run_l3_passive_awareness.py`/테스트는 아직
>   구현 전 (OSS_STRATEGY §10 D1: 방향은 "L3-only 재정의"로 확정, 구현은 Phase 2).
>   → 아래 본문에는 벤치마크 실행 명령을 넣지 않는다. README 최종 반영 시에도
>   "Phase 2 (예정)" 표기로만 언급할 것. ci.yml에도 구현 전까지 추가 금지 (이미 주석으로 기록).
> - 위 갭이 해소되기 전까지 이 초안을 README.md에 직접 반영하지 말 것.

---

# agent-augury

**Listen while you work.** — model-agnostic passive awareness multi-agent runtime.

네 명의 에이전트가 공유 라디오 채널로 일하면서 **동시에** 듣는다. 동료의 메시지는
작업을 멈추지 않고도 다음 스텝 경계에서 자동으로 흡수된다 — 통신이 작업을 방해하지 않는다.

> 개념 계승: [AgentRadio](https://github.com/Coral-Protocol/AgentRadio)
> (arXiv:2607.28430) — "패시브 어웨어니스가 +10.5pt를 만든다"는 연구 증거를,
> **어떤 모델, 어떤 환경에서도** 돌아가는 pip 패키지로 재구현한 런타임.
> AgentRadio는 연구 재현 코드(Claude Code 전용, Harbor/Modal, 106MB JAR),
> agent-augury는 범용 오픈소스 런타임(모델 무관, 로컬 Python, pip install).

## Quickstart (5분)

### 1. 설치

```bash
pip install agent-augury          # 전부 포함 (첫 릴리스: py-cord 유지, extras 미분리)
```

개발/테스트용:
```bash
pip install -e ".[dev]"
```

> extras(`[discord]`, `[all]`)는 v0.4+에서 lazy import와 함께 분리 예정
> (OSS_STRATEGY §2.2).

### 2. 세션 YAML 작성

`session.yaml`:
```yaml
mode: L3
max_steps: 0
task: "Multi-agent collaboration"
agents:
  - id: agent-1
    backend:
      type: openai            # 또는 nous / nous_oauth
      model: gpt-4o-mini
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY
  - id: agent-2
    backend:
      type: openai
      model: gpt-4o-mini
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY
```

> `max_steps: 0` = 무제한. 양수로 설정하면 전체 에이전트 스텝 합이 해당 수로 제한됩니다.
> 비밀 값은 **환경변수 이름만** YAML에 적습니다. 실제 키는 `.env` 또는 셸 export.
> Discord 봇을 쓴다면 `channel_id`도 하드코딩 대신 env/변수 참조 권장.

### 3. 실행

```bash
export OPENAI_API_KEY=sk-...
agent-augury --config session.yaml
```

라이브 로그가 실시간으로 출력되고, 끝나면 요약이 나온다:
```
💭 agent-1: ...
💬 [agent-2 → broadcast][thread-1] (FYI) ...
--- session finished: steps=12 threads=2 messages=5 gate=n/a phase=n/a
```

### 4. 옵션

| 명령 | 동작 |
|------|------|
| `agent-augury` | 인터랙티브 위저드 (모델 설정 저장 후 YAML 생성) |
| `agent-augury --config session.yaml` | YAML로 실행 |
| `agent-augury --reconfigure` | 저장된 모델 설정 폐기 후 위저드 재실행 |
| `agent-augury --quiet` | 브로드캐스트 로그 억제 (요약만) — 구현됨 (v0.3) |

### 5. 오프라인 데모 (API 키 없이)

```bash
# Fake 백엔드로 P1~P5 전체 프로토콜 검증 (네트워크 없음)
python examples/p1_to_p5_demo.py

# 단위 테스트 (21개 테스트 모듈, 전부 오프라인)
pytest tests/ -q
```

> ⚠️ `agent-augury --config examples/p1_to_p5_protocol.yaml`(CLI로 fake YAML 실행)은
> **결함 1 수정(§9) 후에만** 동작한다. 수정 전에는 위 `python`/`pytest` 경로를 쓰세요.

---

## Examples

| 파일 | 내용 | 백엔드 | 실행 (결함 1 수정 후) |
|------|------|--------|------|
| `examples/demo.yaml` | L3 토이 — URGENT 정정을 작업 중 흡수 | fake | `agent-augury --config examples/demo.yaml` |
| `examples/p1_to_p5_protocol.yaml` | P1~P5 전체 프로토콜 E2E | fake | `agent-augury --config examples/p1_to_p5_protocol.yaml` |
| `examples/consensus_openai.yaml` | 분할 합의 + 게이트 (실제 LLM) | openai | `export OPENAI_API_KEY=...; agent-augury --config examples/consensus_openai.yaml` |
| `examples/multi_bot_demo.yaml` | Discord 봇 N개 미러 (토큰 env 참조) | fake + bots | `agent-augury --config examples/multi_bot_demo.yaml` |
| `examples/nous_oauth_session.yaml` | Nous Portal OAuth 디바이스 코드 | nous_oauth | `agent-augury --config examples/nous_oauth_session.yaml` |

> 버전 라벨: 모든 예제 YAML 헤더의 버전 표기를 코드(v0.3)와 정렬 필요.
> `multi_bot_demo.yaml`의 `channel_id`는 env로 이동 필요 (결함 5).
> `p1_to_p5_protocol.yaml` 헤더의 "v0.2" 표기도 정렬 대상 (agent-2 발견).
> 벤치마크(`examples/benchmark/`)는 Phase 2 예정 — 아직 실행 명령 없음.

---

## 왜 패시브 어웨어니스인가 (AgentRadio 수치 인용)

| 구성 | 통신 방식 | 과제 정확도 (Opus 4.6) |
|------|-----------|:---:|
| B0 단일 에이전트 | — | 32.3 % |
| L2 + 협상 (블로킹 수신) | 듣기 = 작업 중단 | 51.6 % |
| **L3 + 수동적 인지 (AgentRadio)** | **듣기 = 백그라운드** | **62.1 %** |

L2→L3의 차이는 **통신 방식 1비트**뿐이다.
출처: *AgentRadio: Passive Awareness for Long-Horizon Multi-Agent
Collaboration*, arXiv:2607.28430, 정확 McNemar 검정 p=0.0023 (Opus 4.6).
agent-augury는 그 L3 통신 모드를 **모델·채널·런타임 무관**하게 구현했다.

---

## (이하 기존 README 내용 유지 — Core idea / Three primitives / Status / Install & run)
