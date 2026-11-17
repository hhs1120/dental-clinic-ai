// 오늘 화면 — 카드·사람이 볼 일·AI가 한 일(기능 정의 §4 오늘 화면). 실데이터는 자동 처리·사람 판정·거절 질문이 0건이라
// /api/today를 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

const EMPTY = {
  date: "2026-12-01",
  cards: {
    appointments: { total: 0, treatment: 0, hygiene: 0, consult: 0 },
    new_patients: 0,
    alert_patients: 0,
    todo: { items: 0, calls: 0, urgent_items: 0 },
  },
  people: {
    urgent_calls: [],
    review_calls: [],
    unconfirmed: 0,
    alert_patients: 0,
    todo_left: { items: 0, calls: 0 },
    judge_pending: 0,
    refused: [],
    appeal_soon: 0,
    preclaim_warning: 0,
    preclaim_ai: 0,
    recon_open: 0,
    recon_month: null,
    expense_open: 0,
    expense_month: null,
    wants_appt_unbooked: { count: 0, names: [], days: 7 },
  },
  briefing: [],
  ai: {
    nightly: null,
    autoclose_on: false,
    auto_total: 0,
    auto_recent: [],
    recon_auto: { on: false, total: 0, recent: [] },
    expense_auto: { on: false, total: 0, recent: [] },
    prep: { rows: 0, suggest: 0, failed: 0 },
    classify_failed: 0,
    followup_failed: 0,
  },
};

const REVIEW = [
  {
    call_uid: "c-0450",
    name: "이영자",
    at: "2026-11-27 10:00:00",
    ai: "none",
    reopened: false,
    flags: ["emergency", "complaint"],
  },
  {
    call_uid: "c-0451",
    name: "김민지",
    at: "2026-11-28 11:00:00",
    ai: "closed",
    reopened: true,
    flags: [],
  },
  {
    call_uid: "c-0300",
    name: "박준호",
    at: "2026-11-10 09:00:00",
    ai: "old_todo",
    reopened: false,
    flags: ["urgent"],
  },
];

const AUTO = {
  id: 1,
  call_uid: "c-0460",
  name: "최수진",
  reason: "두 모델 모두 할 일 없음 · 응급·불만·급함·엇갈림 없음",
  at: "2026-12-01 03:10:00",
};

const FULL = {
  ...EMPTY,
  cards: {
    appointments: { total: 26, treatment: 17, hygiene: 7, consult: 2 },
    new_patients: 4,
    alert_patients: 6,
    todo: { items: 34, calls: 16, urgent_items: 4 },
  },
  people: {
    urgent_calls: [
      {
        call_uid: "c-0470",
        name: "정다은",
        text: "통증 문의 회신",
        at: "2026-11-30 09:00:00",
        emergency: true,
      },
    ],
    review_calls: REVIEW,
    unconfirmed: 1,
    alert_patients: 6,
    todo_left: { items: 34, calls: 16 },
    judge_pending: 3,
    refused: [
      {
        id: 7,
        kind: "chat",
        question: "유치는 언제 빠져?",
        at: "2026-11-09 16:12:00",
      },
    ],
    wants_appt_unbooked: { count: 0, names: [], days: 7 },
  },
  ai: {
    autoclose_on: true,
    auto_total: 1,
    auto_recent: [AUTO],
    recon_auto: {
      on: true,
      total: 1,
      recent: [
        {
          id: 2,
          key: "cccccccccccc",
          date: "2026-11-05",
          cause: "fee_mismatch",
          at: "2026-12-01 06:00:00",
        },
      ],
    },
    expense_auto: { on: false, total: 0, recent: [] },
    prep: { rows: 26, suggest: 12, failed: 1 },
    classify_failed: 0,
    followup_failed: 0,
  },
};

// 개발 서버는 화면을 두 번 그려 조회가 두 번 갈 수 있다 — 응답은 요청 순번이 아니라 함수로 정한다
async function fakeToday(page: Page, body: unknown | (() => unknown)) {
  await page.route("**/api/today", (r) =>
    r.fulfill({ json: typeof body === "function" ? body() : body }),
  );
}

