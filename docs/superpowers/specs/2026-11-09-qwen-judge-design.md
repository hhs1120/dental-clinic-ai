# Qwen 판정기 — 설계

① 판정기(모델 B)를 Solar Mini 4(`judge v2`)에서 파인튜닝한 Qwen3.5-2B full(`judge v3`)로 바꾼다. 분류기(라벨·요약·할 일)는 Haiku 그대로다. 근거는 [[치과 AX - AI 실험 기록]] §15 "추가 분석 — Qwen을 판정기로"와 `evaluation/finetune/qwen/out/report.md`.

## 왜

- 교차검증의 값은 판정기 단독 정확도보다 두 모델이 같이 틀리지 않는 데서 나온다(평가 설계 §2.5). 검증 405 + dev 75건에서 Haiku 오류 포착이 Solar 30/62 → Qwen 58/62, 둘 다 같다고 했는데 틀린 건이 32 → 4, dev 골드 포착 11/22 → 22/22다
- 분류기 라벨 자리에서는 Qwen이 Haiku와 대등할 뿐이라(dev 목적 73/75로 같음) 얻는 것이 라벨 호출 비용뿐이다. 판정기 자리는 Solar 호출이 통째로 빠지고 검증이 강해진다
- 두 자리에 다 두지 않는다 — 같은 모델이면 엇갈림이 사라져 교차검증이 무의미하다(평가 설계 §2.5 "같아진 구성은 내지 않는다")

## 목표

- 앱의 모든 판정 호출(일괄 `make judge`, 새 통화 판정 `backend/calls.py`)이 게이트웨이를 지나 로컬 Qwen으로 간다 — 판정을 부르는 backend 코드는 바뀌지 않는다
- 판정 결과는 지금과 같은 `ax_model_judgments` 행(별칭 `judge`, 버전 `v3`)이라 엇갈림·판정 큐·정확도 뷰·자동 종결이 그대로 읽는다
- 학습한 통화가 판정기 지표를 부풀리지 않는다

## 안 하는 것

- 분류기 쪽 변경, Solar용 판정기 프롬프트 보강(`wants_appt`·목적·상대 정의) — 판정기 자리를 골라 필요 없다
- 재학습·희귀 통화 보강·4B 학습(4B는 아래 "4B 비교"에서 조건만)
- 화면(`frontend/`)·API(`backend/api.py`)·하네스(`backend/harness/`)·스키마(`shared/app_schema.sql`) 변경 — 메인 세션이 ③ 챗을 하고 있다
- `make data`·데이터 재생성
- 골드 점검 다시 돌리기(`make gold-arbitrate` — 판정기 버전이 바뀌면 새 대상이 생기지만 과금이라 사람이 따로 정한다)

## 실패로 보는 것

- 게이트웨이를 거치지 않는 모델 호출, `ax_llm_calls`에 행이 없는 판정
- 앱 판정 결과가 실험의 예측(`out/pred_full.jsonl`)과 같은 입력에서 다른 것 — 같은 지시문·입력·탐욕 디코딩이면 같아야 한다
- backend가 `evaluation/`을 import하는 것(어댑터 경로를 설정으로 가리키는 것은 import가 아니다)
- 학습 재료 통화가 판정기 채점·교차검증 모수에 들어가는 것
- CI(리눅스, `finetune` 그룹 없음)가 MLX 때문에 깨지는 것

## 정한 것

### 게이트웨이 로컬 갈래 (사람이 정함)

- `shared/llm.py`에 Upstage와 같은 모양으로 `provider: mlx` 갈래를 더한다. 모델 호출은 모두 게이트웨이에서 일어난다
- `config/llm.yaml`
  - 모델 항목 예: `qwen3.5-2b-judge: { provider: mlx, base: mlx-community/Qwen3.5-2B-bf16, adapter: evaluation/finetune/qwen/adapters/full, input: 0, output: 0 }`
  - 별칭 `judge: [qwen3.5-2b-judge]` — 폴백 없음(다른 모델로 넘어가면 판정기가 섞인다)
