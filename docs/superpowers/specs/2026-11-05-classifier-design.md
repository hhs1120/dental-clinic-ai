# ① 분류기·판정기 설계

기능 정의 §9 착수 순서의 첫 항목(① 분류기·판정기·평가 정답)이다. 규칙의 원본은 다음과 같다.
- `docs/치과 AX - 기능 정의.md` §4 ①(목적·완료 기준·교차검증)
- `docs/치과 AX - 앱 설계.md` §1(분류·판정 테이블), §7.1(게이트웨이), §7.2(프롬프트)
- `docs/치과 AX - 평가 설계.md` §2.2~§2.6(지표·플래그 판정 기준·블라인드 비교·교차검증)
- `docs/치과 AX - AI 실험 기록.md` 7·8(프롬프트 초안 v1, 파인튜닝 데이터)

이 문서는 이번 범위와 원본에 없는 결정만 적는다.

## 목적

통화 녹취록마다 분류기(모델 A)가 라벨 9항목과 요약·할 일을 내고, 판정기(모델 B)가 같은 통화의 라벨을 따로 낸다. 두 라벨이 엇갈린 통화만 사람이 보게 하는 것이 ①·②의 뼈대다. 이번에는 두 모델이 전체 통화를 분류·판정하고, 엇갈림을 계산하고, `eval.py`가 골드로 정확도와 교차검증 지표를 내는 데까지 만든다. 그다음 같은 평가로 파인튜닝한 Qwen을 분류기 라벨 자리 후보로 잰다.

## 범위

**이번에 만든다**
- LLM 게이트웨이에 Upstage(Solar) 호출 경로
- 분류기: `classify(gw, transcript, direction, version)`(통화 입력은 녹취록·방향뿐) — 라벨 호출 + 요약·할 일 호출(둘 다 Haiku), 결과는 `ax_call_classifications` 1행
- 판정기: `judge(transcript, direction)` — Solar 라벨 호출, 결과는 `ax_model_judgments` 1행
- 엇갈림 계산: `ax_call_disagreements`
- 일괄 실행: 전체 통화를 분류·판정하는 명령(아래 "실행")
- `eval.py`로 dev 채점(분류기·판정기 vs 골드, 교차검증 지표)
- Qwen 파인튜닝 비교(아래 "Qwen 비교") — 학습·평가 코드는 `evaluation/finetune/`
- 문서 반영(아래 "문서 변경")

**이번에 안 만든다**
- 화면(통화 목록·후속 큐·상세·붙여넣기 폼·② 판정 화면)과 그 API. 다음 브랜치(착수 순서 "통화·② 판정 화면")
- HTTP 작업 API(`POST /jobs/classify-batch`·`judge-batch`)와 `ax_jobs`. 진행률을 보여 줄 화면과 함께 만든다 — 내 판단: 이번에는 같은 함수를 명령으로 부른다
- 후속 에이전트·자동 종결·`truth_call_actions`·시나리오 DB. 쓰는 곳이 후속 에이전트라 그 브랜치로
- 골드 점검 화면(`make gold-audit`). 착수 순서상 화면 뒤
- 자동 개선 루프와 블라인드 비교 실행. 표 칸과 요약 형식은 이번에 맞춰 둔다
- 다른 오픈 모델 비교, 세 계열 다수결 — 후순위(기능 정의 §9에 기록)
- `make fixture` — 쓰임이 화면(판정 큐·검색)이라 그 브랜치로

**실패로 보는 것**
- 분류기·판정기가 녹취록과 방향 밖의 정보(환자·번호·시각·EMR)를 보는 것
- backend가 `ground_truth/`·`generator/`·`evaluation/`을 import하는 것, 골드가 `data/`·`app.sqlite`에 들어가는 것
- Qwen 학습이나 프롬프트 수정에 test 분할이나 test 녹취록이 쓰이는 것
- 할 일 목록이 적힌 통화가 엇갈림 없이 지나가는 것(아래 "엇갈림")

## 모델 배치