test("카드와 줄을 누르면 그 화면으로", async ({ page }) => {
  await fakeToday(page, FULL);
  await page.goto("/today");
  // 카드는 이름 → 숫자 순. 할 일 카드의 두 숫자는 같은 단위(줄 수), 급함은 빨간 배지
  const todo = page.getByRole("link", { name: /^오늘 할 일\s*34건/ });
  await expect(todo.locator('[data-variant="danger"]')).toHaveText("급함 4");
  await page.getByRole("link", { name: /^주의 환자\s*6명$/ }).click(); // 카드(세부 없음 — 같은 이름의 줄과 구분)
  await expect(page).toHaveURL(/\/prep\?chip=alert$/);
  await page.goto("/today");
  await page.getByRole("link", { name: /이영자/ }).click();
  await expect(page).toHaveURL(/\/calls\?call=c-0450$/);
  await page.goto("/today");
  await page.getByRole("link", { name: /내원 확인 전 1명/ }).click();
  await expect(page).toHaveURL(/\/prep\?chip=unconfirmed$/);
  await page.goto("/today");
  await page.getByRole("link", { name: /유치는 언제 빠져/ }).click();
  await expect(page.getByLabel("말 입력")).toHaveValue(/유치는 언제 빠져/);
});

// 미예약 환자 줄은 숫자 없는 고정 링크라 0건 숨김 없이 늘 보인다 — 문구(MISSED_LINK_TEXT)는 바뀔 수 있어 링크 주소로 찾는다
test("미예약 환자 줄은 늘 있고 → /missed", async ({ page }) => {
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  const people = page.getByRole("region", { name: "사람이 볼 일" });
  await expect(people.getByText("오늘 남은 일이 없습니다")).toBeVisible();
  const link = people.locator('a[href="/missed"]');
  await expect(link).toHaveCount(1);
  await expect(link).not.toContainText(/\d/);
  await link.click();
  await expect(page).toHaveURL(/\/missed$/);
});

test("확인할 통화 문구", async ({ page }) => {
  await fakeToday(page, FULL);
  await page.goto("/today");
  const people = page.getByRole("region", { name: "사람이 볼 일" });
  await expect(people).toContainText("AI: 할 일 없음, 사람 판정: 응급·불만");
  await expect(people).toContainText("AI: 종결, 사람: 다시 열림");
  await expect(people).toContainText(
    "AI: 할 일 있음(지난 통화), 사람 판정: 급함",
  );
  await expect(people).toContainText("이영자 · AI: 할 일 없음");
  await expect(people).toContainText("11/27");
  for (const code of ["emergency", "has_todo", "old_todo", "closed", "refused"])
    await expect(page.getByText(code)).toHaveCount(0);
});

test("0건 줄은 숨김", async ({ page }) => {
  await fakeToday(page, {
    ...FULL,
    people: { ...FULL.people, refused: [], review_calls: [] },
  });
  await page.goto("/today");
  await expect(page.getByText("근거 없음")).toHaveCount(0);
  await expect(page.getByText("확인할 통화")).toHaveCount(0);
  await expect(page.getByText("급한 통화")).toBeVisible();
  await page.unroute("**/api/today");
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText("오늘 남은 일이 없습니다")).toBeVisible();
});

test("AI가 한 일은 접힌 채 요약, 펼치면 다시 열기", async ({ page }) => {
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText(/자동 종결 꺼짐/)).toBeVisible();
  await page.unroute("**/api/today");
  await fakeToday(page, FULL);
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await expect(ai).toContainText(
    "자동 처리 1건 · 일계표 자동 마감 1건 · 오늘 준비표 26행 · 실패 1",
  );
  await expect(ai.getByRole("button", { name: "다시 열기" })).toHaveCount(0);
  await ai.getByRole("button", { name: "펼치기" }).click();
  await expect(ai.getByRole("button", { name: "다시 열기" })).toBeVisible();
  await expect(ai).toContainText("최수진");
  await expect(ai).toContainText("사람에게 넘김"); // 준비표 후검증 실패 1
  await expect(ai).toContainText("끝남"); // 분류 실패 0
  await expect(ai.getByRole("link", { name: /전체 보기/ })).toHaveAttribute(
    "href",
    "/calls?tab=history",
  );
});

