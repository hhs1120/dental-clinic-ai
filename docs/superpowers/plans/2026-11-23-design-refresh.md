# 화면 디자인 새로 입히기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 라이트(웜 차콜)·다크(Warp 색) 토큰, Inter + Pretendard, 한 덩어리 사이드바, 위쪽 제목 띠, 옅은 바탕 위 카드로 앱 전체 모양을 바꾼다.

**Architecture:** 색·모서리·글꼴·그림자는 `frontend/app/globals.css` 토큰만 바꿔 모든 화면이 따라오게 한다. 사이드바는 새 `--sidebar*` 토큰을 셸 두 파일에서 쓴다. 제목 띠는 새 공용 부품 `PageHeader`를 11개 `page.tsx`가 쓰고, 띠 높이를 CSS 변수로 알려 본문 안 sticky 칸이 그 아래에 붙게 한다.

**Tech Stack:** Next.js(App Router) · Tailwind v4 `@theme inline` · shadcn/ui · next/font(google·local) · Playwright e2e

**Spec:** `docs/superpowers/specs/2026-11-23-design-refresh-design.md` — 토큰 값은 모두 거기 표에서 그대로 옮긴다(이 계획에 다시 적지 않는다).

## Global Constraints

- 색·모서리·글꼴·그림자는 `globals.css` 토큰으로만, 직접 hex·Tailwind 기본 색 이름 금지, 부품은 `components/ui/`만(CLAUDE.md 코드 컨벤션). 배치용 임의 값(`max-w-[…]`, `top-[calc(…)]`)은 지금처럼 허용
- 전환 전 EMR 창 안(`--emr-*`, `components/emr/emr-ui.tsx`)은 손대지 않는다
- 다크는 `<html data-theme="dark">`(지금 그대로)
- 글꼴은 빌드에 들어가게 — 실행 중 외부 CDN 금지
- 화면 기능·문구는 바꾸지 않는다(제목·설명 문장은 그대로 띠로 옮긴다)
- 커밋 전 `make fmt` → `make lint` → `make test`, `git add`는 파일 이름으로, 브랜치 `feat/design-refresh`
- e2e는 로컬 전용(`make e2e`, 켜 둔 `make dev`와 따로 뜬다). 새 e2e는 `frontend/e2e/design.spec.ts` 한 파일에 모은다

## Review Focus

1. **설명이 여러 줄로 감기는 좁은 데스크톱(1024px)** — 띠가 높아져도 통화·종이 차트 이관의 sticky 칸이 띠 밑에 숨지 않아야 한다 → Task 3 테스트 `design: 띠가 높아져도 sticky 칸이 띠 아래`
2. **목록 칸을 접은 상태** — 띠 안 왼쪽에 "목록 펴기"가 보이고 누르면 목록이 다시 펴진다. AI 비서에서는 지금 자리 그대로 → Task 3 테스트 `design: 목록 펴기는 띠 안에`
3. **준비표 인쇄** — 띠가 붙지 않고 공간 이름·펴기 단추·설명은 숨고 h1은 남는다 → Task 3 테스트 `design: 인쇄하면 띠는 제목만`
4. **실행 중 테마 바꾸기** — 아이콘 줄의 테마 단추로 바꾸면 사이드바도 즉시 다크 색 → Task 2 테스트 `design: 테마를 바꾸면 사이드바도`
5. **휴대폰 폭(390px)** — 가로 스크롤이 생기지 않고 띠는 붙지 않는다 → Task 3 테스트 `design: 좁은 화면은 띠가 붙지 않고 가로 넘침 없음`

---

### Task 1: 토큰·글꼴

**Files:**
- Modify: `frontend/app/globals.css` (`@theme inline`, `:root`, `:root[data-theme="dark"]`, 머리 주석)
- Modify: `frontend/app/layout.tsx`
- Modify: `frontend/app/(portal)/layout.tsx:12` (`bg-header` → `bg-background`)
- Modify: `frontend/package.json`, `frontend/package-lock.json` (`pretendard`)
- Create: `frontend/e2e/design.spec.ts`

**Interfaces:**
- Produces: 토큰 `--sidebar` `--sidebar-foreground` `--sidebar-border` `--sidebar-accent` `--sidebar-accent-foreground` `--sidebar-hover` `--sidebar-count`, 그림자 `--shadow-sidebar-active` → Tailwind `bg-sidebar` `text-sidebar-foreground` `border-sidebar-border` `bg-sidebar-accent` `text-sidebar-accent-foreground` `hover:bg-sidebar-hover` `bg-sidebar-count` `shadow-sidebar-active`. `--header`·`--color-header`는 없앤다(쓰는 곳은 Task 2·이 태스크에서 다른 토큰으로). `rounded-xl` = 16px