| 역할 | 모델 |
|---|---|
| 분류기 라벨 | Haiku |
| 분류기 요약·할 일 | Haiku |
| 판정기 라벨 | Qwen3.5-2B full(로컬, `judge v3`) — 옛 판정기 v1·v2는 Solar Mini 4 |

Qwen은 판정기 자리에 둔다. 분류기 라벨 자리에서는 Haiku와 대등할 뿐이지만, 판정기 자리에서는 Haiku 오류 포착이 크게 오른다(실험 기록 15 "추가 분석"). 앱에서 돌리는 방법·학습 통화 빼기는 `docs/superpowers/specs/2026-11-09-qwen-judge-design.md`.

- 비교 규칙은 평가 설계 §2.5 "비교는 한 번에 한쪽만 바꾼다". 판정은 통화마다 저장돼 분류기 버전에 묶이지 않으므로 판정기를 바꿔도 분류기 비교는 새 판정기로 다시 낼 수 있다
- 분류기 라벨(Haiku)과 판정기(Qwen)는 계열이 달라 교차검증 지표를 낸다(평가 설계 §2.5 "같아진 구성은 내지 않는다"에 걸리지 않음)
- 요약·할 일은 분류기 쪽에 둔다. 블라인드 비교(평가 설계 §2.4)가 분류기 `prompt_version`끼리 요약을 비교하기 때문이다. 판정기 호출과 합치지 않는다
- 착수 전 확인(Solar 맛보기): Solar Mini를 실험 기록 7의 v1 원문 그대로 dev 75건과 silver 1,350건에 돌려 같은 조건의 Haiku v1(silver 목적 1328/1350)과 비교한다(약 $0.4). 원문으로 돌리는 이유는 Haiku 수치와 같은 조건으로 비교하기 위해서다. 다음 중 하나라도 해당하면 자리를 바꾼다 — 분류기 라벨 Haiku, 판정기 Solar(이 문서의 배치 표와 별칭만 바뀐다)
  - silver 목적 Strict가 Haiku보다 2%p 넘게 낮다
  - silver 플래그 6개 평균 정확도가 Haiku보다 2%p 넘게 낮다
  - 결과: Solar Mini 4 silver 목적 0.807·플래그 0.926, Haiku 0.984·0.990 — 둘 다 걸려 자리를 바꿨다(분류기 라벨 Haiku·판정기 Solar — 실험 기록 9, 지금 배치는 위 표). 추론 low로도 목적 −2.6%p라 그대로(실험 기록 11)

## 분류기

- 시그니처 `classify(gw, transcript, direction, version) -> Classification` — 통화 입력은 녹취록·방향뿐(앱 설계 §1). 호출 두 번
  1. **라벨**(Haiku): 9항목. 구조화 출력(JSON 스키마). enum은 `generator/master.py`가 원본이지만 backend는 `generator/`를 import할 수 없으므로, 스키마 enum은 `shared/`의 상수로 옮겨 두 곳이 같은지 테스트로 비교한다 — 내 판단
  2. **요약·할 일**(Haiku): 요약 1개와 할 일 목록
- 요약 형식은 평가 설계 §2.4 그대로: 마크다운·글머리 금지, 명사형 종결(~함/~음), 공백 포함 80~110자 단일 문단, 코드 후처리로 줄바꿈·기호 정규화
- 할 일: "누가 무엇을" 문장 목록. 없으면 빈 목록
- 두 호출 중 하나라도 실패하면 `status = failed`
- 저장(`ax_call_classifications`): `labels`는 라벨 호출, `summary`·`todo`는 요약 호출. 칸 추가 — `summary_model_actual`, `summary_llm_call_id`(요약 호출, `ax_llm_calls` 참조). 기존 `model_actual`·`llm_call_id`는 라벨 호출이다(앱 설계 §1 "나머지 칸만 착수 때 추가")
- 폴백 제외: 라벨 채점·교차검증은 라벨 호출의 폴백을, 요약 블라인드 비교는 요약 호출의 폴백을 뺀다. `ax_settings.active_models`에는 요약 모델도 넣는다 — 내 판단
- 프롬프트: `prompts/classify/<version>/labels.md`·`summary.md`. 폴더명이 `prompt_version`이고 두 파일을 함께 가리킨다. 둘 중 하나의 텍스트나 모델이 바뀌면 버전을 올린다 — 내 판단(앱 설계 §7.2의 "파일명이 곧 버전"을 classify만 폴더로)
- 첫 버전 `v1`: 라벨은 실험 기록 7의 v1(`evaluation/finetune/aug/classify_v1.md`)에 아래 `has_todo` 기준을 더한 것. silver 오답을 보고 고친 프롬프트라는 점은 실험 기록 7에 있다