test("다시 열기 — AI가 한 일에서 빠지고 확인할 통화에 뜸", async ({ page }) => {
  const after = {
    ...FULL,
    people: {
      ...FULL.people,
      review_calls: [
        {
          call_uid: "c-0460",
          name: "최수진",
          at: "2026-11-29 10:00:00",
          ai: "closed",
          reopened: true,
          flags: [],
        },
      ],
    },
    ai: { ...FULL.ai, auto_total: 0, auto_recent: [] },
  };
  let release: () => void = () => {};
  const held = new Promise<void>((res) => (release = res));
  const posted: string[] = [];
  let reverted = false;
  await fakeToday(page, () => (reverted ? after : FULL));
  await page.route("**/api/auto-actions/1/revert", async (r) => {
    posted.push(r.request().postData() ?? "");
    await held;
    reverted = true;
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await ai.getByRole("button", { name: "펼치기" }).click();
  const btn = ai.getByRole("button", { name: "다시 열기" });
  await btn.click();
  await expect(btn).toBeDisabled(); // 응답 전에 꺼져 두 번 누를 수 없다
  release();
  await expect(ai).not.toContainText("최수진");
  await expect(
    page.getByRole("region", { name: "사람이 볼 일" }),
  ).toContainText("최수진 · AI: 종결, 사람: 다시 열림");
  expect(posted).toHaveLength(1);
});

test("이미 다시 연 건 — 상태 코드로 알아보고 오류 띠 없이 다시 조회", async ({
  page,
}) => {
  let posted = false;
  await fakeToday(page, () =>
    posted
      ? { ...FULL, ai: { ...FULL.ai, auto_total: 0, auto_recent: [] } }
      : FULL,
  );
  await page.route("**/api/auto-actions/1/revert", (r) => {
    posted = true;
    // 서버 문구가 바뀌어도 409면 "이미 처리됨"으로 본다
    return r.fulfill({ status: 409, json: { detail: "다른 문구" } });
  });
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await ai.getByRole("button", { name: "펼치기" }).click();
  await ai.getByRole("button", { name: "다시 열기" }).click();
  await expect(ai.getByRole("button", { name: "다시 열기" })).toHaveCount(0);
  await expect(page.getByText("다른 문구")).toHaveCount(0); // 오류 띠 없음
});

test("재심 기한 임박·청구 전 상병 경고 줄 → /claims", async ({ page }) => {
  await fakeToday(page, {
    ...EMPTY,
    people: { ...EMPTY.people, appeal_soon: 36, preclaim_warning: 7 },
  });
  await page.goto("/today");
  const people = page.getByRole("region", { name: "사람이 볼 일" });
  await expect(people).toContainText("30일 이내 · 상병 착오 제외");
  await people.getByRole("link", { name: /재심 신청 기한 임박 36건/ }).click();
  await expect(page).toHaveURL(/\/claims\?chip=soon$/);
  await page.goto("/today");
  await page
    .getByRole("region", { name: "사람이 볼 일" })
    .getByRole("link", { name: /청구 전 상병 경고 7건/ })
    .click();
  await expect(page).toHaveURL(/\/claims\?tab=pre$/);
  await page.unroute("**/api/today");
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText("오늘 남은 일이 없습니다")).toBeVisible();
  await expect(page.getByText(/재심 신청 기한 임박/)).toHaveCount(0);
  await expect(page.getByText(/청구 전 상병 경고/)).toHaveCount(0);
});

test("청구 전 AI 줄 → 청구 전 점검 AI 칩", async ({ page }) => {
  await fakeToday(page, {
    ...EMPTY,
    people: { ...EMPTY.people, preclaim_ai: 2 },
  });
  await page.goto("/today");
  await page
    .getByRole("region", { name: "사람이 볼 일" })
    .getByRole("link", { name: /청구 전 차트에 요건 근거 못 찾음 2건\(AI\)/ })
    .click();
  await expect(page).toHaveURL(/\/claims\?tab=pre&chip=ai$/);
});

test("일계표 불일치 줄 → 미확정이 있는 가장 이른 달", async ({ page }) => {
  await fakeToday(page, {
    ...EMPTY,
    people: { ...EMPTY.people, recon_open: 3, recon_month: "2026-08" },
  });
  await page.goto("/today");
  await page
    .getByRole("region", { name: "사람이 볼 일" })
    .getByRole("link", { name: /일계표 불일치 3건/ })
    .click();
  await expect(page).toHaveURL(/\/finance\?month=2026-08$/);
  await page.unroute("**/api/today");
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText(/일계표 불일치/)).toHaveCount(0);
});