- [ ] **Step 1: 브랜치** — `git checkout -b feat/design-refresh` (main에서)

- [ ] **Step 2: 실패하는 테스트** — `frontend/e2e/design.spec.ts`

```ts
// 디자인 토큰·셸 배치(docs/superpowers/specs/2026-11-23-design-refresh-design.md)
import { expect, type Page, test } from "@playwright/test";

const css = (page: Page, sel: string, prop: string) =>
  page.locator(sel).first().evaluate((e, p) => getComputedStyle(e).getPropertyValue(p), prop);
const setTheme = (page: Page, t: "light" | "dark") =>
  page.evaluate((v) => document.documentElement.setAttribute("data-theme", v), t);

test("design: 라이트·다크 바탕과 링크 색", async ({ page }) => {
  await page.goto("/today");
  await setTheme(page, "light");
  expect(await css(page, "body", "background-color")).toBe("rgb(246, 244, 240)");
  expect(await css(page, ":root", "--link")).toBe("#1f5f7a");
  await setTheme(page, "dark");
  expect(await css(page, "body", "background-color")).toBe("rgb(37, 33, 29)");
  expect(await css(page, ":root", "--link")).toBe("#8cc0b8");
});

test("design: 글꼴은 Inter 다음 Pretendard, 숫자 폭 고정", async ({ page }) => {
  await page.goto("/today");
  const family = await css(page, "body", "font-family");
  expect(family.indexOf("Inter")).toBeLessThan(family.indexOf("Pretendard"));
  expect(family).not.toContain("Noto");
  expect(await css(page, "body", "font-variant-numeric")).toBe("tabular-nums");
});
```

(`next/font`는 글꼴 이름을 `'__Inter_xxxx'`처럼 바꾼다 — 단언이 깨지면 `--font-inter`·`--font-pretendard` 변수 순서를 `--font-sans` 값에서 확인하는 쪽으로 바꾼다)

- [ ] **Step 3: 실패 확인** — `npm --prefix frontend run -s e2e -- design.spec.ts` → 두 테스트 FAIL(바탕 `rgb(240, 240, 240)`)

- [ ] **Step 4: 구현**
  - `npm --prefix frontend install pretendard@1.3.9`
  - `layout.tsx`: `Noto_Sans_KR` 대신 `next/font/local`로 `../node_modules/pretendard/dist/web/variable/woff2/PretendardVariable.woff2`, `variable: "--font-pretendard"`, `weight: "45 920"`, `preload: false`(지금 Noto처럼 — 한글 파일이 커서). `<body>`에 `tabular-nums`
  - `globals.css`: `--font-sans: var(--font-inter), var(--font-pretendard), sans-serif`. `:root`·다크 블록의 값을 스펙 표대로(라이트·다크 토큰, 상태 색 4×4, 사이드바). `--radius-xl: calc(var(--radius) + 4px)`(= 16px). 다크에 `--shadow-card`·`--shadow-control`을 `0 0 #0000`으로. 사이드바 토큰 값: 스펙 "사이드바" 문단 — `--sidebar-accent`(선택 칸 바탕), `--sidebar-hover`(라이트 `rgba(0,0,0,.04)`, 다크 `rgba(255,255,255,.05)` — 시안 값), `--shadow-sidebar-active`(라이트 = 스펙의 그림자 + `0 0 0 1px #e2dbd1`, 다크 `0 0 #0000`). 머리 주석의 "Graphite·딥 틸"을 "Mercury 형태 + 웜 차콜(라이트)·Warp(다크)"로
  - `(portal)/layout.tsx` 대기 화면 `bg-header` → `bg-background`

- [ ] **Step 5: 통과 확인** — 같은 e2e → PASS. `npm --prefix frontend run -s typecheck` 통과

- [ ] **Step 6: 커밋** — `frontend: 디자인 토큰·글꼴 — 웜 차콜·Warp, Inter + Pretendard` (globals.css, layout.tsx 두 개, package.json, package-lock.json, e2e/design.spec.ts)

### Task 2: 한 덩어리 사이드바

**Files:**
- Modify: `frontend/components/shell/app-shell.tsx` (`railItem` 44-48, `<aside>` 164-170, 아이콘 줄 172, 좁은 화면 위쪽 줄 218)
- Modify: `frontend/components/shell/space-panel.tsx` (`itemClass` 33-37, 건수 알약 74, 목록 칸 루트 302-306)
- Test: `frontend/e2e/design.spec.ts`

