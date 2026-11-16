# 야간 실행(`make nightly`) + ⑦ 아침 브리핑(템플릿) 설계

## 1. 왜

- 지금은 기능마다 `make classify`·`prep`·`claim-check`·`recon`…을 손으로 돌려야 화면이 채워진다. `make nightly` 한 번으로 모든 화면의 AI 결과를 최신으로 만든다 — "아침에 화면을 열면 다 채워져 있다"
- 오늘 화면 "AI가 한 일"의 끝난 시각·실패 꼬리표(기능 정의 §4 오늘 화면 "`make nightly`의 `ax_jobs` 때")를 여기서 닫는다. 하네스 남은 일(재개 횟수 상한·죽은 호출 비용 맞추기·`ax_settings.daily_usd_limit` 연결 — 기능 정의 §9)은 이번 범위가 아니다
- ⑦ 아침 브리핑은 오늘 화면의 한 칸. 요약 카드·"사람이 볼 일"에 이미 있는 숫자는 되풀이하지 않고 **목록에 안 보이는 하루의 모양과 밤사이**만 담는다. 아침 회의에서 읽어 주기도, 데스크가 혼자 훑기도 한다

## 2. 목표 · 비목표 · 반목표

**목표**
- `make nightly` — 앱 설계 §3b "스케줄러" 고정 순서를 코드로(에이전트 없음). 각 단계는 기존 `make` 명령의 **기본(화면) 범위**
- `make nightly PLAN=1` — LLM을 부르지 않고 단계별 "새로 부를 건수"만 출력(첫 실행·버전 교체 전 과금 확인)
- 실행 기록: `ax_jobs` `kind='nightly'` 한 행 + 단계별 요약 → 오늘 화면 "AI가 한 일"·브리핑 "밤사이"가 읽는다
- ⑦ 브리핑: 코드 템플릿 네 줄(§4), `GET /today` 응답에 넣고 오늘 화면에 카드

**비목표**
- 예약 실행 등록(cron·launchd) — 명령까지만. 실제 밤마다 깨우는 것은 병원 PC 몫
- 운영 에이전트, ④ 주의 환자 야간 조사 에이전트, ⑨ 2단계(보류 — 기능 정의 §9)
- 브리핑 LLM 문장·`ax_briefings` 테이블 — LLM을 붙일 때
- 작업 API(`POST /jobs/...`)·화면에서 야간 실행 시작 버튼, `POST /agents/…/run`
- 평가(`evaluation/`)·test 분할·관문 실행 — nightly는 앱 화면 범위만

**반목표**(기술적으로 돌아가도 실패)
- 사람 모르게 큰돈을 쓴다 — 첫 실행·버전 교체 뒤 몰아 과금. `PLAN=1`과 하루 상한, 그리고 아래 "Batch는 상한 밖" 주의로 막는다
- 브리핑이 "사람이 볼 일" 숫자(급한 통화·주의 환자·할 일 수)를 문장으로 다시 읽는다. 예외: "오늘" 줄의 예약·신환 수는 읽어 주는 장면의 맥락이라 요약 카드와 겹쳐도 둔다
- 한 단계 실패가 뒤 단계를 막아 아침 화면이 반쯤 빈다
- `source.sqlite`에 쓰거나 `backend/`가 `evaluation/`·`generator/`·`ground_truth/`를 import한다(CLAUDE.md 절대 규칙)

## 3. 야간 실행

**모듈**: `backend/nightly.py`(`python -m backend.nightly run [--plan] [--no-batch]`), Makefile `nightly`(`PLAN=1`·`NO_BATCH=1`). Qwen 판정기가 돌아야 하므로 `uv run --group finetune`(make dev와 같음).

**순서**(앱 설계 §3b, 운영 에이전트 착수 전 고정 순서). 각 단계는 기존 모듈 함수를 프로세스 안에서 부른다(서브프로세스 아님).