test("지출 증빙 확인 줄 → 지출 증빙 탭 지난달", async ({ page }) => {
  await fakeToday(page, {
    ...EMPTY,
    people: { ...EMPTY.people, expense_open: 2, expense_month: "2026-11" },
  });
  await page.goto("/today");
  await page
    .getByRole("region", { name: "사람이 볼 일" })
    .getByRole("link", { name: /지출 증빙 확인 2건/ })
    .click();
  await expect(page).toHaveURL(/\/finance\?tab=expense&month=2026-11$/);
  await page.unroute("**/api/today");
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText(/지출 증빙 확인/)).toHaveCount(0);
});

test("일계표 자동 마감 — AI가 한 일 목록과 되돌리기", async ({ page }) => {
  let reverted = false;
  await fakeToday(page, () =>
    reverted
      ? {
          ...FULL,
          ai: { ...FULL.ai, recon_auto: { on: true, total: 0, recent: [] } },
        }
      : FULL,
  );
  await page.route("**/api/auto-actions/2/revert", (r) => {
    reverted = true;
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await expect(ai).toContainText("일계표 자동 마감 1건");
  await ai.getByRole("button", { name: "펼치기" }).click();
  await expect(ai).toContainText("2026-11-05 · 수수료 차이");
  await ai.getByRole("button", { name: "되돌리기" }).click();
  await expect(ai).toContainText("자동으로 마감한 불일치 건이 없습니다");
});

const EXPENSE_AUTO = {
  on: true,
  total: 1,
  recent: [
    {
      id: 3,
      key: "dddddddddddd",
      at: "2026-11-10 10:00:00",
      label: "이음통신",
      amount: 33000,
      account: "통신비",
    },
  ],
};

test("지출 증빙 자동 확정 목록", async ({ page }) => {
  await fakeToday(page, {
    ...FULL,
    ai: { ...FULL.ai, expense_auto: EXPENSE_AUTO },
  });
  await page.goto("/today");
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  await expect(ai).toContainText("지출 증빙 자동 확정 1건");
  await ai.getByRole("button", { name: "펼치기" }).click();
  await expect(ai).toContainText("11/10 · 이음통신 · 33,000원 → 통신비");
});

test("아침 브리핑 — 머리말 2열, 같은 머리말은 첫 줄에만, 없으면 카드 숨김", async ({
  page,
}) => {
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByLabel("아침 브리핑")).toHaveCount(0);
  await page.unroute("**/api/today");
  await fakeToday(page, {
    ...FULL,
    briefing: [
      { head: "오늘", text: "예약 26건(신환 4) · 10시·14시·17시대에 몰림" },
      { head: "챙길 환자", text: "10:30 배정웅 단순발치 — 항응고제 복용" },
      { head: "챙길 환자", text: "15:30 안서준 임플란트 1차 — 당뇨" },
    ],
    ai: {
      ...FULL.ai,
      nightly: {
        finished_at: "2026-12-01T03:12:00",
        state: "done",
        recent: true,
        failed: 2,
      },
    },
  });
  await page.goto("/today");
  const card = page.getByLabel("아침 브리핑");
  await expect(card).toContainText("예약 26건(신환 4)");
  await expect(card.getByText("챙길 환자")).toHaveCount(1);
  await expect(card).toContainText("15:30 안서준 임플란트 1차 — 당뇨");
  await expect(page.getByRole("region", { name: "AI가 한 일" })).toContainText(
    "야간 실행 03:12 끝남 · 야간 실패 2",
  );
});

test("야간 실행 꼬리표 — 오래됨·상한·끊김", async ({ page }) => {
  const ai = page.getByRole("region", { name: "AI가 한 일" });
  const night = (n: object) =>
    fakeToday(page, { ...FULL, ai: { ...FULL.ai, nightly: n } });
  await night({
    finished_at: "2026-11-28T03:12:00",
    state: "done",
    recent: false,
    failed: 0,
  });
  await page.goto("/today");
  await expect(ai).toContainText("야간 실행 11/28 03:12 끝남(오래됨)");
  await page.unroute("**/api/today");
  await night({
    finished_at: "2026-12-01T03:05:00",
    state: "budget",
    recent: true,
    failed: 0,
  });
  await page.goto("/today");
  await expect(ai).toContainText("야간 실행 03:05 상한으로 중단");
  await page.unroute("**/api/today");
  await night({ finished_at: null, state: "broken", recent: false, failed: 0 });
  await page.goto("/today");
  await expect(ai).toContainText("야간 실행 끊김");
});

// 사람이 볼 일 — 통화에서 온 일 / 업무에서 온 일 두 묶음(기능 정의 §4 오늘 화면). 넓은 화면은 좌우, 통화가 왼쪽
test("사람이 볼 일은 통화에서 온 일과 업무에서 온 일로 나뉜다", async ({
  page,
}) => {
  await fakeToday(page, FULL);
  await page.goto("/today");
  const calls = page.getByRole("region", { name: "통화에서 온 일" });
  const work = page.getByRole("region", { name: "업무에서 온 일" });
  for (const tag of ["급한 통화", "확인할 통화", "남은 할 일"])
    await expect(calls.getByText(tag, { exact: true }).first()).toBeVisible();
  for (const tag of ["내원 확인 전", "주의 환자", "판정 대기", "근거 없음"])
    await expect(work.getByText(tag, { exact: true }).first()).toBeVisible();
  await expect(calls.getByText("주의 환자", { exact: true })).toHaveCount(0);
  await expect(work.getByText("급한 통화", { exact: true })).toHaveCount(0);
  // 소제목 숫자는 줄 수 — 미예약 환자 고정 링크는 세지 않고 업무 쪽 맨 끝
  // 제목과 숫자 사이 공백 — 스크린리더가 붙여 읽지 않게, 숫자는 끝까지 맞춘다("14"에 통과하지 않게)
  await expect(calls.getByRole("heading")).toHaveText(
    `통화에서 온 일 ${1 + REVIEW.length + 1}`,
  );
  await expect(work.getByRole("heading")).toHaveText("업무에서 온 일 4");
  await expect(work.getByRole("listitem").last().locator("a")).toHaveAttribute(
    "href",
    "/missed",
  );
  const [c, w] = [await calls.boundingBox(), await work.boundingBox()];
  expect(c!.x).toBeLessThan(w!.x);
});

test("통화에서 온 일이 없으면 한 줄로 알린다", async ({ page }) => {
  await fakeToday(page, {
    ...FULL,
    people: {
      ...FULL.people,
      urgent_calls: [],
      review_calls: [],
      todo_left: { items: 0, calls: 0 },
    },
  });
  await page.goto("/today");
  await expect(
    page
      .getByRole("region", { name: "통화에서 온 일" })
      .getByText("통화에서 온 일 없음"),
  ).toBeVisible();
  await expect(page.getByText("오늘 남은 일이 없습니다")).toHaveCount(0);
});

test("둘 다 비면 남은 일 없음 한 번만", async ({ page }) => {
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText("오늘 남은 일이 없습니다")).toBeVisible();
  await expect(page.getByText("통화에서 온 일 없음")).toHaveCount(0);
});

