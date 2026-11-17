# 자동 개선 설계 — ② 개선 에이전트(① 분류 `has_todo`)

근거: [[치과 AX - 기능 정의]] §9 "② 자동 개선"·§4 ②, [[치과 AX - 평가 설계]] §2.7, [[치과 AX - AI 실험 기록]] §19, [[치과 AX - 앱 설계]] §3b·§7.2.

## 1. 왜, 무엇을 보여 주나

지금 프롬프트 후보는 사람이 쓰고 관문(`make prompt-gate`)이 거른다. 이번에는 **AI가 재료의 오류를 보고 자기 프롬프트 후보를 쓰고, 같은 관문이 숫자로 거르는 루프**를 만든다.

- **주인공은 루프의 안전함**: 정확도가 오르는 것은 덤이다. `has_todo`는 검증 구간 오답이 같은 프롬프트에서도 16~20으로 흔들리고(§19) 골드도 "원장님께 전달"만 한 통화에서 일관되지 않아 합격을 장담할 수 없다. 탈락 이력("후보 1 응급 새로 놓침 → 탈락, 후보 2 목표 미달 → 탈락, 기준 유지")도 그대로 보여 줄 장면이다
- **반-목표**: 합격할 때까지 관문 값을 바꿔 가며 돌리기, few-shot 넣기, 재료 문장을 프롬프트에 옮기기, 생성기의 버릇에 맞추기

## 2. 범위

- **넣는 것**: `make improve TASK=classify ITEM=has_todo`, 개선 에이전트(하네스), 후보 검사(베끼기·예시·고칠 범위), 관문 연결과 실패 기록 넘기기, 대기 버전과 classify 전환, 관문의 켜진 자동 처리 오류율 조건, `reports/improve_runs.jsonl`, `GET /improve/runs`, AI 평가 화면 "자동 개선" 탭(읽기 전용)
- **넣지 않는 것**(기능 정의 §9 그대로): 프롬프트 밖 재발 방지 경로(`ax_failure_cases`·`GET /failure-cases`), ① 밖 task, 현장(골드 없음) 재료 나누기(설계만), 작업 API `POST /jobs/classify-batch`

## 3. 흐름

`make improve TASK=classify ITEM=has_todo` — `evaluation/`에서만 돈다(과금, 훅 과금 목록에 등록). 평가 설계 §2.7의 규칙을 모두 따르고 아래는 이번에 정한 모양이다.

1. **멈춤 먼저**(LLM 부르기 전): 대기 버전(`ax_settings.pending_prompt_version`)이 있으면, 같은 기준·같은 관문 값의 시도가 `GATE_ATTEMPTS`에 닿았으면 돌지 않는다. 자동 처리가 켜져 있으면 §5의 조건으로 간다(지금처럼 무조건 멈추지 않는다)
2. **재료 모으기**(무료): 기준 = 활성 버전(지금 v2). silver 재료 구간(`evaluation/splits.py` `silver_split`)에서 앱 DB에 저장된 기준 버전 분류(`ax_call_classifications`, `ok`)의 `has_todo`가 골드와 다른 통화. 다시 분류하지 않는다(Claude 판단 — 저장된 것이 곧 기준 버전의 답이고 무료). 입력에 dev·test·hard 행이 있으면 실패
3. **개선 에이전트**(과금): 하네스(`backend/harness/`)로 돈다. 실행 기록·LLM 로그는 `evaluation/runs/improve/`(커밋 안 함)에 — 저장소 주입(앱 설계 §3b). 모델 별칭 `improve`(새로 — 분류 모델보다 상위, Claude 판단으로 Opus 5.5, 폴백 없음)
   - 받는 것: 기준 버전의 `has_todo` 규칙 줄, 오류 통화 목록(통화 id·모델 값·골드 값), 같은 기준의 앞선 탈락 후보 기록(바꾼 규칙·못 넘은 조건 이름 enum만 — §2.7 실패 기록)
   - 도구: 재료 오류 통화 녹취 읽기, 오류 유형 제출(유형 이름·해당 통화 수), 후보 규칙 제출(끝내는 도구). 녹취 읽기는 재료 오류 통화만 허용 — 밖이면 훅이 막는다
   - 고치는 곳은 `has_todo` 규칙 칸 하나뿐(Claude 판단 — 목표 밖 항목이 망가질 길을 막는다). 후보 = 기준 버전 폴더를 복사하고 `labels.md`의 `has_todo` 줄만 바꾼 새 버전. `summary.md`·모델은 그대로