**Interfaces:**
- Consumes: Task 1 사이드바 토큰

- [ ] **Step 1: 실패하는 테스트** (같은 파일에 더한다)

```ts
test("design: 아이콘 줄과 목록 칸은 같은 면, 선택 칸은 흰 바탕", async ({ page }) => {
  await page.goto("/claims");
  await setTheme(page, "light");
  const rail = await css(page, 'aside > div:first-child', "background-color");
  const panel = await css(page, "aside > :nth-child(2)", "background-color");
  expect(rail).toBe("rgb(239, 233, 225)");
  expect(panel).toBe(rail);
  expect(await css(page, 'nav[aria-label="메뉴"] a[aria-current=page]', "background-color")).toBe("rgb(255, 255, 255)");
  expect(await css(page, 'nav[aria-label="공간"] a[aria-current=page]', "background-color")).toBe("rgb(255, 255, 255)");
});

test("design: 테마를 바꾸면 사이드바도", async ({ page }) => {
  await page.goto("/claims");
  await setTheme(page, "dark");
  expect(await css(page, "aside > :nth-child(2)", "background-color")).toBe("rgb(28, 25, 22)");
});
```

- [ ] **Step 2: 실패 확인** — `npm --prefix frontend run -s e2e -- design.spec.ts -g 사이드바|같은 면` → FAIL

- [ ] **Step 3: 구현**
  - `<aside>`에 `border-r border-sidebar-border bg-sidebar`, 아이콘 줄·목록 칸의 `border-r`·`bg-header`·`bg-card`는 지운다(둘 사이 경계 없음)
  - `railItem`·`itemClass`: 기본 `text-sidebar-foreground hover:bg-sidebar-hover`, 선택 `bg-sidebar-accent font-semibold text-sidebar-accent-foreground shadow-sidebar-active`
  - 건수 알약: `bg-sidebar-count text-sidebar-accent-foreground`(지금 `bg-primary text-primary-foreground`)
  - 좁은 화면 위쪽 줄(218): `bg-header` → `bg-sidebar`

- [ ] **Step 4: 통과 확인** — 같은 e2e PASS, 기존 `e2e/shell.spec.ts` PASS

- [ ] **Step 5: 커밋** — `frontend: 아이콘 줄·목록 칸을 한 사이드바로`

### Task 3: 위쪽 제목 띠

**Files:**
- Create: `frontend/components/shell/page-header.tsx`
- Create: `frontend/components/shell/list-expand-button.tsx` (지금 `app-shell.tsx` 233-247의 "목록 펴기" 단추를 옮긴다)
- Modify: `frontend/components/shell/app-shell.tsx` (`<main>` 229-232, 펴기 단추)
- Modify: 11개 `frontend/app/(portal)/{ask,calls,chart-ocr,claims,eval,finance,missed,prep,report,schedule,today}/page.tsx` — `<header>…</header>`를 `<PageHeader>`로
- Modify: `frontend/components/calls/calls-view.tsx:213`, `frontend/components/chart-ocr/chart-ocr-view.tsx:214` (`lg:top-4`)
- Test: `frontend/e2e/design.spec.ts`

**Interfaces:**
- Produces:
  - `PageHeader({ title, description }: { title: string; description: ReactNode })` — 클라이언트 부품, 루트에 `data-slot="page-header"`. 공간 이름은 `spaceOf(usePathname())`로 `lib/nav`의 `SPACES` label
  - 띠의 실제 높이를 `document.documentElement`의 CSS 변수 `--page-band-h`(px)로 알린다(`ResizeObserver`, 언마운트 때 지움). 본문 안 sticky 칸은 `lg:top-[calc(var(--page-band-h,0px)+1rem)]`
  - `ListExpandButton({ className }: { className?: string })` — `saveListCollapsed(false)`, 넓은 화면에서 목록이 접혔을 때만 보임(지금 클래스 그대로)

- [ ] **Step 1: 실패하는 테스트**