- 동작
  - 채팅 템플릿에 생각 끔(`enable_thinking=False`, 학습·`infer.py`와 같음), 탐욕 디코딩
  - 출력 상한: 요청의 `max_tokens`가 아니라 `infer.py`의 256 — 내 판단, 위치·이름은 구현자
  - `schema`가 있으면 출력에서 첫 JSON 객체를 읽어 `data`에 넣는다(게이트웨이는 라벨을 모른다). 값 검사는 지금처럼 `backend/classify.py` `valid_labels`가 한다
  - 실험 채점(`data.py` `parse_labels`)은 `{`마다 읽어 라벨로 맞는 첫 객체를 고른다. 같게 하려고 `judgment_from`이 `data`가 라벨이 아니면 `text`에서 같은 규칙으로 다시 찾는다(backend는 `evaluation/`을 import하지 못해 규칙을 옮겨 쓰고, 두 함수가 같은 결과인지 `pred_full.jsonl` 출력 전부로 테스트한다) — 내 판단
  - 어댑터·모델 파일이 없으면 예외 대신 실패 결과(`ok=False`, 오류 문구에 경로)를 돌려 `failed` 판정으로 남긴다. 새 통화 추가가 판정 때문에 멈추지 않는다 — 내 판단
  - 기록 1행은 지금과 같고 토큰 수는 토크나이저로 센다. 비용 0
- 모델·어댑터는 처음 부를 때 한 번 올리고 프로세스 안에서 다시 쓴다. 게이트웨이는 요청마다 새로 만들어지므로(`backend/harness/llm.py` `make_gateway`) 캐시는 모듈 수준이다. MLX 생성은 잠금 하나로 한 번에 하나씩 돈다(API 스레드가 동시에 부를 수 있다)
- `mlx_lm`은 이 갈래 안에서만 import한다(Upstage의 openai와 같은 방식). 테스트는 생성 함수를 바꿔 끼운 가짜로 이 갈래를 시험한다(CI에는 `mlx-lm`이 없다)
- 의존성(사람이 정함): `mlx-lm`은 지금처럼 `finetune` 그룹에 두고, Qwen 판정이 일어나는 Makefile 명령(`make judge`, 새 통화를 판정하는 `make dev`)만 `uv run --group finetune`으로 돌린다. `pyproject.toml`·`uv.lock`·CLAUDE.md는 바꾸지 않는다
  - 이유: "mlx-lm은 기본 그룹이 아니다"(CLAUDE.md)를 지킨다. 판정이 필요 없는 생성기·테스트·CI에는 `transformers`까지 딸린 무거운 묶음을 깔지 않는다. 쓰는 사람에게 보이는 차이는 Makefile이 덮는다
  - 그룹 없이 띄우면(`mlx_lm` import 실패) 앱은 멈추지 않고 그 판정을 `failed`로 남긴다. 오류 문구에 `--group finetune`으로 실행하라고 적는다
  - 실제 병원에 앱만 따로 설치할 때는 `backend` 그룹으로 옮긴다(`sys_platform == 'darwin'` 표지, 병원 PC가 Mac이 아니면 그때 추론 경로를 다시 정한다) — 그때는 앱 설치 한 번으로 판정이 돌아야 하고, 생성기·학습 코드는 병원에 가지 않는다. 이 줄은 기능 정의 §9 "Qwen 도입 시" 항목에도 적는다
- Batch 없음 — `classify_batch._batchable`이 anthropic만 Batch로 보내므로 손대지 않아도 일반 호출로 돈다
- `model_actual`은 별칭 모델 이름에 어댑터 지문(`adapters.safetensors` sha256 앞 16자, `infer.py` `adapter_fingerprint`와 같은 값)을 붙인다 — 어댑터를 바꾸면 행에서 드러난다. 형식은 구현자

### 판정기 v3

- `prompts/judge/v3.md` 본문은 학습 지시문(`evaluation/finetune/qwen/data.py` `INSTRUCTION`)과 글자 하나까지 같다. 테스트(`tests/` — import 제한 밖)가 비교한다. 머리말 `effort`는 없다
- 입력은 지금 판정기와 같은 `user_message(transcript, direction)` — 학습 입력과 같다
- 확인: `out/pred_full.jsonl`에는 어댑터 지문이 없어 지금 어댑터로 낸 것인지 파일만으로는 모른다. 그래서 (가) 검증·dev 30건을 지금 어댑터로 `infer.py` 경로로 다시 예측해 `pred_full`과 출력 텍스트가 같은지 보고(어댑터 확인), (나) 같은 30건을 게이트웨이 v3 판정으로 돌려 (가)와 출력 텍스트·라벨이 모두 같은지 본다. 한 건이라도 다르면 멈추고 원인을 찾는다. (가)가 다르면 지금 어댑터로 예측을 다시 내 실험 기록 수치를 고칠지 사람에게 묻는다. 결과는 임시 폴더, 기록은 임시 `JsonlLog` — `app.sqlite`에 쓰지 않는다 — 내 판단

### 학습한 통화 빼기

