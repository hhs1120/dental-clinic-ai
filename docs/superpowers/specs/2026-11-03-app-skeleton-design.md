# 앱 뼈대·기록 검색 화면 설계

기능 정의 §9 착수 순서의 첫 항목. 규칙의 원본은 `docs/치과 AX - 앱 설계.md` §3·§7·§9, `docs/치과 AX - 기능 정의.md` §4 ③, 화면의 레이아웃·동작 기준은 `docs/wireframe/index-v2.html`이다. 이 문서는 이번 범위와 원본에 없는 결정만 적는다. 표시 규칙처럼 뒤 화면도 따를 결정은 앱 설계 §9에 있다.

## 목적

명령 하나로 앱을 띄워 실제 데이터로 기록 검색을 해 볼 수 있고, 이후 화면을 같은 틀에 계속 붙일 수 있는 상태를 만든다. 검색은 평가에 쓴 `Searcher`를 그대로 써서 평가 수치와 화면 동작이 같은 코드에서 나오게 한다.

## 범위

**이번에 만든다**
- FastAPI 앱: `GET /search`, 병원 이름·기준일을 주는 `GET /clinic`
- Next.js 앱: 시작 화면 `/`, 공통 레이아웃, `/ask`의 기록 검색 탭
- 공통 레이아웃(앱 설계 §9): 로고(시작 화면으로), 메뉴, 담당 파트 선택, 기준일. `/eval`·`/emr`은 메뉴 자리에 공간 이름과 돌아가기
- `make dev`, `make lint`·`make fmt`의 프론트 검사, CI의 프론트 검사와 빌드
- 문서 반영(아래 "문서 변경")

**이번에 안 만든다**
- AI 요약과 "이걸로 질문하기" — ③ 챗에서 같은 출력 형식(`{text, cited_ids[]}`)으로 함께 만든다. 카드 자리도 두지 않는다
- `ax_search_logs` — 위와 같은 이유로 ③ 챗에서 만든다. 이번 검색은 기록을 남기지 않는다
- 질문하기·문서함 탭의 내용 — 탭 자리만 두고 "준비 중"을 보인다
- 그 밖의 메뉴 내용 — 라우트는 만들고 "준비 중"을 보인다
- 카드에서 원본 전체로 가는 이동 — 통화 화면·문서함이 생길 때 잇는다
- `make fixture`, Playwright, `make ci`
- 담당 파트의 서버 저장. 지금은 기록할 행동이 없다
- 기동 때 하는 일(앱 설계 §7.3 지문 비교) — 해당 기능을 만들 때

**실패로 보는 것**
- 평가에 쓴 검색과 다른 경로로 결과를 만드는 것 (예: API 안에서 따로 쿼리 조합, 관련도 임계값 추가)
- 검색용 표현이 화면에 나오는 것 (차트 끝 괄호, 청크 prefix가 본문에 중복)
- 테스트가 API 키를 요구하는 것

## 백엔드

**구조**: `backend/api.py`에 FastAPI 앱 하나. 라우터 분리는 화면이 늘 때 한다. 의존성 `fastapi`, `uvicorn`은 `backend` 그룹, `httpx`(TestClient)는 `dev` 그룹. `tests/test_no_ground_truth_import.py`가 파일 내용을 검사하므로 주석에도 금지어를 쓰지 않는다.

**연결**: sqlite3 연결은 만든 스레드에 묶인다. 의존성과 핸들러는 다른 워커 스레드에서 돌 수 있어, 의존성에서 연 연결을 핸들러가 쓰면 동시 요청에서 500이 난다(`TestClient`로는 재현되지 않는다). 그래서
- 핸들러는 동기 `def`로 쓰고 `async def`는 쓰지 않는다
- `backend.db.connect`와 `Searcher`는 핸들러 안에서 만들고 `try/finally`로 닫는다
- 임베더·리랭커만 프로세스에서 한 번 만들어 의존성으로 준다. 테스트는 이 의존성을 `FakeEmbedder`로 바꿔 끼운다
- 이번 API는 읽기뿐이므로 연결 직후 `PRAGMA query_only = ON`
- 경로는 `make index`와 같은 `data/app.sqlite`·`data/source.sqlite`