```ts
test("design: 업무 화면 제목은 위쪽 띠, AI 비서·EMR은 띠 없음", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/claims");
  const band = page.locator('[data-slot="page-header"]');
  await expect(band.getByRole("heading", { level: 1, name: "청구 점검" })).toBeVisible();
  await expect(band.getByText("업무", { exact: true })).toBeVisible();
  expect(await css(page, '[data-slot="page-header"]', "position")).toBe("sticky");
  await page.mouse.wheel(0, 2000);
  expect((await band.boundingBox())!.y).toBe(0);
  for (const path of ["/", "/emr"]) {
    await page.goto(path);
    await expect(page.locator('[data-slot="page-header"]')).toHaveCount(0);
  }
});

test("design: 띠가 높아져도 sticky 칸이 띠 아래", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  await page.goto("/calls?call=c-0450"); // 상세 칸이 열린 채로(e2e/calls.spec.ts와 같은 통화)
  await page.mouse.wheel(0, 1500);
  const band = (await page.locator('[data-slot="page-header"]').boundingBox())!;
  const panel = (await page.locator(".lg\\:sticky").first().boundingBox())!;
  expect(panel.y).toBeGreaterThanOrEqual(band.y + band.height);
});

test("design: 목록 펴기는 띠 안에", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/claims");
  await page.getByRole("button", { name: "목록 접기" }).click();
  const expandBtn = page.locator('[data-slot="page-header"]').getByRole("button", { name: "목록 펴기" });
  await expect(expandBtn).toBeVisible();
  await expandBtn.click();
  await expect(page.getByRole("navigation", { name: "메뉴" })).toBeVisible();
});

test("design: 인쇄하면 띠는 제목만", async ({ page }) => {
  await page.goto("/prep");
  await page.emulateMedia({ media: "print" });
  const band = page.locator('[data-slot="page-header"]');
  await expect(band.getByRole("heading", { level: 1, name: "준비표" })).toBeVisible();
  await expect(band.getByText("업무", { exact: true })).toBeHidden();
  expect(await css(page, '[data-slot="page-header"]', "position")).toBe("static");
});

test("design: 좁은 화면은 띠가 붙지 않고 가로 넘침 없음", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/today", "/claims", "/ask"]) {
    await page.goto(path);
    expect(await css(page, '[data-slot="page-header"]', "position")).toBe("static");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});
```

- [ ] **Step 2: 실패 확인** — `npm --prefix frontend run -s e2e -- design.spec.ts -g 띠|펴기|인쇄|좁은` → FAIL(`page-header` 없음)

- [ ] **Step 3: `ListExpandButton`** — `app-shell.tsx`의 펴기 단추를 `list-expand-button.tsx`로 옮긴다. 셸은 `space === "assistant"`일 때만 지금 자리(`absolute top-6 left-3`)에 그린다. `<main>`의 `md:list-collapsed:pl-14`도 AI 비서일 때만

- [ ] **Step 4: `PageHeader`**
  - 위치: `md:sticky md:top-0 z-20`, 본문 패딩만큼 음수 여백으로 본문 열 끝까지(`-mx-4 sm:-mx-12 -mt-6 mb-7`), `border-b bg-card px-4 py-3.5 sm:px-12`, 목록 접힘이면 `md:list-collapsed:pl-14` 자리에 `ListExpandButton`
  - 내용: 공간 이름(`text-xs text-muted-foreground`), h1(`text-[1.45rem] font-medium tracking-tight` — 1.45rem은 스펙 값, 글꼴 크기 임의 값이 걸리면 `text-2xl`로 두고 보고), 설명(`text-[13px] text-muted-foreground`)
  - 인쇄: `print:static print:m-0 print:border-0 print:bg-transparent print:p-0`, 공간 이름·펴기 단추·설명은 `print:hidden`(준비표 설명이 지금 `print:hidden`인 것과 같다)
- [ ] **Step 5: 셸 `<main>`** — `mx-auto max-w-[90rem]` 지우고 `px-4 pt-6 pb-12 sm:px-12`
- [ ] **Step 6: 11개 page.tsx** — `<header>` 블록을 `<PageHeader title="…" description={…} />`로. 문장·`&ldquo;` 그대로. 바깥 `div`의 `gap-5`는 그대로
- [ ] **Step 7: sticky 칸** — 두 곳 `lg:top-4` → `lg:top-[calc(var(--page-band-h,0px)+1rem)]`
- [ ] **Step 8: 통과 확인** — `design.spec.ts` 전부 PASS, `make e2e` 전체 PASS(제목 h1을 찾는 기존 테스트 포함)
- [ ] **Step 9: 커밋** — `frontend: 화면 제목을 위쪽 띠로(PageHeader)`

### Task 4: 부품 모양