- Qwen은 silver 재료 구간(`evaluation/splits.py` `silver_split`의 재료)으로 학습했다. `eval.py --split dev`의 판정기 채점(`evaluate_judge`)과 교차검증(`crosscheck`)은 silver·dev 골드를 모수로 쓰므로 v3에서는 재료 구간 통화 전체를 뺀다(`original_wrong`으로 학습에서 빠진 것도 함께 — 내 판단, 셈이 단순하다)
- 어느 판정기 버전이 학습했는지는 프롬프트 머리말에 둔다. 예: `trained_on: silver_material`. 평가가 이 값을 읽어 뺀다. 기존 few-shot 제외(평가 설계 §7 `fewshot_call_uids`)와 같은 자리에서 처리한다 — 이름·형식은 구현자
- 골드 점검 대상 선정(`evaluation/gold_audit.py`)도 같은 제외를 쓴다 — 판정기가 학습한 통화는 판정기와 골드가 거의 늘 같아 조용히 빠지므로, 빠졌다는 것을 수로 적는다. 중재 실행은 이번 범위 밖 — 내 판단
- 앱 정확도 뷰(`backend/metrics.py`)는 dev 판정 풀만 쓰므로 그대로다. 판정 큐·엇갈림은 학습 통화에서도 그대로 돈다(앱은 정답을 모른다)

### 비교 규칙 — 한 번에 한쪽만 바꾼다 (사람이 정함)

- "판정기는 처음부터 끝까지 고정한다"를 이 규칙으로 바꾼다(분류기 설계 "모델 배치", 평가 설계 §2.5): **분류기 버전 비교는 같은 판정기 버전 안에서, 판정기 비교는 같은 분류기 버전·같은 통화 모수에서 한다.** 분류기 v1은 Solar로, v2는 Qwen으로 재고 둘을 비교하는 식으로 섞지 않는다
- 판정 결과는 통화마다 저장되고 분류기 버전에 묶이지 않아, v3 판정 한 번으로 분류기 v1·v2 모두 v3 기준 엇갈림을 다시 계산할 수 있다 — 판정기를 바꿔도 분류기 비교는 잃지 않는다
- 판정기 v2(Solar) 대 v3(Qwen)를 분류기 v2 고정으로 비교한 수치가 이 변경의 대표 결과다. 모수는 같아야 하므로 Solar 쪽에서도 Qwen 학습 통화(재료 구간)를 뺀다
- 판정기 버전마다 머리말 `model`로 모델을 고정한다(v1·v2 `solar-mini4`, v3 `qwen3.5-2b-judge`, 사람이 정함). 지금 `judge` 별칭의 첫 모델과 다르면 새 통화·다시 분류는 판정을 건너뛰고(`backend/classify.py` `judge_model_ok`), `make judge`는 시작하지 않는다 — 머지 뒤 전환 전까지 옛 v2 행이 Qwen 실패로 덮이지 않게
- 옛 판정기 버전은 모델이 바뀌어 다시 돌리지 않는다(`make judge`는 거부, 다시 분류는 판정만 건너뛰고 엇갈림은 새 분류로 다시 계산) — 있는 v2 판정 1,500건은 읽기만

### 실행 순서 (`app.sqlite`에 쓰는 단계는 돌리기 전에 사람에게 묻는다)

1. 코드·테스트·문서 → `make fmt` → `make lint` → `make test`
2. 위 "확인"(`app.sqlite`에 쓰지 않음)
3. 묻고 실행: `make judge V=v3 NO_BATCH=1` — 1,500통화 × 약 2.2초 ≈ 55분. `--shard`는 프로세스마다 4.8GB라 둘까지. 탐욕 디코딩이라 실패한 건은 다시 돌려도 같다 — `failed`로 두고 건수를 실험 기록에 적는다(다시 도는 비용은 건당 2초라 표시를 따로 두지 않는다) — 내 판단
4. 머지하고 다른 작업 브랜치가 리베이스한 뒤(옛 코드에는 v3 프롬프트가 없어 새 통화 판정이 실패한다 — 기능 정의 §9에 할 일로 둔다) 묻고 실행: 활성 판정기 `ax_settings.prompt_versions.judge`를 `v3`로 **먼저** 바꾸고 `make disagreements A=v2 B=v3`(활성 쌍이면 `refresh_if_active`로 큐를 채우고 `judge_queue.prune`으로 옛 대기 건을 지운다 — 아래), `make autoclose`
   - 자동 종결 평가(`evaluation/autoclose.py`)도 비교 규칙대로 분류기 v2 고정, 판정기 v2·v3를 나란히 낸다. 검증 구간만 써 학습 통화와 겹치지 않는다. `auto_policy`는 꺼져 있어 앱 동작은 바뀌지 않는다
   - 판정 큐 정리(사람이 정함): `ax_judge_state`의 `pending` 중 v3 대상(엇갈림 또는 무작위 표본, `judge_queue.TARGETS_SQL`)이 아닌 통화를 지운다. `skipped`·`done`과 사람 판정(`ax_judgments`)은 그대로 둔다. 판정기를 바꿀 때 쓰는 함수로 `backend/judge_queue.py`에 두고 테스트한다
