# Team planner — 작업을 보고 에이전트 팀을 짠다

> **Status:** v1 구현 완료 (실제 provider 수동 검증 전)
> **Date:** 2026-09-28
> **Rev:** v1.1 — Ink 안에서 task 입력·확인(`--plan-first` 사전 단계), 자유 텍스트 피드백으로 재구성
> **Code:** `src/agent_augury/planner.py` (신규), `wizard.py`, `model_config.py`, `cli.py`, `gateway/session_stdio.py`, `fronts/ink/src/{gateway.ts,App.tsx}`
> **Tests:** `tests/test_planner.py` (32)
> **관련:** `DYNAMIC_ROSTER_DESIGN.md` (세션 안에서 pool → roster 축소), `USER_INTERVENTION_DESIGN.md`

---

## 0. Problem

지금 사용자는 wizard에서 **에이전트마다** id · provider · model을 직접 고른다.

| 위치 | 현재 동작 | 문제 |
|------|-----------|------|
| `wizard._collect_model_settings` | agent 1명씩 `_build_agent` 반복 | 몇 명이 필요한지, 어떤 모델이 맞는지 사용자가 미리 알아야 함 |
| `model_config.json` | `agents[]` 고정 명단 저장 | 작업이 바뀌어도 같은 팀이 재사용됨 |
| 역할 | `role_custom` 필드가 있지만 wizard가 안 채움 | 전원이 역할 없이 같은 프롬프트로 시작 |

사용자가 실제로 아는 건 "무슨 일을 시킬지"와 "어떤 provider 계정이 있는지" 두 가지다. 팀 구성은 그 둘로부터 도출할 수 있다.

## 1. 핵심 결정

### 1.1 사용자는 provider와 플래너 모델만 설정한다

wizard(최초 1회)는 **provider 등록·인증 + 플래너 모델 1개 선택**만 한다. 에이전트 명단은 작업마다 플래너 LLM이 만든다.

### 1.2 플래너는 세션 **시작 전** 1회 호출이다

- 세션 안쪽(`Session`, protocol, gateway)은 바꾸지 않는다. 플래너 출력은 **평범한 세션 YAML**이고, 이후 경로는 `_launch_session()` 그대로다.
- 세션 중 에이전트 교체·추가는 비목표(§7). 세션 안에서 인원을 줄이는 건 이미 `DYNAMIC_ROSTER_DESIGN.md`의 roster가 한다 — 플래너는 **pool**을 정하고, roster는 그 pool 안에서 턴마다 축소한다. 두 층은 겹치지 않는다.

### 1.2a 기본 경로는 Ink 안에서 묻는다

사용자가 보기엔 "Ink 입력 → 팀 구성 → 그 입력으로 실행"이다. 구현은 Ink gateway 자식(`session_stdio --plan-first`)이 **Core Session을 만들기 전에** 짧은 사전 단계를 돈다(§2.1). 터미널 `--plan TASK` 경로는 headless·스크립트용으로 남긴다.

### 1.3 결정 모델(Jev 등)이 아니라 일반 LLM을 쓴다

팀 구성은 드물게(작업당 1회) 일어나고, 역할 설명(`role_custom`)이라는 **글**이 필요하다. 빠른 typed-decision 모델의 이점(지연·호출 단가)이 여기선 무의미하다. 결정 모델은 매 step 일어나는 `core/attention.py` 판단 쪽 후보로 남긴다.

### 1.4 수동 방식은 유지한다

`agents[]` 직접 지정(`--config` YAML, wizard "Manual" 모드)은 그대로 둔다. 재현 가능한 설정, 오프라인 fake 데모, 기존 테스트, 기존 사용자의 저장 설정이 모두 이 경로다.

## 2. 흐름

```
[wizard — 최초 1회 / --reconfigure]
  Setup mode: 1) Team planner (기본)  2) Manual agents
  └ 1) provider 등록 루프 (nous_oauth / openrouter / openai / nous)
       → 인증 (기존 OAuth device code · api_key_env 재사용)
     플래너 provider + model 선택
     → model_config.json 에 providers + planner 저장

[매 실행 — 기본: Ink]
  agent-augury
  ① model_config 로드 → planner 모드
  ② provider API 키 확인 (터미널, Ink 전 — 기존 _prompt_and_set_api_keys)
  ③ providers/planner 를 담은 세션 YAML 저장 → Ink 실행 (AUGURY_PLAN_FIRST=1)
  ④ [session_stdio 사전 단계] 첫 human.send = task
  ⑤ catalog 수집 → 플래너 호출 → 검증 (실패 시 사유 붙여 1회 재시도)
  ⑥ human.question 으로 구성안 → yes / no / 자유 텍스트(= 수정 요청 → ⑤ 재호출)
  ⑦ yes → YAML을 평범한 세션 설정(agents + task)으로 덮어씀 → Core 기동(new_session) → task 자동 실행

[터미널 — --plan "작업" 또는 --headless]
  ②와 같고, ④~⑥을 터미널 input 으로 한 뒤 ⑦
```

