# L3 Passive Awareness Benchmark (agent-3)

> DESIGN.md §6 (v0.1a)의 "L2 vs L3 대조 토이 시나리오"를 **L3 전용 수치 검증**으로
> 승격한 벤치마크. agent-3(벤치마크 트랙) 산출물.
>
> ⚠️ v0.3에서 L2 모드(`wait_for_mention`, 전경 blocking receive)가 제거되어
> "L2 vs L3 대조"는 런타임 차원에서는 더 이상 실행 불가. 따라서 이 벤치마크는
> **"L3가 일을 멈추지 않고 정정을 흡수한다"는 사실을 숫자로 단언**하는 방식으로
> 재정의했다. 논문의 +10.5pt 수치는 arXiv:2607.28430 인용으로 제공한다
> (README 퀵스타트 초안의 AgentRadio 표 참고).
>
> **구현 상태 (2026-09): `run_l3_passive_awareness.py` + `test_l3_passive_awareness.py`
> 구현 완료.** 오프라인·결정적(FakeModelBackend). OSS_STRATEGY §10 D1(벤치마크
> 형태)을 (a) "L3-only 재정의"로 해소하고, CI(`pytest tests/ examples/benchmark/ -q`)에
> 통합됨.

---

## 왜 이 벤치마크인가

패시브 어웨어니스(L3)의 핵심 주장은 두 가지다:

1. **작업이 방해받지 않는다** — 정정이 도착해도 에이전트는 탐색을 계속한다.
2. **정정은 흡수된다** — 다음 스텝 경계에서 자동으로 들어오고 최종 답에 반영된다.

이 벤치마크는 `FakeModelBackend`로 에이전트의 tool 시퀀스를 고정해
**search 호출 횟수**와 **최종 답**이라는 숫자로 위 둘을 단언한다.

## 시나리오 (DESIGN §6 그대로)

```
agent-a: 숫자 탐색 중 (search tool을 N회 반복)
agent-b: "(URGENT) 정답은 42가 아니라 43." 정정 멘션을 agent-a로 push
결과:   agent-a는 search를 N회 이상 수행 AND 최종 답 = 43
        정정은 [radio] 블록으로 step 경계에서 자동 흡수 (전경 wait 없음)
```

## 단언 (숫자)

```python
assert search_steps >= N                 # 작업이 방해받지 않음
assert final_answer == "정답은 43"        # 정정이 흡수됨
assert radio_seen                        # push → [radio] 삽입 확인
assert final_answer_after_radio          # 정정 이후에 답이 나옴 (흡수 순서)
assert agent_a.polled == 0               # 명시적 폴링/대기 도구 사용 안 함 (push-only)
```

- `agent_a.polled == 0`: agent-a가 `read_resource`(명시적 상태 덤프/폴링)를
  전혀 호출하지 않았음을 확인 → 수신이 전경 폴링이 아니라 **inbox push**임을 증명.

## 실행

```bash
# 스크립트 (수치 단언 + 출력)
python examples/benchmark/run_l3_passive_awareness.py

# pytest 버전 (CI 통합용 — 오프라인, fake 백엔드)
pytest examples/benchmark/ -q
```

### CI 통합 (ci.yml에 반영 완료)

```yaml
- name: Run offline test suite
  run: pytest tests/ examples/benchmark/ -q
```

## 구현 세부 (agent-3, 2026-09)

- `run_l3_passive_awareness.py`:
  - `run_l3_scenario(search_rounds=N)` — N회 search + 정정 흡수 + 최종 답 시나리오를
    결정적 인터리빙으로 실행하고 메트릭 dict를 반환.
  - `assert_benchmark(metrics, search_rounds)` — 위 5개 단언을 한 번에 실행.
  - `main()` — `python examples/benchmark/run_l3_passive_awareness.py` 실행 시
    숫자 출력 + 단언 + ✅ PASSED.
- `test_l3_passive_awareness.py`: 5개 pytest 테스트 (radio 블록 삽입, 흡수 순서,
  push-only, search_rounds 확장) — CI 통합용.

## 현재 테스트가 이미 이 역할을 하는가? (agent-3 검증 결론)

| 테스트 | 역할 | L2/L3 대조인가? |
|--------|------|------------------|
| `tests/test_toy.py` | `LocalTool`(search) 메커니즘 단위 테스트 | ❌ (로컬 도구 동작만) |
| `tests/test_parallel.py` | 병렬 실행 / max_steps / 장애 격리 | ❌ |
| `tests/test_regression.py` | tool_calls 보존, 게이트 블로킹, READY P1 정책 | ❌ |
| `examples/demo.yaml` | L3 시나리오 **데모** (텍스트, 단언 없음) | ⚠️ 시나리오는 맞으나 **수치 단언 없음** |
| `examples/benchmark/` (신규) | L3 전용 수치 단언 (5개 pytest + 스크립트) | ✅ (L3-only 재정의) |

→ **기존 테스트는 L2/L3 대조 벤치마크가 아니다.** `demo.yaml`이 가장 가깝지만
search 횟수/최종 답 단언이 없고, search tool이 아니라 텍스트("searching...")다.
`examples/benchmark/`가 그 빈칸을 메운다.

## L2 대조 암 (선택, Phase 2.5)

L2 모드는 v0.3에서 제거됐지만, "전경 대기 없이 push로만 흡수"가 실제로
L2(블로킹 수신)보다 작업을 덜 방해한다는 것을 보여주고 싶다면 서버 레벨
시뮬레이션으로 재구성할 수 있다 (향후 작업):

- L2-유사 암: agent-a의 inbox push를 끄고, 정정을 알기 위해 `read_resource` 폴링을
  명시적으로 호출하도록 스크립팅 → `search_count`가 push 암보다 작거나,
  정정 흡수까지의 wall-step이 더 큼.
- 이는 `MessageServer`에 "push 억제 플래그"를 추가하는 코드 변경이 필요.
  Phase 2 로드맵 항목으로 남겨둔다 (필수 아님 — 논문 수치로 대체 가능).