**Files:**
- Modify: `frontend/components/ui/badge.tsx` (`warn` `danger` `ok` `info` variant 26-29)
- Modify: `border bg-card` 큰 덩어리 — `grep -rnE "rounded-(md|lg) border bg-card" frontend/components` 로 나오는 곳(약 22곳)
- Modify: h2·h3 47곳 — `grep -rnE "<h[23][^>]*font-bold" frontend/components frontend/app`
- Modify: `frontend/components/assistant/assistant-view.tsx:381` (질문 말풍선)
- Test: `frontend/e2e/design.spec.ts`

**Interfaces:**
- Consumes: Task 1 `rounded-xl` = 16px

- [ ] **Step 1: 실패하는 테스트**

```ts
test("design: 상태 딱지는 테두리 없는 알약, 굵기 500", async ({ page }) => {
  await page.goto("/claims");
  const sel = '[data-slot=badge][data-variant=danger]';
  expect(await css(page, sel, "border-top-color")).toBe(await css(page, sel, "background-color"));
  expect(await css(page, sel, "font-weight")).toBe("500");
  // 준비표 경고 띠는 테두리가 남는다(딱지만 바꾼다)
});

test("design: 카드 모서리 16px, 소제목 굵기 500", async ({ page }) => {
  await page.goto("/today");
  expect(await css(page, '[data-slot="card"], .rounded-xl.border', "border-top-left-radius")).toBe("16px");
  expect(await css(page, "main h2", "font-weight")).toBe("500");
});
```

- [ ] **Step 2: 실패 확인** — FAIL(굵기 700, 테두리 색 다름)
- [ ] **Step 3: 딱지** — 네 variant의 `border-status-*-border` → `border-transparent`, `font-bold` → `font-medium`. `kind`·`muted`·`outdated`·`chip`은 그대로
- [ ] **Step 4: 카드** — grep으로 나온 `rounded-md|rounded-lg` + `border bg-card` 블록을 `rounded-xl`로. 작은 상자(입력·칩·경고 띠 `border-status-*`)는 그대로
- [ ] **Step 5: 소제목** — h2·h3의 `font-bold` → `font-medium tracking-tight`(한 커밋 안 기계적 치환, 다른 클래스는 그대로)
- [ ] **Step 6: 말풍선** — `bg-muted` → `border bg-card`(새 바탕과 구분)
- [ ] **Step 7: 통과 확인** — `design.spec.ts` PASS, `make e2e` PASS, `make lint` PASS
- [ ] **Step 8: 커밋** — `frontend: 딱지·카드·소제목 모양 — Mercury`

### Task 5: 시안과 대조·문서

**Files:**
- Modify: `docs/superpowers/specs/2026-11-23-design-refresh-design.md` (구현하며 바뀐 판단만)
- 대조용 시안(저장소 밖): `/private/tmp/claude-501/-Users-hhs-Desktop-pj2/c193c82f-e427-46be-8ffa-7fa786216a09/scratchpad/design-cmp/` — `*-c4-charcoal-linkpetrol.png`(라이트), `*-layout-dark.png`(다크), `detail/*-sel-white*.png`(선택 칸 A), `detail/assistant-dark-link-seafoam*.png`(다크 링크)

- [ ] **Step 1: 스크린숏** — `make dev`를 켠 채 오늘·청구 점검·AI 비서를 1440×960 라이트·다크로 찍는다(스크립트는 scratchpad에, Playwright는 `frontend/node_modules/@playwright/test/index.mjs` 절대 경로로)
- [ ] **Step 2: 대조** — 시안과 나란히 놓고 스펙 "실패로 보는 것" 네 줄을 하나씩 본다: 급한 딱지가 시안보다 덜 보이지 않는가, 링크가 글씨와 구분되는가, 선택 메뉴가 보이는가, 옛 모양(회색·청록)이 남은 화면이 없는가. 남은 옛 모양은 `grep -rnE "bg-header|text-primary.*teal|#115e59" frontend` 로도 확인
- [ ] **Step 3: 나머지 화면 훑기** — 예약·준비표·통화·미예약·재무·리포트·종이 차트·자료실·AI 평가·EMR을 라이트·다크로 한 장씩 찍어 어색한 곳(표 머리, 빈 상태, 대화 상자)을 고친다 — 고친 곳은 같은 태스크 커밋에
- [ ] **Step 4: 전체 확인** — `make fmt` → `make lint` → `make test` → `make e2e` 모두 PASS
- [ ] **Step 5: 커밋** — `frontend: 새 디자인 화면별 다듬기` / 스펙을 고쳤으면 `docs: 디자인 새로 입히기 설계 — 구현하며 바뀐 판단`