4. **후보 검사**(코드, 관문 전): 재료 녹취·골드와 연속 어절 겹침(§3.1 베끼기 기준 — `evaluation/claim_qa.py`의 어절 수 상수·방식을 재사용), 따옴표 예시 문장 삽입, `has_todo` 줄 밖 변경. 걸리면 관문에 넣지 않고 그 후보는 탈락(조건 이름 `copy`·`example`·`scope`로 기록). 탈락도 시도 1회로 센다 — 같은 기준에 서로 다른 후보 `GATE_ATTEMPTS`개
5. **관문**: `evaluation/prompt_gate.py` `run_gate`를 그대로(목표 항목 `has_todo`). 합격 → `pending_prompt_version` = 후보, 루프 끝. 불합격 → 시도가 남았으면 실패 기록을 들고 3으로(한 번 실행에서 최대 `GATE_ATTEMPTS`개)
6. **기록**: 후보 하나 = `reports/improve_runs.jsonl` 1행(§6)

후보 파일(Claude 판단 — 앱 코드 `backend/classify.py`·`backend/prompts.py`를 고치지 않으려고): 관문 동안 `prompts/classify/<후보>/`에 두고, 탈락하면 `evaluation/runs/improve/`로 옮긴다. 넘은 후보만 `prompts/`에 남는다(커밋은 사람).

시도 수(Claude 판단): 관문 전 검사로 탈락한 후보도 같은 기준·같은 관문 값의 시도로 센다 — `prompt_gate.attempts`가 `improve_runs.jsonl`의 관문 전 탈락도 읽어 수동 `make prompt-gate`와 상한을 함께 쓴다. `GATE_ATTEMPTS` 2라 이번 한 번 실행으로 기준 v2의 시도는 끝나고, 더 돌리려면 사람이 관문 값을 고친다(`GATE_AUTOCLOSE_ERROR_CAP`를 `GATE_VALUES`에 넣어 관문 해시가 바뀌므로 시도는 0부터 센다).

## 4. 전환 — classify 재분류 뒤

앱 설계 §7.2 "classify 전환"을 코드로: `make classify V=<대기 버전>`이 전체 통화를 다시 분류하고 끝났을 때, 그 버전이 `pending_prompt_version`이고 모든 통화가 `ok`면 같은 트랜잭션에서 `active_prompt_version`을 바꾸고 `pending_prompt_version`을 비우고 엇갈림을 다시 계산한다. `failed`가 남으면 바꾸지 않는다. 사람이 실행한다(자동 개선은 재분류를 부르지 않는다).

## 5. 관문에 더하는 조건 — 켜진 자동 처리

평가 설계 §2.7 "관문에 더할 조건". 지금 `run_gate`는 정책이 하나라도 켜져 있으면 멈추는데, 이를 조건으로 바꾼다.

- 켜진 정책이 ① 자동 종결(`call_autoclose`)이면: 후보 다수결 라벨 + 현재 판정기(모델 B)로 검증 구간의 자동 종결을 다시 계산(`backend/autoclose.py` `autoclose_reason`)하고, 종결된 통화 중 골드로 틀린 비율의 **Wilson 95% 상한이 5% 이하**(사용자 결정 — 새 상수 `evaluation/metrics.py` `GATE_AUTOCLOSE_ERROR_CAP = 0.05`, `GATE_VALUES`에 넣는다). 오류 0건을 안전의 증거로 쓰지 않으려고 상한으로 본다(평가 설계 §1 원칙 9). 5%는 지금 표본(검증 405건 중 종결 88건·오류 0 → 상한 약 4.2%)에서 실수 0건이면 통과하고 1건이면 탈락하는 가장 엄격한 둥근 값이다 — 현장에서 허용할 실수율을 따로 정한 것은 아니고, 종결 건수가 늘면 낮출 수 있다
  - 후보의 할 일 목록·요약 폴백은 기준 버전에 저장된 것을 빌린다(Claude 판단 — 후보는 `labels.md`의 `has_todo` 줄만 바뀌고 `summary.md`는 같다, 추가 과금 없음)