**검색기**: `make_embedder(DEFAULT_EMBEDDING)` + `voyage_rerank(DEFAULT_RERANKER)`. 리랭커 실패는 `_bucket`이 이미 로그를 남기므로 API가 더 하지 않는다.

**`GET /search`**
- 인자: `q`(필수), `source_types`(쉼표 구분), `date_from`, `date_to`, `chart_no`
- `backend/api.py`의 변환 함수가 `SearchArgs`로 옮긴다. `SearchArgs`와 도구 시그니처는 바꾸지 않는다(평가 `expected_args`의 기준)

| API | `SearchArgs` | 변환 |
|---|---|---|
| `q` | `query` | 앞뒤 공백 제거. 비면 422 |
| `source_types` | `source_types` | 생략은 전체, 값을 주고 비어 있으면 422 |
| `date_from`·`date_to` | 같음 | 빈 문자열은 생략과 같다 |
| `chart_no` | `patient_chart_no` | 앞뒤 공백 제거, 대문자로. 빈 문자열은 생략과 같다 |
| 없음 | `top_k` | `SEARCH_TOP_K` |

- `ValidationError`는 잡아서 422로 답한다. `Searcher.search`에는 `model_dump(mode="json")` 값을 넘긴다(`backend/harness/loop.py`와 같다)
- 화면 결과 수 `SEARCH_TOP_K = 10`은 `backend/rag_search.py` 상수 (도구 기본값과 별개, 화면은 훑어보는 용도)
- 응답: `{manual_results: [...], case_results: [...]}`. 순서는 `Searcher.search` 그대로이고 묶지 않는다. 각 결과는

| 칸 | 내용 |
|---|---|
| `id` | 청크 id |
| `source_type` | `document` / `call` / `message_thread` / `chart_note` |
| `title` | 문서는 `제목 > 섹션`, 그 밖에는 `null` |
| `body` | 화면에 낼 본문. prefix와 차트 끝 괄호를 뺀 것 |
| `event_date` | 시행일·통화일 등. 없으면 `null` |
| `chart_no` | 환자 기록이면 차트 번호, 아니면 `null` |
| `patient_name` | 통화·문자. 차트 기록은 prefix 규칙대로 이름 없음 |
| `caller_name` | 환자 없는 통화만. 색인 prefix의 `상대`와 같은 값 |
| `direction` | 통화만. `수신`/`발신` |
| `doc_type` | 문서만. `ax_chunks.doc_type` |
| `version` | 문서만. `source.ax_documents.version` |
| `is_outdated` | 문서만. 더 새 공지가 있으면 참(`Searcher.replaced_by`) |

- 화면 칸 만들기는 출처마다 방식이 하나다
  - 문서·통화·문자: `text`의 prefix를 떼어 만든다. 이름·상대·방향도 prefix에서 읽는다 — 통화는 source 조인에 기대지 않아 뒤에 붙여넣기 통화(`call:p-…`)가 들어와도 깨지지 않는다
  - 차트: `chart:<treatment_id>`로 `source.emr_chart_notes.note`를 읽는다. `text`에서 괄호를 잘라내지 않는다(서술 자체에 괄호가 있다)
  - prefix를 만드는 코드와 어긋나지 않도록 `backend/rag_index.py` 옆에 함수를 두고 테스트로 묶는다

**`GET /clinic`**: `{name, today}` — `source.emr_clinic.name`, `load_clinic().today`.

**오류** — 503 문구는 `make index EMBEDDING=<DEFAULT_EMBEDDING 값>`을 가리킨다.

| 상황 | 처리 |
|---|---|
| `app.sqlite`·`source.sqlite` 파일 없음 | 연결 전에 확인해 503. `connect`가 빈 파일을 만들게 두지 않는다 |
| 청크 0개 | 503 (`eval_hooks`의 빈 색인 거절과 같다) |
| `MissingVectors` | 503 |
| 임베딩 호출 실패(키 없음 포함) | 503, 원인 문구 |
| 리랭커 실패 | 합친 순서로 정상 응답 |

## 프론트