## 판정기

- 시그니처 `judge(transcript, direction) -> Judgment` — 입력 제한은 분류기와 같다
- Solar 한 번, 라벨 9항목만. 판정 기준은 평가 설계 §2.3 플래그 판정 기준(원본)을 따른다
- 프롬프트 `prompts/judge/v1.md`는 분류기 v1 문장을 옮기지 않고 평가 설계 §2.3과 라벨 정의(가상 데이터 생성 설계 §4.1)에서 따로 쓴다. 같은 문장이면 두 모델이 같은 쪽으로 틀려 교차검증 지표(오류 포착률)가 낮아진다. 판정 기준 자체(`has_todo` 기준, 주 목적 = 처음 꺼낸 용건 등)는 두 프롬프트에 같게 들어간다
- 저장은 `ax_model_judgments`(칸 그대로)

## `has_todo` 기준

직원이 "원장님께 전달하겠다"고만 약속한 통화도 `has_todo = 1`이다. 평가 설계 §2.3 정의의 "전달"에 해당하고, 놓치면 할 일이 자동 종결로 사라지기 때문이다. 분류기·판정기 프롬프트와 평가 설계 §2.3에 같은 문장을 넣는다. 골드는 이 경우에 일관되지 않다(실험 기록 8 dev 점검: c-0019·c-0792는 0, c-0130·c-0519는 1) — 점수 한계로 적는다.

## 엇갈림

- 앱 설계 §1 그대로: 9항목 중 하나라도 A ≠ B면(급함은 urgent 여부만 — 평가 설계 §2.5) `(call_uid, a_version, b_version)` 1행, 목적·급함·응급이 다르면 `is_core`, 어느 쪽이든 폴백이면 표시
- 추가 규칙: 분류기 할 일 목록이 비어 있지 않은데 분류기 라벨 `has_todo = 0`이면 판정기와 같아도 엇갈림으로 본다. 라벨과 할 일 목록을 다른 모델이 내므로 할 일이 적힌 통화가 자동 종결되지 않게 한다 — 내 판단. 이 경우를 구분하는 칸을 둔다(`todo_mismatch`)
- 테이블 `ax_call_disagreements`는 이번에 `shared/app_schema.sql`에 만든다

## 게이트웨이

- `shared/llm.py`에 Upstage 경로를 더한다. `config/llm.yaml`의 모델 항목에 프로바이더를 적고(`provider: anthropic | upstage`, 없으면 anthropic), 별칭은 지금처럼 모델 순서다. 기록·비용·일일 상한·`FakeGateway`는 공통이다
- Upstage는 OpenAI 호환 API다. SDK는 Upstage 문서가 안내하는 방식을 따르고 의존성은 `backend`·`generator` 그룹에 넣는다. 정확한 모델 id·단가·구조화 출력 방식·Batch 지원은 착수 전 확인(Solar 맛보기와 함께)에서 정한다
- 별칭: `classify: [claude-haiku-5-5, claude-sonnet-5-5]`, `summarize: [claude-haiku-5-5, claude-sonnet-5-5]`, `judge: [solar-mini4]`(Upstage 하나라 폴백 없음). 폴백 결과는 채점에서 빠진다(평가 설계 §1) — 내 판단
- 키는 `.env`의 `UPSTAGE_API_KEY`, `.env.example`에 빈 칸 추가

## 실행