| # | 단계 | 부르는 것 | 범위 |
|---|---|---|---|
| 0 | 잠금 | `ax_jobs` `kind='nightly'` `running`이 `NIGHTLY_STALE`(`backend/nightly.py`, 화면 작업의 `api.JOB_STALE` 30분보다 길게 — Batch 대기·Qwen 판정이 길다) 안이면 시작하지 않고 끝 | — |
| 1 | 소스 지문 | `backend/harness/policy.py` `check_source` | 바뀌었으면 자동 처리 정책 전부 끔 |
| 2 | 분류 | `classify_batch.run` classify — 활성 분류 버전(`calls.active_versions`) | ok 아닌 생성 통화(판정 풀 포함 — `make classify`와 같고 앱 설계 §3b "판정 풀은 엇갈림까지만") + 활성 분류 ok가 없는 붙여넣은 통화(`calls.classify_one`) |
| 3 | 판정 | `classify_batch.run` judge — 활성 판정기 버전 | 같음 |
| 4 | 엇갈림 | `classify_batch` disagreements 경로 + 판정 큐 갱신(`judge_queue.refresh_if_active`) | 활성 버전 쌍 |
| 5 | 자동 종결 | `autoclose.run` | 정책이 꺼져 있으면 아무것도 안 함 |
| 6 | 후속 처리안 | `followup` run 경로(`targets`, `drop_stale`은 부르지 않음 — `make followups` 기본과 같음. 처리안 버전을 올린 다음 날 몰아 과금하지 않게, 다시 내기는 사람이 `REDO=1`) | 오늘 할 일에 오른 통화 중 처리안 없는 것 |
| 7 | 색인 갱신 | `rag_index` — 청크 수집에 붙여넣은 통화(`ax_pasted_calls`)를 더하고 `build_index`(청크·FTS 다시 쓰기, 벡터는 없는 청크만 — 비용은 새 청크 임베딩뿐) | 색인이 없으면 건너뜀. 임베딩 모델은 지금 검색이 쓰는 모델 |
| 8 | 준비표 | `prep.build` | `prep.tab_days`(make prep 기본) |
| 9 | 청구 점검 | `claim_check` → `claim_omission` → `claim_evidence` | 각 모듈 기본(화면) 범위 |
| 10 | 일계표 조사 | `recon_agent.run_batch` | 기본(전 기간 — 화면 달 전부) |
| 11 | 지출 증빙 | `expense_agent.run_batch` | 같음 |
| 12 | 리포트 | `report.build` | 화면 기간 전부 |

- 브리핑은 단계가 아니다 — 템플릿이라 `GET /today`가 그때 계산한다(§4)
- **다시 부르지 않음**: 모든 단계는 기존 캐시(해시·버전·모델이 같고 통과한 건 건너뜀)를 그대로 쓴다. 그래서 둘째 실행부터는 새로 들어온 통화·바뀐 입력만 처리한다
- **이어 가기**: 따로 장치를 두지 않는다. 끊긴 nightly는 잠금이 `JOB_STALE`로 풀린 뒤 다시 돌리면 캐시가 이미 한 건을 건너뛴다. 끊긴 에이전트 실행은 각 모듈의 `_close_stuck`이 정리한다
- **단계 실패**: 한 단계의 예외(`SystemExit` 포함 — `classify_batch.run`은 판정기 모델이 맞지 않으면 `SystemExit`)는 그 단계에 기록하고 다음 단계로 간다(반목표 "반쯤 빈 화면")
- **하루 상한**: 하네스·`calls.add_call`이 `BudgetExceeded`를 안에서 삼키므로(`handed_off` `reason=budget`, 라운드 `break`) 예외로 알아채지 않는다. 단계가 끝날 때마다 게이트웨이의 그날 사용액(`spent`)을 상한과 비교해 넘었으면 남은 단계를 건너뛰고 "상한으로 중단"·`failed`를 남긴다
- **Batch 기본**: 밤에 도는 일이라 결과 대기는 문제가 아니므로 각 모듈 기본대로 Batch(반값), `NO_BATCH=1`이면 일반 호출만
- **Batch는 상한 밖**: 게이트웨이의 일일 상한은 즉시 호출에만 걸린다(`config/llm.yaml` 주석 "배치 제출은 상한 밖"). Batch를 쓰는 단계는 상한이 막지 못하므로 `PLAN=1`이 유일한 사전 확인이다 — `PLAN=1` 출력에 이 사실을 한 줄로 보인다
- **잠금 범위**: 잠금은 nightly끼리만 막는다. 화면의 준비표 갱신·청구 점검 작업(`ax_jobs` `prep`·`claim_check`)과는 각 모듈 캐시·중복 방지 키로 겹쳐도 같은 건이 두 번 저장되지 않는다는 기존 보장에 맡긴다