test("예약 원함 줄과 AI 비서로 처리 링크", async ({ page }) => {
  await fakeToday(page, {
    ...EMPTY,
    people: {
      ...EMPTY.people,
      appeal_soon: 2,
      recon_open: 3,
      recon_month: "2026-08",
      wants_appt_unbooked: { count: 2, names: ["김영철", "박순자"], days: 5 },
    },
  });
  await page.goto("/today");
  const people = page.getByRole("region", { name: "사람이 볼 일" });
  await expect(people.getByText(/예약 원함 2명/)).toBeVisible();
  await expect(people.getByText(/김영철, 박순자/)).toBeVisible();
  const links = people.getByRole("link", { name: "AI 비서로 처리" });
  await expect(links).toHaveCount(3);
  const q = (t: string) => `/?${new URLSearchParams({ draft: t })}`;
  await expect(links.nth(0)).toHaveAttribute(
    "href",
    q("지난 5일 전화로 예약 원했는데 안 잡힌 사람 보여줘"), // 날수는 응답 값
  );
  await expect(
    people.locator(
      `a[href="${q("재심 신청 기한이 얼마 안 남은 삭감 건 보여줘")}"]`,
    ),
  ).toHaveCount(1);
  await expect(
    people.locator(`a[href="${q("일계표 불일치 있는 날 보여줘")}"]`),
  ).toHaveCount(1);
  await page.unroute("**/api/today");
  await fakeToday(page, EMPTY);
  await page.goto("/today");
  await expect(page.getByText(/예약 원함/)).toHaveCount(0);
  await expect(page.getByRole("link", { name: "AI 비서로 처리" })).toHaveCount(
    0,
  );
});