**세팅**: `frontend/`에 Next.js(App Router) + TypeScript strict, ESLint `next/core-web-vitals`, Prettier. 패키지 관리자는 npm(이 PC에 pnpm 없음), `package-lock.json` 커밋, Node 버전은 `.nvmrc`로 고정.

**스타일**: Tailwind CSS v4 + shadcn/ui. 와이어프레임은 레이아웃·동작의 기준이고, 색·모양은 아래 토큰에서 나온다. 와이어프레임의 `:root` 값과 CSS는 옮기지 않는다.
- 색·모서리·글꼴·그림자는 `frontend/app/globals.css`의 CSS 변수 토큰으로만 쓴다. 화면 코드에 hex·`rounded-[7px]` 같은 직접 값을 쓰지 않고, Tailwind 기본 색 이름(`teal-700` 등)도 쓰지 않는다
- 버튼·입력·체크박스·탭·카드·배지 같은 부품은 shadcn CLI가 만든 `frontend/components/ui/`의 것만 쓴다. 필요한 모양은 그 파일에 variant로 더하고, 화면에서 같은 부품을 새로 만들지 않는다
- 다크 값도 토큰에 넣고 OS 설정(`prefers-color-scheme`)을 따른다. 전환 버튼은 두지 않는다
- 토큰 값은 2~3개 화면이 생긴 뒤 다듬기 단계에서 고친다(기능 정의 §9). 지금 값은 아래 표

| 토큰 | 라이트 | 다크 |
|---|---|---|
| `background` | `#F0F0F0` | `#1A1A1A` |
| `card`·`popover` | `#F5F5F5` | `#202020` |
| 머리 띠(`sidebar`) | `#EAEAEA` | `#1F1F1F` |
| `secondary`·`muted`·`input` | `#E0E0E0` | `#303030` |
| `border` | `#D0D0D0` | `#353535` |
| `foreground` / `muted-foreground` | `#333333` / `#666666` | `#D9D9D9` / `#808080` |
| `primary` / `primary-foreground` | `#115E59` / `#FFFFFF` | `#2BB3A3` / `#062421` |
| `accent`(선택된 메뉴·종류 배지) / 글자 | `#D0E4E2` / `#0F4F4B` | `#173A37` / `#99E6DA` |
| `ring` | `rgba(17, 94, 89, 0.3)` | `rgba(43, 179, 163, 0.35)` |
| 링크 | `#115E59` | `#5FD0C2` |
| 검색어 강조(`mark`) | `#CBE1DE` | `#1C4743` |
| `--radius` | `0.4375rem` — 컨트롤(`rounded-md`) 5px, 카드(`rounded-xl`) 11px | 같음 |
| 그림자 | `0 2px 0 0 hsl(0 0% 20% / 0.15), 0 1px 2px -1px hsl(0 0% 20% / 0.15)`, 입력·버튼은 `0 2px 0 0 hsl(0 0% 20% / 0.07)` | 같음 |
| 글꼴 | Inter + Noto Sans KR (`next/font/google`) | 같음 |

- 바탕·글자·그림자는 tweakcn Graphite 테마(oklch → sRGB), 강조색은 그 위에 얹은 딥 틸이다. 라이트와 다크의 `primary`가 다른 것은 의도다 — 다크 값 `#2BB3A3`을 라이트에 쓰면 흰 글자 대비가 약 2.5:1로 모자라고, 링크·선택 글자를 결국 진한 청록으로 따로 둬야 한다
- 상태 표시 — 진한 배경 + 같은 색 테두리 + 점 + 굵은 글자. `components/ui/badge`의 variant(`warn`·`danger`·`ok`·`info`)와 안내 띠가 같은 토큰(`--status-<종류>-bg·fg·border·dot`)을 쓴다. "옛 버전"은 `muted-foreground` 점선 테두리 variant