- 자동 처리가 켜져 있으면 dev 입력 신뢰 경계 위반 0건(평가 설계 §5): **후보 버전**의 저장된 `evaluation/input_trust` 결과로 본다. 없으면 조건 `trust_unmeasured`로 탈락 — 측정(`make trust-eval`)은 사람이 돌린다(Claude 판단 — 관문 안에서 몰래 과금하지 않게)
- 조건 이름(enum)은 기존 `failed` 목록에 더한다. 지금은 자동 종결이 꺼져 있어 실측 없이 테스트로만 확인한다

## 6. 기록·화면

- `reports/improve_runs.jsonl`(커밋): `at`·`task`·`item`·`base`·`candidate`·`attempt`·`error_types`(유형 이름·통화 수)·`changed_rule`(바꾼 규칙 글)·`gate`(관문 결과 — `passed`·`failed` enum·항목별 집계, 통화 id 없음)·`adopted`(대기 버전이 됐나)·`cost_usd`. 자유 글 칸(`error_types` 이름·`changed_rule`)은 쓰기 전에 §3-4와 같은 겹침 검사, 걸리면 쓰지 않는다(§2.7 누출 차단)
- `GET /improve/runs`: 이 파일을 읽어 돌려준다(backend는 `evaluation/`를 import하지 않고 파일만 읽는다)
- AI 평가 화면 "자동 개선" 탭(`frontend/components/eval/`): 읽기 전용 표 — 시각·후보·오류 유형·바꾼 규칙·관문 결과(못 넘은 조건)·반영 여부. 지금 대기 버전이 있으면 위에 한 줄. 기준 수정·되돌리기는 두지 않는다

## 7. 테스트(가짜 게이트웨이)

기능 정의 §4 ② 완료 기준 + 이번 모양:
- 관문 조건을 하나라도 못 넘으면 대기 버전이 생기지 않는다 / 넘으면 생긴다
- 재분류가 전부 `ok`로 끝나기 전에는 활성 버전이 바뀌지 않는다 / 끝나면 바뀌고 대기가 비워진다
- 입력에 test(또는 dev·hard) 통화가 있으면 실패
- 베끼기·예시·`has_todo` 줄 밖 변경 후보는 관문에 가지 않고 탈락
- 탈락 후보의 기록이 다음 후보 입력에 enum·규칙 글만으로 들어간다(수치·통화 id 없음)
- 대기 버전이 있거나 시도 상한이면 LLM을 부르지 않는다
- 자동 종결이 켜져 있을 때 오류율 조건이 넘으면 탈락, 아니면 통과
- `improve_runs.jsonl`에 통화 id·녹취 조각이 없다
- 녹취 읽기 도구가 재료 오류 밖 통화를 막는다

## 8. 실측과 문서

- 실측(사용자 승인 뒤): `make improve TASK=classify ITEM=has_todo` 한 번 — 예상 $1~2(관문 한 번 약 $0.5 + 에이전트). 결과가 합격이든 탈락이든 그대로 적는다
- [[치과 AX - AI 실험 기록]] 새 절, 기능 정의 §9에서 삭제, §4 ②·평가 설계 §2.7·앱 설계의 "착수 때" 표시 정리, CLAUDE.md 명령 한 줄, 기술 소개 ② "다음 단계" 표시 정리

## 9. 빌더에게 남기는 것

- 에이전트 도구의 정확한 이름·인자, 시스템 프롬프트(새 task `improve`의 버전 파일 `prompts/improve/v1.md`)
- 후보 버전 이름 규칙과 탈락 후보 파일 위치
- 오류 통화가 많을 때 에이전트에 한 번에 줄 목록 크기
