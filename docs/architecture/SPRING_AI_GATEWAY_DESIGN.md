# Spring AI 게이트웨이 — 에이전트별 프로바이더 혼용

> **Status:** draft (구상 · **착수 미정**)
> **Date:** 2026-09-17
> **동기:** 2026 오픈소스 AI·SW 특화형 과정 트랙2 Part 2 (Spring AI) 적용처 검토
> **Parent:** `DESIGN.md` §3.2 (Model Backend)
> **인접:** `BACKEND_ERROR_CLASSIFICATION_DESIGN.md` (재시도·폴백 seam)
> **agent-augury 코드 변경:** **없음** (설정 `base_url` 한 줄)
> **결정:** `/v1/models` 먼저 → `/v1/chat/completions` 나중 (§6)

---

## 0. 한 줄

agent-augury 는 그대로 두고, `base_url` 이 가리키는 **상대방**을 직접 만든
Spring Boot 앱으로 바꾼다. 그러면 **에이전트마다 다른 프로바이더**를 쓸 수 있다.

---

## 1. 왜 여기인가

### 1.1 지금 안 되는 것

프로바이더마다 인증 방식(`api_key_env`, OAuth)이 달라 **한 세션에서 섞기 어렵다.**
그런데 협업 프로토콜의 가치는 §2.3 ②가 말하는 **서로 다른 모델의 교차 검증**에 있다.
지금은 네 에이전트가 사실상 같은 모델을 돌린다 — 실사용 로그에서 넷이 같은 답을
네 번 재게시한 것도 이와 무관하지 않다.

### 1.2 접점이 이미 표준이다

`OpenAICompatBackend` 는 임의의 OpenAI 호환 엔드포인트를 받는다.
**위저드(설정 시)와 에이전트 루프(실행 시)가 같은 `base_url` 을 쓴다:**

```text
                       ┌─ 위저드   → GET  {base_url}/models
agent-augury ─ base_url ┤            (backends_factory.py:153 list_models_openai_compat)
                       └─ 루프     → POST {base_url}/chat/completions
                                    (backend/openai_compat.py:48)
```

그래서 게이트웨이가 구현할 것은 **경로 두 개뿐**이다.

---

## 2. 계약 (코드에서 확인한 실제 형태)

### 2.1 `GET /v1/models`

```text
Authorization: Bearer {api_key}
Accept: application/json
```

응답은 `data` 배열에서 id 를 뽑는다 (`openai_compat.py:114`):

```json
{ "data": [ {"id": "gpt-4o"}, {"id": "llama3.1"}, {"id": "claude-sonnet"} ] }
```

**여기가 핵심 화면이다.** 위저드가 이 목록을 그대로 보여주므로, 프로바이더를
섞어 내려주면 사용자가 **에이전트별로 다른 프로바이더를 고르게 된다.**
agent-augury 코드는 한 줄도 바뀌지 않는다.

### 2.2 `POST /v1/chat/completions`

요청 (`openai_compat.py:38`):

```json
{
  "model": "gpt-4o",
  "messages": [ {"role": "...", "content": "..."} ],
  "tools": [ {
    "type": "function",
    "function": {
      "name": "send_message",
      "description": "...",
      "parameters": { "type": "object", "properties": { ... } }
    }
  } ]
}
```

응답에서 읽는 것 (`openai_compat.py:85`):

```json
{ "choices": [ { "message": {
      "content": "...",
      "tool_calls": [ { "id": "...",
        "function": { "name": "...", "arguments": "{...}" } } ]
  } } ],
  "usage": { ... } }
}
```

- `arguments` 는 **JSON 문자열**이다 (`json.loads` 로 파싱).
- `usage` 는 그대로 `Completion.usage` 로 들어간다 — 비용 상한(커리큘럼 ④)의 재료.

### 2.3 `model` 은 라우팅 키다

요청에 모델명이 실려 오므로 게이트웨이가 그것으로 갈래를 정한다.

```text
"gpt-4o"        -> OpenAI
"llama3.1"      -> Ollama   (폐쇄망)
"claude-sonnet" -> Bedrock
```

### 2.4 인증

`_api_key_from_env` 가 **`api_key_env` 를 필수로 요구**한다(없으면 빌드 실패).
로컬 게이트웨이라도 더미 키를 설정하거나 게이트웨이가 아무 키나 받아야 한다.
사소하지만 이것 때문에 기동이 안 되면 원인을 찾기 어렵다.

---

## 3. 최대 난관 — 툴 11개가 왕복해야 한다

매 호출에 **11개 툴 스펙**이 함께 간다:

```text
create_thread  send_message  read_resource  ask_user  read_file
list_directory write_file    run_command    fetch_url edit_file  append_file
```

그중 **`send_message` 가 프로토콜의 생명줄**이다. 왕복이 깨지면 에이전트가 말을
못 하고 게이트가 영영 열리지 않는다 — 즉 **부분 실패가 아니라 전면 정지**다.

### 3.1 Spring AI 쪽 함정

Spring AI 의 툴 추상화(`@Tool`, `ToolCallback`)는 **Spring 앱이 자기 툴을 정의**
하는 용도다. 호출자가 준 스키마를 중계하는 것은 설계 의도가 아니다.

더 중요한 것: 툴 호출이 오면 **Spring AI 가 스스로 실행하려 한다.**
게이트웨이는 실행하면 안 되고 agent-augury 에게 **돌려줘야** 한다.
따라서 **"내부 툴 실행 끄기"** 옵션(`internalToolExecutionEnabled(false)` 류)이
반드시 필요하다. **가장 먼저 확인할 것.** 없으면 §4 의 (b)는 성립하지 않는다.