- 분류·판정 함수는 backend 모듈(예: `backend/classify.py`)에 두고, 일괄 실행은 그 함수를 부르는 명령으로 한다(`make classify`·`make judge`, 대상은 생성 통화 전체 — 붙여넣기 통화는 화면과 함께). 버전·대상 범위·재시도는 인자로 — 세부는 구현자
- Haiku 일괄은 게이트웨이 Batch(`submit_batch`)로 돌린다(단가 절반). Upstage Batch가 없으면 Solar는 일반 호출로 돈다
- 예상 비용(1,500건): Haiku 라벨 약 $0.3(Batch), Haiku 요약 약 $0.3(Batch), Solar 판정 약 $0.2. 런타임 일일 상한($15) 안
- 결과는 `app.sqlite`. 재실행은 UNIQUE `(call_uid, model_alias, prompt_version)`로 이미 `ok`인 건을 건너뛴다

## fixture

`make fixture` — 쓰임이 화면(판정 큐·검색)이라 그 브랜치로. 이번 테스트는 작은 합성 DB로 한다

## 평가

- 기존 `evaluation/classification.py`가 읽는 칸을 그대로 채운다. `eval.py --split dev`로 분류기·판정기 vs 골드(항목별, 목적 Strict 가중·단순), 교차검증 지표(일치 오류율·오류 포착률)를 낸다
- `--section auto`(자동 종결 오류율)와 정확도 뷰 정의는 자동 종결과 함께 — 다음 브랜치
- 급함 normal/low는 골드가 추첨이다(실험 기록 7). 지표는 그대로 내되 보고에 한계로 적는다

## Qwen 비교

- **silver 재료·검증 분할을 이번에 고정한다**: `eval.py --split dev` 모수에 silver가 들어가고(`evaluation/classification.py` `_gold_ids`), 자동 개선 관문은 silver의 검증 구간으로 판정한다(평가 설계 §2.7). silver 전부로 학습하면 Qwen이 본 통화로 Qwen을 재고, Qwen이 분류기가 되면 관문 구간도 오염된다. 그래서 평가 설계 §2.7의 층화 분할(층 키 `(희귀 목적 묶음, emergency, complaint)`, 층 안 `call_uid` 해시 정렬)을 **재료 70 : 검증 30**으로 이번에 고정하고, 값은 `evaluation/metrics.py` 상수로 둔다. 분할 함수는 자동 개선과 Qwen이 같이 쓴다
- 학습 재료: 재료 구간 silver 원본 중 `original_wrong`이 아닌 936건 + 그 원본의 `train = true` 바꿔 쓰기 2,419건 = 3,355건(어림 — 분할 함수로 다시 센다). 검증 구간 통화는 원본도 변형도 쓰지 않는다. dev·test 녹취록은 쓰지 않는다
- 급함은 3값 그대로 학습한다(합치거나 빼면 다른 모델과 같은 칸으로 비교할 수 없다). 비교는 urgent 여부를 주로 보고 3값 결과를 함께 적는다. normal/low는 골드가 추첨이라는 한계를 적는다
- 평가 셋
  - 검증 구간 silver 약 405건: 처음 보는 통화로 정확도 비교(교체 기준 1·2)
  - dev 75건과 문체가 다른 dev 225건(`dev_eval.jsonl`, 학습 금지): 희귀 목적(검증 구간에는 몇 건뿐이라 dev 판정 풀의 과표집을 쓴다)과 문체 견고성(교체 기준 3). 벌별 점수와 평균
  - 같은 셋에서 Solar·Haiku 라벨과 나란히 낸다(문체 dev는 두 모델도 추가로 돌린다)