⑦에서 `new_session`을 강제하는 이유: 명단이 바뀐 팀으로 이전 체크포인트(LATEST)를 이어 붙이면 참가자·inbox가 어긋난다.

### 2.1 Ink 사전 단계 (`session_stdio.plan_first`)

Wire 통로(gateway)는 Session이 소유하는데, Session은 명단이 있어야 만들 수 있다. 그래서 사전 단계는 gateway 없이 **JSONL을 직접** 주고받는다:

| 방향 | 메시지 | 용도 |
|------|--------|------|
| → Ink | `session.started` (`agents: []`) · `log` | 준비 알림. Core 기동 뒤 명단을 담은 `session.started`가 한 번 더 오고 Ink가 roster를 갱신 |
| ← Ink | `human.send` | task |
| → Ink | `human.question` (`question_id: plan-N`, options yes/no) | 구성안 확인 (기존 ask_user 패널 재사용) |
| ← Ink | `human.answer` / `human.skip` | `1`/`2` 는 옵션 번호, `/skip` 은 no |
| → Ink | `session.turn_done` (`reason: planning`) | 거절·실패 후 Ink의 running 표시를 풀어 다음 task를 받음 |
| ← Ink | `session.quit` / EOF | 종료 (`session.ended`) |

사전 단계가 `sys.stdin`을 순서대로 읽고 끝나면, runner의 stdin 리더 스레드가 같은 스트림을 이어받는다. 이 단계의 모든 출력은 Wire로만 보낸다(bare `print` 금지 → `build_catalog(notify=)`, OAuth device code도 `log` 이벤트).

## 3. 저장 형식

`~/.agent-augury/model_config.json`:

```json
{
  "max_steps": 0,
  "providers": {
    "nous_oauth": {"type": "nous_oauth", "base_url": "https://inference-api.nousresearch.com/v1"},
    "openrouter": {"type": "openai", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}
  },
  "planner": {"provider": "nous_oauth", "model": "Hermes-4-405B"},
  "bots": []
}
```

- provider 이름 = wizard provider key (`openai` / `openrouter` / `nous` / `nous_oauth`). v1은 **종류당 1개**.
- provider 항목 = backend spec에서 `model`만 뺀 것. 에이전트 backend = `{**providers[name], "model": m}` → `build_backend()` 무변경.
- 비밀값은 저장하지 않는다 (env 이름 / TokenStore만). 기존 원칙 유지.
- `load_model_config()`는 `agents`(수동) **또는** `providers`+`planner`(플래너) 둘 중 하나가 유효하면 받아들인다. 둘 다 있으면 플래너 모드가 우선.

## 4. 플래너 호출

### 4.1 catalog

provider별 `wizard._try_list_models()` 결과. `str`만 주는 provider(openai 호환)는 `is_general_purpose_model_id()`로 걸러 embedding/tts 등을 뺀다. provider당 **최대 150개** (프롬프트 길이 상한 — OpenRouter는 수백 개).

사용 가능 판정:
- `nous_oauth`: 유효 토큰 또는 refresh 성공
- 키 방식: `api_key_env`가 현재 프로세스 env에 있음
- 목록 조회 실패 → 해당 provider 제외, 한 줄 안내. 전부 제외되면 중단.

### 4.2 프롬프트 (요지)

- "작업을 해낼 **최소 인원**(1–10명)의 팀을 짜라. 인원을 채우려 하지 마라."
- 모델은 아래 목록의 `provider/model` 만. 가격이 붙어 있으면 핵심 역할엔 강한 모델, 보조엔 싼 모델.
- 출력은 JSON 하나만:

```json
{"agents": [
  {"id": "architect", "provider": "nous_oauth", "model": "Hermes-4-405B",
   "role": "설계와 작업 분할을 맡는다 ...", "reason": "..."}
]}
```

### 4.3 검증 (`parse_plan`, 순수 함수)