**`PLAN=1`**: LLM·임베딩을 부르지 않고 단계별 대상 수만 출력하고 `ax_jobs`에 쓰지 않는다. 대상 수는 각 모듈이 실제 실행 때 고르는 함수와 **같은 함수**로 센 **지금 시점의 수**다(예: `classify_batch.targets`, `claim_check.todo_ids`, `recon_agent.targets`). 그런 함수가 없는 단계(준비표·리포트의 "다시 부를 행/기간")는 그 모듈에 대상 함수를 꺼내 실행 경로와 함께 쓴다. 앞 단계 결과에 기대는 단계(판정·엇갈림·자동 종결·후속 처리안)는 "앞 단계 뒤 늘 수 있음"을 붙인다 — 첫 실행에서 큰 단계(준비표·청구·일계표·지출·리포트)는 앞 단계와 무관해 정확하다.

**기록**: `ax_jobs`에 `kind='nightly'` 한 행 — `status`(`running`→`done`, 상한 중단이면 `failed`), `done`/`total`은 단계 수, 시작·끝 시각은 실제 시계. 단계별 요약은 새 칸 `detail`(JSON 목록 `{step, new, failed, note}` — `new`는 이번에 새로 만든 건수, `note`는 "건너뜀: 색인 없음"·"상한으로 중단" 같은 한 줄)에 둔다. `shared/app_schema.sql` `ax_jobs`에 `detail TEXT` 추가(`db.init_schema`의 칸 추가 경로가 기존 DB에 넣는다).

**출력**: 터미널에 단계마다 한 줄(새로 만든 건수·실패·걸린 시간)과 마지막에 총비용(`ax_llm_calls` 이번 실행 구간 합).

## 4. ⑦ 아침 브리핑(템플릿)

**모듈**: `backend/briefing.py` `lines(conn, today) -> list[{"head", "text"}]`. `backend/today.py` `sheet`가 `briefing`으로 붙인다. LLM을 부르지 않는다. 입력은 `prep.prep_facts(conn, today, today)` 행과 마지막 nightly `ax_jobs` 행.

| 머리말 | 내용 | 규칙 |
|---|---|---|
| 오늘 | `예약 26건(신환 4) · 10시·14시·17시대에 몰림` | 예약·신환은 준비표 행 수와 `new_patient`(요약 카드와 같은 함수). 몰리는 시간대 = 시작 시각의 시(hour)별 예약 수가 그날 최댓값인 시간대(동률 전부, 최대 `BUSY_TOP`개) |
| 큰 시술 | `사랑니 발치 9:30 · 단순발치 10:30 · 임플란트 1차 15:30` | 시술 분류(`emr_procedures.category`)가 `BIG_CATEGORIES`(surgery·implant)인 예약, 시각순, 최대 `BIG_MAX`개(넘으면 "외 n건") |
| 챙길 환자 | `10:30 배정웅 단순발치 — 항응고제 복용` (한 명에 한 줄) | 큰 시술이면서 준비표 `alert` 칩(그 시술과 관련된 주의사항 — `alert_rules`)이 켜진 예약, 시각순 최대 `CARE_MAX`명. 주의사항은 관련된 것만 이름으로 |
| 밤사이 | `통화 5건 분류 · 자동 종결 3 · 실패 0 (03:12 끝남)` | 마지막 nightly 행의 `detail` — 분류 단계 `new`, 자동 종결 단계 `new`, 단계 `failed` 합. 급한 통화는 "사람이 볼 일"에 있어 넣지 않는다. 끝난 지 `NIGHT_HOURS`(실제 시계) 안일 때만. 상한 중단이면 "상한으로 중단"을 붙인다 |