- 재료가 충분한지: 재료 구간의 절반과 재료 구간 전부로 한 번씩 학습해, 둘 다 같은 평가 셋(검증 구간·dev·문체 dev)으로 비교한다. 검증 구간은 어느 쪽에도 들어가지 않는다. 거의 같으면 충분하고, 전부 쓴 쪽이 확실히 높으면 재료 구간 희귀 통화만 바꿔 쓰기를 더 만들어 보강한다(Haiku, 같은 사람 검토). 2B 결과는 전부 쓴 쪽이 높았지만 보강은 하지 않았다(실험 기록 15)
- 환경: 2B부터 로컬 Mac(MLX)으로 시도하고, 안 되면 GPU 클라우드. 0.8B·4B는 2B 결과를 보고 정한다
- 위치: 학습·평가 코드는 `evaluation/finetune/`(골드를 읽으므로 backend 밖). 가중치는 저장소에 넣지 않는다
- **교체 기준**(학습 전에 고정 — 셋 모두 통과해야 분류기 라벨 자리를 Qwen으로 바꾼다)
  1. 검증 구간 silver에서 목적 Strict가 지금 분류기 라벨 모델보다 2%p 넘게 낮지 않다
  2. 같은 곳에서 플래그 6개 평균 정확도가 2%p 넘게 낮지 않다
  3. 문체 dev 3벌 중 가장 낮은 벌의 목적 Strict가 원본 dev보다 5%p 넘게 떨어지지 않는다(문장 모양을 외운 것인지 확인)
  - 2%p는 검증 405건의 측정 오차(±1%p 남짓)보다 커서 우연한 차이와 구분되는 크기, 5%p는 말투 변화로 조금 떨어지는 것은 허용하고 크게 무너지는 것만 잡는 선이다
- 결과는 실험 기록에 문제→원인→시도→결과→한계로

## 문서 변경

- 앱 설계 §7 "모델 (확정)" 줄: ① 분류기 라벨·요약·할 일 Haiku, 판정기 Solar Mini 4. 프로바이더 Anthropic·Upstage
- 앱 설계 §7.1: 게이트웨이가 두 프로바이더를 쓴다(변환 계층 없이 각 SDK 직접)
- 앱 설계 §7.2: classify 프롬프트는 버전 폴더(`labels.md`·`summary.md`)
- 앱 설계 §1: `ax_call_classifications` 추가 칸, `ax_call_disagreements`의 `todo_mismatch`
- 평가 설계 §2.3: `has_todo`에 "전달만 약속해도 1"
- 평가 설계 §2.7: 재료·검증 비율을 `evaluation/metrics.py` 상수 이름으로 가리킨다
- AI 실험 기록 8: "급함은 합치거나 손실에서 뺀다"를 "3값 그대로, 비교는 urgent 여부 위주"로
- 기능 정의 §9: "모델 B 착수 시" 항목을 이번 배치로 정리, 후순위 두 줄 추가(다른 오픈 모델 비교 — 얻는 것: 모델 선택 근거를 정확도·속도·크기로, Qwen 대안, 병원 사양에 맞춘 크기 선택 / 꺼낼 조건: Qwen 비교 뒤, 판정기는 그대로. 세 계열 다수결 — Qwen 교체 뒤 필요하면)
- `.env.example`, `config/llm.yaml`

## 정한 것과 구현자에게 남긴 것

**사람이 정함**: 범위(위 "이번에 만든다/안 만든다"), 모델 배치와 비교 규칙(판정기 자리 Qwen — Qwen 판정기 설계), 호출 분리(요약은 분류기 쪽), `has_todo` 기준, 할 일·`has_todo` 표시(할 일 목록은 Haiku 것, 다르면 엇갈림), Solar 맛보기 먼저와 그 전환 기준, Haiku Batch, Qwen 2B 로컬부터, silver 재료·검증 70:30, Qwen 교체 기준, 다른 오픈 모델 비교 후순위

**내 판단(위에 표시)**: HTTP 작업 API를 화면 브랜치로 미룸, 라벨 enum을 `shared/`로 복사하고 비교 테스트, classify 프롬프트 버전 폴더, 별칭과 폴백 구성, 요약 호출의 폴백 제외와 `active_models`, `todo_mismatch` 엇갈림 규칙, 판정기 프롬프트를 분류기와 따로 씀, Qwen 급함 3값 학습

**구현자에게**: 모듈·함수 나눔, 명령 인자, Upstage SDK 세부(착수 전 확인 뒤), 요약 후처리 규칙의 세부, Qwen 학습 하이퍼파라미터