| 규칙 | 이유 |
|------|------|
| 응답 본문에서 첫 `{` ~ 마지막 `}` 추출 후 JSON 파싱 | 모델이 ```json 펜스·서두를 붙임 |
| `agents` 1–10개 | 비용 상한 |
| (`provider`, `model`) 쌍이 catalog에 존재 | 없는 모델 이름 지어내기 차단 |
| `id` 비어있지 않음 · 중복 없음 (대소문자 무시) · `RESERVED_NAMES` 아님 | 서버 등록 규칙과 동일 |
| `role` 비어있지 않은 문자열 | `role_custom` 검증(config.py)과 동일 |

실패 → `ValueError(사유)`. `plan_team`은 사유를 대화에 붙여 **1회** 재시도, 또 실패하면 예외를 올린다 (CLI가 에러 출력 후 종료).

`reason`은 확인 화면에만 쓰고 YAML에는 넣지 않는다.

### 4.4 출력 → 세션 YAML

```yaml
max_steps: 0
human: {id: human}
protocol: {...DEFAULT_PROTOCOL}
task: "작업 내용"
agents:
  - id: architect
    role_custom: "설계와 작업 분할을 맡는다 ..."
    backend: {type: nous_oauth, base_url: ..., model: Hermes-4-405B}
bots: []
```

기존 세션 YAML의 수기 키 보존(`_preserve_session_extras`)은 유지하되 **에이전트에 묶인 키(`bots`, `surfaces`)는 버린다** — 이전 팀의 agent_id를 가리키므로 새 팀에서 config 오류가 난다. 같은 이유로 v1 플래너 모드는 wizard에서 Discord bot 바인딩을 묻지 않는다.

## 5. 확인 화면

```
Proposed team (3 agents):
  architect  nous_oauth/Hermes-4-405B   [$1/$3 per 1M tok]   설계 판단이 핵심
  coder      openrouter/qwen3-coder     [$0.2/$0.8 per 1M tok] 구현량이 많음
  reviewer   nous_oauth/Hermes-4-70B    [$0.1/$0.4 per 1M tok] 검토는 중간 모델로
yes = start · no = cancel · anything else = describe changes and re-plan
```

답 해석(`classify_answer`): yes(빈 입력·y·네·예·응 …) / no(n·아니·취소 …) / 그 밖 = **수정 요청**. 수정 요청이면 직전 구성안(JSON)과 요청을 플래너 대화에 덧붙여(`revision_messages`) 다시 짠다 — 요청할 때마다 대화가 2턴씩 늘어난다. 터미널에서 no → 종료(코드 0). Ink에서 no → 새 task 대기.

## 6. 컴포넌트별 변경

| 파일 | 변경 |
|------|------|
| `planner.py` (신규) | `build_catalog`, `build_messages`, `parse_plan`, `plan_team`(async), `plan_loop`(재구성 루프), `classify_answer`, `format_plan`, `to_session_agents` |
| `wizard.py` | `_build_agent`에서 provider 수집부를 `_collect_provider()`로 분리(수동 모드 재사용), setup mode 선택, `_collect_planner_settings()` |
| `model_config.py` | `save_model_config(..., providers=, planner=)`, 로드 검증 확장, `is_planner_config()` |
| `cli.py` | `--plan TASK`, `_run_wizard_flow` 플래너 분기, Ink 경로면 `AUGURY_PLAN_FIRST=1` |
| `gateway/session_stdio.py` | `--plan-first`, `plan_first()` 사전 단계 |
| `fronts/ink` | `gateway.ts` 플래그 전달, `App.tsx` `turn_done(reason=planning)` 이면 resume 안내 생략 |
| `tests/test_wizard.py`, `test_model_listing.py` | 수동 흐름 테스트는 setup mode를 manual로 고정(autouse fixture) |

## 7. 비목표 (v1)

- 세션 도중 에이전트 추가·교체
- 같은 종류 provider 여러 계정
- 모델 강점 등급표 (가격만으로 부족하다는 근거가 생기면)
- protocol 모드 자동 선택, 플래너 모드의 Discord bot 바인딩
- 구성안 직접 편집 UI (자유 텍스트 수정 요청 → 재구성으로 대체)
- Ink에서 거절된 뒤의 세션 YAML은 providers/planner가 남은 "계획 대기" 상태 — 그 파일로 `--headless`(설정 없이)를 돌리면 에이전트가 없어 실패한다. 다시 `agent-augury`로 계획하면 된다.

## 8. 리스크

| 리스크 | 대응 |
|--------|------|
| 플래너가 인원을 과하게 채움 → 비용 | 프롬프트 "최소 인원" + 상한 10 + 확인 화면에 가격 |
| 모델 이름 환각 | catalog 쌍 검증 + 1회 재시도 |
| 플래너 판단 품질이 모델 지식에 의존 | v1 허용. 사용자 확인이 최종 게이트 |
| 기존 저장 설정 호환 | `agents` 전용 파일은 그대로 수동 모드로 동작 |