5. `eval.py --split dev`가 분류기 v2 고정으로 판정기 v2·v3의 판정기 채점·교차검증(엇갈림·핵심 엇갈림·일치 오류·오류 포착·향상 배수)을 같은 모수(재료 구간을 뺀 silver·dev 골드)에서 나란히 낸다. 이 모수는 오프라인 분석(검증 405 + dev 75)과 같으므로 실험 기록 §15 수치(포착 Solar 30/62·Qwen 58/62)를 앱 경로로 다시 확인하는 셈이다. 다르면 이유를 실험 기록에 적는다

## 4B 비교 (다시 볼 조건 한 줄)

- 기능 정의 §9에 적을 줄: **4B half가 2B full보다 dev 목적 또는 Haiku 오류 포착(검증+dev, 2B full 58/62)이 높고, MLX로 바꾼 뒤 건당 추론 시간·메모리가 병원 PC에 들어가면 판정기 모델을 4B로 바꿀지 다시 본다**
- 4B는 Colab(GPU)에서 PEFT QLoRA로 학습하므로 어댑터 형식(PEFT `adapter_model.safetensors`)·예측 경로(transformers)가 2B(MLX LoRA, `infer.py`)와 다르다
  - 비교 단계: Colab에서 같은 셋에 예측하고 `pred_*.jsonl`과 같은 줄 형식으로 내려받아 `score.py`로 채점한다. 판정기 자리 분석도 같은 예측으로 한다. 앱은 건드리지 않는다
  - 바꾸기로 하면: PEFT 어댑터를 bf16 기본 모델에 합친 뒤 `mlx_lm.convert`로 MLX 모델을 만든다(4비트 기본 위에 학습한 어댑터를 그대로 합치지 않는다). 그 모델로 앱 예측이 Colab 예측과 같은지 다시 맞춰 보고, 같지 않으면 앱 경로로 다시 채점한다. 게이트웨이 설정은 `base`만 바뀌고 `adapter`가 없는 모양이 된다
  - 지시문·입력·출력 형식은 2B와 같게 학습한다(같은 `data.py` 셋) — 판정기 v3 프롬프트를 그대로 쓰려면

## 문서 변경

- 분류기 설계(`2026-11-05-classifier-design.md`) "모델 배치" 표: 판정기 라벨 Solar Mini 4 → Qwen3.5-2B full(로컬), 분류기 라벨은 Haiku 그대로. 표 아래 "Qwen 자리" 줄을 결과로, "판정기는 처음부터 끝까지 고정한다" 줄을 위 비교 규칙으로. "Qwen 비교"의 "앱에서 Qwen을 돌리는 방법은 바꾸기로 한 뒤에 정한다"를 이 문서로 가리킨다
- 기능 정의 §9: "Qwen 자리 정하기"를 지우고 결과를 반영(판정기 자리, v3 판정 실행이 남은 일이면 그 한 줄), 4B 줄에 위 조건, "사람 개입 최소화" 순서에서 "Qwen 자리"를 뺀다, "Qwen 도입 시"에 의존성을 `backend` 그룹으로 옮기는 줄
- 앱 설계·기능 정의·실험 기록에서 "판정기 = Solar"로 읽히는 문장(활성 판정기를 말하는 곳만 — 실험 경위는 그대로). `tests/`의 solar는 가짜 설정이라 그대로
- 앱 설계 §7 "모델 (확정)"·§7.1 게이트웨이: 프로바이더에 로컬 MLX(`finetune` 그룹으로 실행), 판정기 모델
- 평가 설계 §2.5: 비교 규칙(한 번에 한쪽만), 학습한 통화 빼기, 판정기끼리 비교는 같은 모수
- AI 실험 기록 §15: 5단계에서 나온 앱 수치(오프라인과 다르면 이유)

## 구현자에게 남긴 것

게이트웨이 갈래의 함수 나눔과 이름, 출력 상한 상수의 자리, `model_actual` 형식, 머리말 키 이름, 확인 스크립트를 둘 곳(일회성이면 테스트나 `evaluation/finetune/qwen/` 하위 명령), 테스트 가짜의 모양