- 해당하는 것이 없는 줄은 숨긴다. 네 줄이 다 없으면 카드를 숨긴다
- 상수(`BUSY_TOP`·`BIG_CATEGORIES`·`BIG_MAX`·`CARE_MAX`·`NIGHT_HOURS`)는 `backend/briefing.py`에 이름으로
- 표시: 코드가 계산한 것이라 `AI` 배지를 달지 않는다(기능 정의 §4 "표시 세 종류")
- 화면: 오늘 화면 요약 카드 아래 한 칸, 왼쪽 머리말·오른쪽 내용 2열(와이어프레임 `index-v2.html` 브리핑 카드 자리, 브레인스토밍 시안 `briefing-v2`). 부품은 `components/ui`, 색은 토큰만

## 5. 문서 반영

- 기능 정의 §5 ⑦: "LLM은 몇 문장으로 묶기만" → 1단계 템플릿(이 설계 §4), LLM 문장은 2단계. 완료 기준은 1단계 — 규칙별 회귀 테스트, 2단계 — 지금 기준 + §9의 쓸모 지표
- 기능 정의 §9: 착수 순서 줄을 "`make nightly` + ⑦ 1단계" 끝남으로 정리, 운영 에이전트·⑦ LLM·⑨ 2단계는 보류 이유와 함께. 새 통화 색인 항목 지움
- 앱 설계 §3b 스케줄러: 순서 표는 이 문서가 아니라 `backend/nightly.py` 독스트링을 가리키게, `PLAN`·실패 시 계속·Batch 상한 밖 한 줄씩
- 앱 설계 §4 오늘 화면: `ax_briefings`는 ⑦ LLM 때, 지금은 `backend/briefing.py`
- `CLAUDE.md` 명령에 `make nightly [PLAN=1] [NO_BATCH=1]`(과금, 이어 돎)

## 6. 테스트(먼저 쓴다)

- `tests/backend/test_briefing.py` — 줄별 규칙(몰림 동률, 큰 시술 상한 "외 n건", 챙길 환자 = 큰 시술 ∩ alert, 관련 없는 주의사항은 이름에 안 나옴, 빈 줄 숨김, 밤사이 시간 창). 작은 fixture DB
- `tests/backend/test_nightly.py` — 가짜 게이트웨이로: 단계 순서, 잠금(살아 있는 running이면 시작 안 함·오래된 것은 무시), 실패 없는 실행 뒤 두 번째 실행의 `new`가 전부 0, 한 단계 예외(`SystemExit` 포함) 뒤 다음 단계 진행, 단계 뒤 사용액이 상한을 넘으면 남은 단계 건너뜀·`failed`, `PLAN`은 LLM 호출 0·`ax_jobs` 쓰기 0이고 앞 단계와 무관한 단계의 대상 수가 실제 실행의 `new`와 같음
- `tests/backend/test_today.py` — `briefing` 칸, "AI가 한 일" 끝난 시각·실패 꼬리표
- 화면: 오늘 화면 E2E에 브리핑 카드 한 줄 확인

## 7. 만드는 사람 몫

- 단계 함수들을 부를 때의 인자 맞추기(각 모듈 `main`이 하는 일을 함수로 꺼낼지)
- 붙여넣은 통화 색인을 `rag_index`에 넣는 방식(청크 id `call:p-0001:n` 꼴), 지금 검색이 쓰는 임베딩 모델을 찾는 곳
- `PLAN` 대상 함수가 없는 모듈에 꺼낼 함수 이름
- 터미널 출력 모양