---

## 4. 게이트웨이 설계 세 갈래

| | Spring AI 사용도 | 툴 왕복 | 평가 |
|--|-----------------|---------|------|
| **(a) 순수 JSON 프록시** | 없음 | 쉬움 | 검증용으로만. 이것만으로는 Spring AI 를 쓴 것이 아니다 |
| **(b) ChatClient 완전 중계** | 높음 | **어려움** | §3.1 옵션에 달림 |
| **(c) ChatModel 빈 라우팅·폴백** | 중간 | 중간 | **현실적 목표** |

(c)가 중심이다. 여러 `ChatModel` 빈(OpenAI / Ollama / Bedrock)을 두고
`model` 로 라우팅하며, 실패 시 다른 모델로 폴백한다.

---

## 5. 폴백을 어디서 할 것인가 (열린 결정)

`BACKEND_ERROR_CLASSIFICATION_DESIGN` §5.2 가 agent-augury 쪽 폴백을 설계해 두었다
(변경 3곳). 게이트웨이가 생기면 **같은 일을 두 곳에서 할 수 있다.**

| | 게이트웨이(Spring AI) | agent-augury |
|--|----------------------|--------------|
| 언어 중립 | ✓ 다른 클라이언트도 혜택 | ✗ |
| 구현 위치 | 한 곳 | 에이전트마다 |
| **관측** | agent-augury 는 어느 모델이 응답했는지 모른다 | `BackendError.model` 로 남는다 |
| 운영 부담 | Java 프로세스 상시 기동 | 없음 |

**둘 다 하면 재시도가 중첩된다** (게이트웨이 3회 × agent-augury 3회 = 9회).
어느 한쪽으로 정해야 한다.

**제안:** 게이트웨이에서 **모델 폴백**, agent-augury 는 **전송 실패 재시도**만.
그 경우 게이트웨이가 응답 헤더로 실제 사용 모델을 알려주면 관측이 회복된다
(예: `X-Model-Used`). 표준이 아니므로 우리가 정의해야 한다.

**D1: 미결.** 게이트웨이를 실제로 만들 때 확정한다.

---

## 6. 착수 순서 (위험이 낮은 것부터)

| 순서 | 내용 | 얻는 것 | 실패해도 |
|------|------|---------|----------|
| 1 | `GET /v1/models` 만 구현, 여러 프로바이더 목록 합쳐 반환 | **위저드에서 에이전트별 프로바이더 선택 화면**이 바로 나온다 | agent-augury 는 기존 `base_url` 로 되돌리면 그만 |
| 2 | `POST /chat/completions` 를 **(a) 순수 프록시**로 | 툴 11개 왕복 여부를 **가장 싸게 검증** | 여기서 깨지면 일찍 안다 |
| 3 | (c) `ChatModel` 둘(OpenAI + Ollama) 라우팅·폴백 | 커리큘럼 ⑩⑬ | (a)에 머문다 |
| 4 | (b) ChatClient 중계 시도 | 커리큘럼 ⑪ | (c)에 머문다 |

**1단계만으로도 눈에 보이는 진전**이다. 위저드 화면에 OpenAI 모델과 로컬 Ollama
모델이 같이 뜨고, 네 에이전트에 다르게 배정할 수 있다.

---

## 7. 이것으로 덮이지 않는 것

- **⑫ RAG / OpenSearch** — 게이트웨이에 맞지 않는다.
  `AgentLoop.LocalTool`(`core/agent/loop.py:120`)이 별도 접점이다.
  `_build_local_tools()` 에 핸들러를 하나 등록하면 에이전트가 검색 툴을 부를 수 있고,
  `web_search` 가 이미 그 경로로 들어와 있어 **패턴이 증명돼 있다.**
  두 접점이 독립이므로 한쪽이 막혀도 다른 쪽은 진행된다.
- **① MCP** — agent-augury 에 아직 없다. 툴이 전부 내장이다.
- **④ 비용 상한** — `usage` 가 이미 들어오므로 누적만 하면 된다. 게이트웨이와 무관.

---

## 8. 열린 결정

| # | 질문 | 메모 |
|---|------|------|
| D1 | 모델 폴백을 게이트웨이 vs agent-augury | §5. 중첩 재시도 주의 |
| D2 | Spring AI 내부 툴 실행 끄기 가능한가 | **가장 먼저 확인.** (b) 성립 여부 |
| D3 | `X-Model-Used` 같은 비표준 헤더를 쓸까 | 관측 회복 vs 표준 이탈 |
| D4 | 게이트웨이를 상시 의존으로 둘까 | Java 런타임이 개발 셋업에 추가된다. 과정 기간만 쓸 수도 |
| D5 | `/v1/models` 에 프로바이더 접두사를 붙일까 | `openai/gpt-4o` 처럼. 라우팅은 쉬워지나 모델명이 길어진다 |

---

## 9. 요약

붙는 자리는 **`base_url` 하나**이고, 위저드와 에이전트 루프가 그것을 공유하므로
게이트웨이는 **경로 두 개**만 만들면 된다. 얻는 것은 **에이전트마다 다른 프로바이더**
— 지금 구조에서 가장 안 되던 것이자 교차 검증 프로토콜이 원래 전제하던 것이다.
최대 위험은 **툴 11개의 왕복**이며, `/v1/models` 부터 시작하면 그 위험을 지연시킨
채로 먼저 성과를 낼 수 있다.
