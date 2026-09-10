# agent-augury TUI — "Enter 제출 불가" 분석/설계 (agent-4 초안 → 통합본 보조 문서)

> **⚠️ 상위 문서:** `TUI_ENTER_SUBMIT_FIX_DESIGN.md` (agent-2 통합본 v1 — 4인 수렴)가
> **최종 통합 문서**입니다. 본 문서는 agent-4 초안으로, 통합본에 모두 흡수되었으며
> 통합본의 **근거/보조 자료**로만 사용하세요.
> **핵심 내용 (통합본 §0~§8과 동일):**
> - 원인: `input_bar.py`의 `TextArea(multiline=True)`에 `enter` 단독 바인딩 부재 →
>   기본 바인딩(multiline Enter=newline)이 Enter 소비 → `accept_handler` 미호출.
> - 수정(안 B, 1순위): `@kb.add("enter"|"c-j", eager=True)` → `validate_and_handle()`
>   + `_accept`의 reset 선행 제거(`return False`) → history 저장 버그(agent-4 발견) 동시 해소.
> - 안 A(단일 라인)는 사용자가 멀티라인 포기를 선택할 때만 (통합본 §6.1).
> - 위저드 `_prompt_multiline` 키맵 통일 여부 = 사용자 확정 대기 (통합본 §6.2).
> **역사:** 2026-09 · agent-4 초안 작성 → agent-2 통합본으로 흡수 (본 문서는 하위 참조).