| 상태 | 라이트 배경 / 글자 / 테두리 / 점 | 다크 배경 / 글자 / 테두리 / 점 |
|---|---|---|
| `warn` 주의 | `#FDE68A` / `#78350F` / `#F59E0B` / `#D97706` | `#5C4410` / `#FDE68A` / `#B45309` / `#FBBF24` |
| `danger` 엇갈림·오류 | `#FECACA` / `#991B1B` / `#F87171` / `#DC2626` | `#5C1D1D` / `#FECACA` / `#DC2626` / `#F87171` |
| `ok` 처리 완료 | `#BBF7D0` / `#14532D` / `#4ADE80` / `#16A34A` | `#14432A` / `#BBF7D0` / `#16A34A` / `#4ADE80` |
| `info` 대기 | `#BFDBFE` / `#1E3A8A` / `#60A5FA` / `#2563EB` | `#1E3A6E` / `#BFDBFE` / `#2563EB` / `#60A5FA` |

이번 화면에서 상태 색을 쓰는 곳은 "옛 버전"과 오류 띠뿐이지만, 토큰과 variant는 지금 다 만든다 — 뒤 화면이 값을 새로 고르지 않게.

**API 연결**: `next.config`의 rewrites로 `/api/*` → `http://localhost:8000/*`. CORS를 쓰지 않는다. 외부 API 재시도까지 합친 지연을 프록시가 먼저 끊지 않는지 구현 때 확인한다.

**화면**
- 시작 화면 `/`: 메뉴 없음. 제목은 `GET /clinic`의 병원 이름, 타일은 와이어프레임
- 공통 레이아웃: 담당 파트는 원장/데스크/상담실장 중 고르고 `localStorage`에 기억(읽기·쓰기 `try/catch`). 와이어프레임의 직원 이름은 옮기지 않는다. 기준일은 `GET /clinic`, 실패하면 `—`
- 메뉴 항목은 기능 정의 §4 순서(`예약` 포함 — 와이어프레임에는 없다). 아직 없는 화면은 "준비 중"
- `/ask` 탭 `질문하기 | 기록 검색 | 문서함`. 지금은 기록 검색만 작동하므로 기본 탭이 기록 검색이다. 입력한 내용은 탭을 바꿔도 유지한다
- 기록 검색: 검색어 + 찾기 버튼, 출처 체크박스(문서·통화·문자·차트 기록), 기간, 환자번호. 결과는 2단(`매뉴얼 결과` / `실제 사례 결과`)
- 출처를 모두 끄면 찾기를 막는다. 빈 값은 프론트가 보내지 않는다
- 결과 머리에 건수를 "관련도 상위 N건"으로 표시한다. 벡터 검색에 임계값이 없어 의미 없는 검색어도 결과가 나오기 때문이다
- 결과 카드: 출처 종류, 날짜, 차트 번호. 통화·문자는 환자 이름(환자 없는 통화는 `상대: caller_name`), 통화는 방향. 문서는 종류, 공지는 버전·시행일, 옛 공지는 "옛 버전" 표시
- 카드 하나는 조각(청크) 하나이고 묶지 않는다. 본문은 접어서 앞부분만, 펼치면 그 조각 전체
- 검색어 강조: 공백으로 나눈 어절이 본문에 그대로 있으면 표시한다. 벡터로 찾은 카드는 강조가 없을 수 있다. HTML 문자열 삽입 없이 React 노드로 만든다
- 상태: 처음(안내 문구), 로딩, 결과 없음, 오류. 오류는 API `detail`을 쓰고, JSON이 아니면(프록시 실패) "서버에 연결하지 못했습니다"
- 매뉴얼 칸이 필터 때문에 비면 이유를 적는다
  - 출처에서 문서를 뺌 → "문서가 출처에서 빠졌습니다"
  - 환자번호 있음 → "환자번호 필터에서는 문서를 찾지 않습니다"
  - 기간 있음 → "기간 필터에서는 시행일 있는 공지만 찾습니다"
- 검색 상태는 URL 쿼리로 둔다(`/ask?tab=search&q=…`) — 새로고침·공유에 살아남고, 뒤에 챗의 "근거 더 보기"가 같은 URL을 쓴다

**옮기지 않는 것**: 와이어프레임의 색·CSS, 클라이언트 측 단어 일치 검색, HTML 문자열 강조, 지금 사실이 아닌 머리말 문구("무엇을 물었는지 기록됩니다", "색인 갱신 04:00", "08:50"). 머리말은 현재 동작에 맞게 쓴다.

## 실행·도구

- `make dev`: uvicorn(8000)과 `next dev`(3000)를 같이 띄운다. `.env`가 있으면 읽는다. Ctrl+C로 둘 다 끝난다
- `frontend/node_modules` make 타깃이 `npm ci`를 돌린다. `make dev`·`make lint`·`make fmt`는 이 타깃을 선행 조건으로 둔다 — 커밋 훅이 `make lint`를 돌리므로 새 worktree에서 파이썬 커밋이 막히지 않게
- `make lint`에 `eslint`·`prettier --check`·`tsc --noEmit`, `make fmt`에 `prettier --write` 추가
- `.github/workflows/ci.yml`: `make lint` 앞에 `actions/setup-node`(`.nvmrc`)를 두고, `make test` 뒤에 `next build`
- `.gitignore`: `frontend/node_modules`, `frontend/.next`, `frontend/next-env.d.ts`, `*.tsbuildinfo`

## 테스트

- `tests/backend/test_api.py` (TestClient + `FakeEmbedder`, 리랭커 없음, 임시 DB). `tests/backend/conftest.py`의 source 픽스처에 `ax_calls`·`emr_chart_notes`를 더한다
  - 버킷 분리, 필터 전달, `/search` 결과가 같은 인자의 `Searcher.search` 결과와 id·순서가 같다
  - 422: `q` 공백, 빈 `source_types`, 잘못된 `source_types`·날짜
  - 503: DB 파일 없음(빈 파일이 생기지 않음), 청크 0개, 벡터 없음, 임베딩 실패
  - 카드 칸: `patient_name`·`caller_name`·`direction`·`is_outdated`, `chart_no` 소문자 입력
  - 연결이 핸들러 안에서 열리는지: 스레드 풀로 동시 요청을 넣어 500이 없는지 실제 uvicorn으로 확인한다(`TestClient`로는 재현되지 않으므로, 구현 때 자동 테스트로 쓸 수 없으면 완료 기준 수동 확인으로 남긴다)
- 화면용 칸 함수: 출처마다 prefix가 없고, 차트 끝 괄호가 없으며 서술 안의 괄호는 남는다
- 프론트 단위 테스트는 두지 않는다. 검사는 lint·tsc·build + 아래 수동 확인

## 완료 기준

1. `make test`·`make lint` 통과, CI 통과
2. `make dev` 뒤 브라우저에서 시작 화면 → 물어보기 → 기록 검색으로 이동, Ctrl+C로 둘 다 종료
3. 실데이터(`DEFAULT_EMBEDDING` + `DEFAULT_RERANKER`)로 다음을 직접 확인
   - "크라운 가격"이 두 버킷으로 나뉘어 나온다
   - 출처를 차트 기록만 체크하면 사례 결과가 차트만 나오고 차트 번호가 보이며, 매뉴얼 칸에 안내가 나온다
   - 환자번호 필터를 주면 그 환자 기록만 나온다
   - 차트 카드에 검색용 괄호가 없다
   - 옛 공지 카드에 "옛 버전"이 보인다
   - 새로고침해도 검색이 복원되고, 담당 파트 선택이 유지된다
4. 동시 요청 시험에서 500이 없다
5. 서버를 내리거나 색인이 없을 때 화면에 오류 문구가 나오고 깨지지 않는다

## 문서 변경 (구현과 함께)

- `CLAUDE.md`: 폴더(backend FastAPI, frontend), 명령(`make dev`), 코드 컨벤션의 Next.js 규칙을 켠 상태로(Tailwind + shadcn, 토큰만·`components/ui`만 규칙 포함), 깃 절의 CI 설명, 설계 문서의 검색 상수 위치에 맞는지 확인
- `README.md`: `frontend/ Next.js (예정)` 줄
- `.github/workflows/ci.yml` 머리 주석
- 앱 설계 §7 '스택' 행에 Tailwind + shadcn/ui, '실행' 행에 `make dev`. §9 첫 문단을 "와이어프레임은 레이아웃·동작의 기준, 색·모양은 `frontend/app/globals.css` 토큰과 `components/ui`"로, 공통 레이아웃에 `GET /clinic`
- 기능 정의 §9: 끝난 "프론트 착수 시 — 컨벤션" 항목 삭제, 착수 순서에서 완료분 제거
