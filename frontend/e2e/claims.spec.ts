// ⑤ 청구 점검 화면(설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1) — 실데이터엔 판단 기록·경계 사례가
// 없고 판단은 앱 DB에 남으므로 /api/claims·/api/claims/pre는 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

type Judgment = { value: string; by: string; at: string } | null;

const base = {
  chart_no: "C-00001",
  service_date: "2026-08-01",
  tooth_no: "00",
  status: "adjusted",
  expired: false,
  judgment: null as Judgment,
  suggest_by: null as string | null,
  ai_evidence: null as { source: string; quote: string }[] | null,
  standard: null as { doc_title: string; heading: string; body: string } | null,
  chart_quotes: null as string[] | null,
  appeal: null as unknown,
};

const DOC_STANDARD = {
  doc_title: "공통 분야 청구 기준",
  heading: "내역설명 기재",
  body: "- 요건을 충족했음을 보여 주는 근거를 내역설명란에 적는다.\n- 근거는 간결하게 적는다.",
};
const DOC_QUOTE = "치주낭 5mm, 출혈 있음";

const DOC = {
  ...base,
  claim_id: 11,
  chart_no: "C-00011",
  name: "김하나",
  tooth_no: "36",
  procedure_name: "신경치료",
  status: "denied",
  claim_amount: 12340,
  paid_amount: 0,
  unpaid: 12340,
  adjust_note: "내역설명 미기재로 인정 근거 없음",
  type: "documentation",
  reviewed_at: "2026-09-09",
  deadline: "2026-12-08",
  days_left: 7,
  chips: ["all", "soon", "denied"],
  suggestion: "resubmit",
  suggest_by: "rule",
  reason: "빠진 기재·증빙을 채워 다시 청구할 수 있습니다",
  fill_item: "내역설명 기재",
  standard: DOC_STANDARD,
  chart_quotes: [DOC_QUOTE],
};
const DX = {
  ...base,
  claim_id: 13,
  name: "박세나",
  procedure_name: "스케일링",
  claim_amount: 60000,
  paid_amount: 30000,
  unpaid: 30000,
  adjust_note: "상병과 시행 행위 불일치",
  type: "diagnosis",
  reviewed_at: "2026-09-22",
  deadline: "2026-12-21",
  days_left: 20,
  chips: ["all", "diagnosis", "adjusted"],
  suggestion: "justified",
  suggest_by: "rule",
  reason: "상병 착오는 재청구할 수 없습니다 — 다음 청구 때 상병을 확인하세요",
  fill_item: null,
};
const REQ = {
  ...base,
  claim_id: 12,
  name: "이두리",
  procedure_name: "잇몸치료",
  claim_amount: 35000,
  paid_amount: 20000,
  unpaid: 15000,
  adjust_note: "치주치료 산정 기준 소견 미확인",
  type: "requirement",
  reviewed_at: "2026-10-10",
  deadline: "2027-01-08",
  days_left: 38,
  chips: ["all", "adjusted"],
  suggestion: null,
  reason: "차트에 요건 근거가 있으면 재심 신청, 없으면 삭감 수용입니다",
  fill_item: null,
};
const OLD = {
  ...base,
  claim_id: 14,
  name: "최네오",
  tooth_no: "46",
  procedure_name: "아말감/GI",
  service_date: "2025-11-20",
  claim_amount: 25000,
  paid_amount: 15000,
  unpaid: 10000,
  adjust_note: "동일 부위 재시행 간격 기준 미충족",
  type: "requirement",
  reviewed_at: "2026-05-11",
  deadline: "2026-08-09",
  days_left: -114,
  expired: true,
  chips: ["expired"],
  suggestion: null,
  reason: "차트에 요건 근거가 있으면 재심 신청, 없으면 삭감 수용입니다",
  fill_item: null,
  judgment: { value: "contestable", by: "원장", at: "2026-08-01 10:00:00" },
};

const CHIPS = ["all", "diagnosis", "soon", "denied", "adjusted", "expired"];

function sheet(rows: (typeof DOC | typeof DX | typeof REQ | typeof OLD)[]) {
  return {
    date: "2026-12-01",
    appeal_days: 90,
    soon_days: 30,
    watch_days: 60,
    chips: Object.fromEntries(
      CHIPS.map((c) => {
        const hit = rows.filter((r) => r.chips.includes(c));
        return [
          c,
          {
            count: hit.length,
            amount: hit.reduce((s, r) => s + r.unpaid, 0),
          },
        ];
      }),
    ),
    rows,
  };
}

const PRE = {
  counts: { lines: 2, warning: 1, ai: 0, ai_checked: 0 },
  ai_at: null as string | null,
  ai_pending: { pre: 0, adjust: 0 },
  rows: [
    {
      claim_id: 21,
      service_date: "2026-11-12",
      chart_no: "C-00021",
      name: "정우리",
      procedure_name: "사랑니 발치",
      tooth_no: "38",
      claim_amount: 90000,
      dx_code: "D05",
      dx_name: "만성 치주염",
      warning: { allowed: ["매복치"], past_count: 2, past_amount: 22000 },
      ai: null as unknown,
    },
    {
      claim_id: 22,
      service_date: "2026-11-10",
      chart_no: "C-00022",
      name: "한가람",
      procedure_name: "스케일링",
      tooth_no: "00",
      claim_amount: 60000,
      dx_code: "D06",
      dx_name: "치아 침착물(치석)",
      warning: null,
      ai: null as unknown,
    },
  ],
};

// 개발 서버는 화면을 두 번 그려 조회가 두 번 갈 수 있다 — 응답은 요청 순번이 아니라 함수로 정한다
async function fakeClaims(
  page: Page,
  body: () => unknown,
  pre: () => unknown = () => PRE,
) {
  await page.route("**/api/claims", (r) => r.fulfill({ json: body() }));
  await page.route("**/api/claims/pre", (r) => r.fulfill({ json: pre() }));
}

const STD_QUOTE = "매복 정도와 골삭제 여부를 진료기록에 반드시 적는다";
const OMIT_QUOTE = "이어서 12번 치아 아말감/GI 함께 진행함";
const PRE_AI = {
  ...PRE,
  counts: { lines: 2, warning: 1, ai: 2, ai_checked: 2 },
  ai_at: "2026-11-11T09:30:00",
  ai_pending: { pre: 3, adjust: 2 },
  rows: [
    {
      ...PRE.rows[0],
      ai: {
        requirement: "missing",
        evidence: [{ source: "standard", quote: STD_QUOTE }],
        omissions: [],
      },
    },
    {
      ...PRE.rows[1],
      // 요건 대상이 아닌 줄에 2b 누락만(설계 §2b)
      ai: {
        requirement: null,
        evidence: [],
        omissions: [
          {
            procedure_code: "U0006",
            procedure: "아말감/GI",
            tooth: "12",
            quote: OMIT_QUOTE,
          },
        ],
      },
    },
  ],
};

const ALL = [DOC, DX, REQ, OLD];
const table = (page: Page) => page.getByRole("table");
const bodyRows = (page: Page) => table(page).locator("tbody > tr[data-claim]");

test("메뉴에서 청구 점검이 열리고 기본은 삭감 대응", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/today");
  await page
    .getByRole("navigation", { name: "메뉴" })
    .getByRole("link", { name: "청구 점검" })
    .click();
  await expect(page).toHaveURL(/\/claims$/);
  await expect(page.getByRole("tab", { name: "삭감 대응" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByText("준비 중")).toHaveCount(0);
  await expect(bodyRows(page)).toHaveCount(3);
});

test("5칸과 기한 표시", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await expect(table(page).getByRole("columnheader")).toHaveText([
    "재심 신청 기한 ↓",
    "환자 · 시술",
    "못 받은 돈",
    "왜 깎였나",
    "할 일",
  ]);
  const doc = bodyRows(page).filter({ hasText: "김하나" });
  await expect(doc).toContainText("12/08 · 7일 남음");
  await expect(doc).toContainText("30일 이내");
  await expect(doc).toContainText("12,340원 (전액)");
  await expect(doc).toContainText("내역설명 미기재로 인정 근거 없음");
  await expect(doc).toContainText("진료일 08/01 · 36번");
  await expect(doc).toContainText("판단 필요");
  await expect(doc).toContainText("추천: 보완 후 재청구");
  const req = bodyRows(page).filter({ hasText: "이두리" });
  await expect(req).toContainText("01/08 · 38일 남음"); // 다음 해 기한도 월/일
  await expect(req).toContainText("60일 이내");
  await expect(req).toContainText("차트 확인 후 결정");
  const dx = bodyRows(page).filter({ hasText: "박세나" });
  await expect(dx).toContainText("12/21 · 20일 남음");
  await expect(dx).toContainText("재청구 불가");
  await expect(dx).toContainText("추천: 삭감 수용");
  for (const code of ["C", "justified", "U0001", "documentation", "denied"])
    await expect(page.getByText(code, { exact: true })).toHaveCount(0);
});

test("칩 건수와 합계", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await expect(page.getByText("못 받은 돈 합계 57,340원")).toBeVisible();
  await page.getByRole("button", { name: "신청 기한 30일 이내 1" }).click();
  await expect(page).toHaveURL(/\/claims\?chip=soon$/);
  await expect(bodyRows(page)).toHaveCount(1);
  await expect(page.getByText("못 받은 돈 합계 12,340원")).toBeVisible();
  await page.getByRole("button", { name: "기한 지남 1" }).click();
  await expect(page).toHaveURL(/chip=expired/);
  const old = bodyRows(page).filter({ hasText: "최네오" });
  await expect(old).toContainText("08/09");
  await expect(old.getByText("기한 지남", { exact: true })).toBeVisible();
  await expect(old).toContainText("진료일 2025/11/20 · 46번");
  await expect(old).toContainText("재심 신청 · 원장 08/01");
  await old.click();
  const detail = page.getByRole("region", { name: "최네오 청구 상세" });
  await expect(detail).toBeVisible();
  for (const name of ["삭감 수용", "재심 신청", "보완 후 재청구"])
    await expect(detail.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );
});

test("상병 착오는 삭감 수용만", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "박세나" }).click();
  const detail = page.getByRole("region", { name: "박세나 청구 상세" });
  await expect(
    detail.getByRole("button", { name: "삭감 수용", exact: true }),
  ).toHaveCount(1);
  await expect(
    detail.getByRole("button", { name: "재심 신청", exact: true }),
  ).toHaveCount(0);
  await expect(
    detail.getByRole("button", { name: "보완 후 재청구", exact: true }),
  ).toHaveCount(0);
});

const names = (page: Page) =>
  bodyRows(page).locator("td:nth-child(2) .font-bold");

test("정렬 — 기한 가까운 순 기본, 못 받은 돈 큰 순", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await expect(page.getByText("기한 가까운 순", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "재심 신청 기한" }),
  ).toHaveAttribute("aria-sort", "ascending");
  await expect(names(page)).toHaveText([/김하나/, /박세나/, /이두리/]);
  await page.getByRole("button", { name: "못 받은 돈" }).click();
  await expect(
    page.getByText("못 받은 돈 큰 순", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "못 받은 돈" }),
  ).toHaveAttribute("aria-sort", "descending");
  await expect(names(page)).toHaveText([/박세나/, /이두리/, /김하나/]);
  await page.getByRole("button", { name: "재심 신청 기한" }).click();
  await expect(names(page).first()).toHaveText(/김하나/);
});

test("기한 지남 칩은 최근에 지난 순(서버 순서)", async ({ page }) => {
  const OLDER = { ...OLD, claim_id: 15, name: "정오래", unpaid: 50000 };
  await fakeClaims(page, () => sheet([OLD, OLDER]));
  await page.goto("/claims?chip=expired");
  await expect(page.getByText("최근에 지난 순", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("columnheader", { name: "재심 신청 기한" }),
  ).toHaveAttribute("aria-sort", "descending");
  await expect(names(page)).toHaveText([/최네오/, /정오래/]);
  await page.getByRole("button", { name: "못 받은 돈" }).click();
  await expect(names(page)).toHaveText([/정오래/, /최네오/]);
});

test("기한 지난 상병 착오는 두 딱지 다", async ({ page }) => {
  await fakeClaims(page, () =>
    sheet([
      {
        ...DX,
        deadline: "2026-10-02",
        days_left: -60,
        expired: true,
        chips: ["expired"],
      },
    ]),
  );
  await page.goto("/claims?chip=expired");
  const dx = bodyRows(page).filter({ hasText: "박세나" });
  await expect(dx).toContainText("10/02");
  await expect(dx.getByText("기한 지남", { exact: true })).toBeVisible();
  await expect(dx).toContainText("재청구 불가");
});

test("판단을 누르면 기록되고 할 일 칸이 바뀜", async ({ page }) => {
  let judged: Judgment = null;
  await fakeClaims(page, () =>
    sheet([DOC, DX, { ...REQ, judgment: judged }, OLD]),
  );
  let posted: unknown = null;
  await page.route("**/api/claims/12/judgment", async (r) => {
    posted = r.request().postDataJSON();
    judged = { value: "contestable", by: "데스크", at: "2026-11-09 10:00:00" };
    await r.fulfill({ json: { ok: true } });
  });
  await page.goto("/claims");
  const req = bodyRows(page).filter({ hasText: "이두리" });
  await req.click();
  const detail = page.getByRole("region", { name: "이두리 청구 상세" });
  await detail.getByRole("button", { name: "재심 신청", exact: true }).click();
  await expect(req).toContainText("재심 신청 · 데스크 11/09");
  expect(posted).toEqual({ judgment: "contestable", part: "데스크" });
});

test("보완 후 재청구 추천은 채울 항목", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "김하나" }).click();
  const detail = page.getByRole("region", { name: "김하나 청구 상세" });
  await expect(detail).toContainText("채울 항목: 내역설명 기재");
  await expect(detail).toContainText("청구 12,340원 → 지급 0원");
  await expect(detail).toContainText("전액 불능");
});

test("펼친 곳 — 기준 조항 카드와 서류 사유 차트 문장(AI)", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "김하나" }).click();
  const detail = page.getByRole("region", { name: "김하나 청구 상세" });
  await expect(detail).toContainText("공통 분야 청구 기준 › 내역설명 기재");
  await expect(detail).toContainText("근거는 간결하게 적는다");
  await expect(detail).toContainText("차트에서 옮겨 적을 문장(AI)");
  await expect(detail).toContainText(`“${DOC_QUOTE}”`);
});

test("서류 사유 빈 답은 '없음', 결과 없는 행은 아무 줄도", async ({ page }) => {
  await fakeClaims(page, () =>
    sheet([
      { ...DOC, chart_quotes: [] },
      { ...DX, claim_id: 15, chart_quotes: null },
    ]),
  );
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "김하나" }).click();
  const doc = page.getByRole("region", { name: "김하나 청구 상세" });
  await expect(doc).toContainText("차트에 옮겨 적을 문장 없음");
  await bodyRows(page).filter({ hasText: "박세나" }).click();
  const dx = page.getByRole("region", { name: "박세나 청구 상세" });
  await expect(dx).not.toContainText("옮겨 적을");
});

test("청구 기준 찾기 — 찾은 조항, 닫기, 색인 없음", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  let status = 200;
  await page.route("**/api/claims/standards?**", (r) =>
    status === 200
      ? r.fulfill({
          json: {
            results: [
              {
                chunk_id: "doc:2:1",
                doc_title: "치주 분야 청구 기준",
                heading: "치석제거 뒤 치주치료 연결",
                text: "- 치석제거를 시행한 날부터 90일 이내에 이어서 치주치료를 하면 재평가 소견을 적는다.",
              },
            ],
          },
        })
      : r.fulfill({
          status: 503,
          json: {
            detail:
              "검색 색인이 없습니다 — make index EMBEDDING=voyage-4을 먼저 실행하세요",
          },
        }),
  );
  await page.goto("/claims");
  const box = page.getByRole("searchbox", { name: "청구 기준 찾기" });
  await box.fill("치석제거 다음 달로 넘어가면 왜 깎여?");
  await box.press("Enter");
  const found = page.getByRole("region", { name: "찾은 조항" });
  await expect(found).toContainText(
    "치주 분야 청구 기준 › 치석제거 뒤 치주치료 연결",
  );
  await expect(found).toContainText("90일 이내");
  await expect(bodyRows(page)).toHaveCount(3); // 목록은 그대로
  await found.getByRole("button", { name: "닫기" }).click();
  await expect(found).toHaveCount(0);
  status = 503;
  await box.press("Enter");
  await expect(page.getByText("검색 색인이 없습니다")).toBeVisible();
});

const APPEAL = {
  sentences: [
    {
      text: "조정 사유는 '치주치료 산정 기준 소견 미확인'입니다.",
      basis: "adjustment",
      quote: "치주치료 산정 기준 소견 미확인",
      assumption: null,
    },
    {
      text: "청구 기준은 치주낭 깊이가 4mm 이상인 부위가 확인될 때 산정한다고 정합니다.",
      basis: "standard",
      quote: "치주낭 깊이가 4mm 이상인 부위가 확인될 때",
      assumption: null,
    },
    {
      text: "위 기록에 따라 재심사를 요청합니다.",
      basis: "assumption",
      quote: null,
      assumption: 1,
    },
  ],
  assumptions: ["재심사를 요청한다는 결론은 AI가 쓴 문장입니다"],
  valid: true,
  reviewed: null as { by: string; at: string } | null,
  submitted: null as { by: string; at: string } | null,
  at: "2026-11-13T10:00:00",
};
const CONTESTED = {
  ...REQ,
  judgment: { value: "contestable", by: "데스크", at: "2026-11-13 10:00:00" },
};

test("재심 사유서 — 초안 쓰기, 꼬리표·추측, 검토 확인 뒤 제출 결정", async ({
  page,
}) => {
  let appeal: typeof APPEAL | null = null;
  await fakeClaims(page, () => sheet([{ ...CONTESTED, appeal }]));
  await page.route("**/api/claims/12/appeal", (r) => {
    appeal = { ...APPEAL };
    return r.fulfill({ json: appeal });
  });
  await page.route("**/api/claims/12/appeal/review", (r) => {
    appeal = {
      ...APPEAL,
      reviewed: { by: "데스크", at: "2026-11-13 10:01:00" },
    };
    return r.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/claims/12/appeal/submit", (r) => {
    appeal = {
      ...appeal!,
      submitted: { by: "데스크", at: "2026-11-13 10:02:00" },
    };
    return r.fulfill({ json: { ok: true } });
  });
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "이두리" }).click();
  const draft = page.getByRole("region", { name: "재심 사유서" });
  await draft.getByRole("button", { name: "초안 쓰기" }).click();
  await expect(draft).toContainText("재심사를 요청합니다.");
  await expect(draft).toContainText("조정 내역 인용");
  await expect(draft).toContainText("기준 절 인용");
  await expect(draft).toContainText("추측 ①");
  await expect(draft).toContainText(
    "AI가 추측한 부분 — 여기만 확인하면 됩니다",
  );
  // 사유서 본문 — 번호·꼬리표 없이 복사될 글 그대로 한 문단, 복사 버튼은 본문 상자에 하나뿐
  const body = draft.getByRole("region", { name: "사유서 본문" });
  await expect(body).toContainText(
    "조정 사유는 '치주치료 산정 기준 소견 미확인'입니다. 청구 기준은",
  );
  await expect(body).not.toContainText("기준 절 인용");
  await expect(body).toContainText("[복사]하면 이 글이 그대로 복사됩니다");
  await expect(draft).toContainText("문장별 근거 (확인용)");
  await expect(draft.getByRole("button", { name: "복사" })).toHaveCount(1);
  await expect(draft.getByRole("button", { name: "복사" })).toBeDisabled();
  await expect(draft.getByRole("button", { name: "제출 결정" })).toBeDisabled();
  // 체크는 서버에 저장한 뒤 다시 조회한 값으로 그린다 — check()가 아니라 눌러 놓고 상태를 기다린다
  const box = draft.getByRole("checkbox", { name: "검토했고 정확함" });
  await box.click();
  await expect(box).toBeChecked();
  await expect(draft.getByRole("button", { name: "제출 결정" })).toBeEnabled();
  await expect(draft.getByRole("button", { name: "복사" })).toBeEnabled();
  await draft.getByRole("button", { name: "제출 결정" }).click();
  await expect(draft).toContainText("제출 결정 · 데스크");
  await expect(draft.getByRole("button", { name: "다시 쓰기" })).toHaveCount(0);
  const detail = page.getByRole("region", { name: "이두리 청구 상세" });
  await expect(
    detail.getByRole("button", { name: "삭감 수용" }),
  ).toBeDisabled();
});

test("재심 사유서 — 실패 안내, 재심 신청이 아니면 숨김", async ({ page }) => {
  let rows: unknown[] = [
    {
      ...CONTESTED,
      appeal: { ...APPEAL, valid: false, sentences: null, assumptions: null },
    },
  ];
  await fakeClaims(page, () => sheet(rows as (typeof REQ)[]));
  await page.goto("/claims");
  await bodyRows(page).filter({ hasText: "이두리" }).click();
  const draft = page.getByRole("region", { name: "재심 사유서" });
  await expect(draft).toContainText("초안을 만들지 못했습니다");
  await expect(draft.getByRole("button", { name: "다시 쓰기" })).toBeVisible();
  await expect(draft.getByRole("button", { name: "제출 결정" })).toHaveCount(0);
  rows = [
    {
      ...REQ,
      judgment: { value: "justified", by: "데스크", at: "x" },
      appeal: APPEAL,
    },
  ];
  await page.reload();
  await bodyRows(page).filter({ hasText: "이두리" }).click();
  await expect(page.getByRole("region", { name: "재심 사유서" })).toHaveCount(
    0,
  );
});

test("청구 전 점검 — 경고 칩 기본, 과거 이력 문구", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims?tab=pre");
  await expect(page.getByRole("tab", { name: "청구 전 점검" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const rows = table(page).locator("tbody > tr");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("허용 상병 밖 — 이 시술은 매복치");
  await expect(rows.first()).toContainText(
    "지난 조정에서 이 조합 2건, 22,000원 못 받음",
  );
  await page.getByRole("button", { name: "청구 전 2줄" }).click();
  await expect(rows).toHaveCount(2);
  await expect(page.getByRole("link", { name: "한가람" })).toHaveAttribute(
    "href",
    "/emr?chart=C-00022",
  );
  await expect(page.getByText("D05", { exact: true })).toHaveCount(0);
});

test("청구 전 AI 칩 — 점검 결과가 있을 때만", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims?tab=pre");
  await expect(page.getByRole("button", { name: /\(AI\)/ })).toHaveCount(0);
  await page.unroute("**/api/claims/pre");
  await page.route("**/api/claims/pre", (r) => r.fulfill({ json: PRE_AI }));
  await page.goto("/claims?tab=pre");
  await page
    .getByRole("button", { name: "임상 요건·차트 기반 누락(AI) 2" })
    .click();
  await expect(page).toHaveURL(/\/claims\?tab=pre&chip=ai$/);
  const rows = table(page).locator("tbody > tr");
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toContainText("차트에 요건 근거 못 찾음(AI)");
  await expect(rows.first()).toContainText(STD_QUOTE);
  await expect(rows.nth(1)).toContainText("차트에 있는데 청구에 없음(AI)");
  await expect(rows.nth(1)).toContainText(`아말감/GI 12번: “${OMIT_QUOTE}”`);
  await expect(rows.nth(1)).not.toContainText("요건 근거");
  await expect(page.getByText("AI 점검: 11/11 09:30 기준")).toBeVisible();
});

test("AI 점검 갱신 버튼 — 작업이 끝나면 다시 조회", async ({ page }) => {
  let pre: unknown = PRE_AI;
  await fakeClaims(
    page,
    () => sheet(ALL),
    () => pre,
  );
  let posted = false;
  await page.route("**/api/jobs/claim-check", async (r) => {
    posted = true;
    pre = { ...PRE_AI, ai_pending: { pre: 0, adjust: 0 } };
    await r.fulfill({ json: { job_id: 5 } });
  });
  await page.route("**/api/jobs/5", (r) =>
    r.fulfill({ json: { id: 5, status: "done", error: null } }),
  );
  await page.goto("/claims?tab=pre");
  // 버튼 한 번으로 두 탭이 같이 갱신된다 — 탭별 수를 나눠 적는다
  await page
    .getByRole("button", {
      name: "AI 점검 갱신하기 (청구 전 3줄 · 삭감 대응 2건)",
    })
    .click();
  await expect(
    page.getByRole("button", { name: "AI 점검 최신" }),
  ).toBeDisabled();
  expect(posted).toBe(true);
});

const AI_Q = "38번 수평 매복, 골삭제 후 분할 발치함";

test("조정 건 AI 추천 — 사실 문장과 차트 인용", async ({ page }) => {
  const met = {
    ...REQ,
    suggestion: "contestable",
    suggest_by: "ai",
    ai_evidence: [{ source: "chart", quote: AI_Q }],
  };
  const miss = {
    ...REQ,
    claim_id: 16,
    name: "오다솜",
    suggestion: "justified",
    suggest_by: "ai",
    ai_evidence: [{ source: "standard", quote: STD_QUOTE }],
  };
  await fakeClaims(page, () => sheet([DOC, met, miss]));
  await page.goto("/claims");
  const metRow = bodyRows(page).filter({ hasText: "이두리" });
  await expect(metRow).toContainText("AI가 차트에서 근거 찾음 → 재심 신청");
  await expect(bodyRows(page).filter({ hasText: "오다솜" })).toContainText(
    "AI가 차트에서 근거 못 찾음 → 삭감 수용",
  );
  await expect(bodyRows(page).filter({ hasText: "김하나" })).toContainText(
    "추천: 보완 후 재청구",
  );
  await metRow.click();
  const detail = page.getByRole("region", { name: "이두리 청구 상세" });
  await expect(detail).toContainText(`“${AI_Q}”`);
});

test("AI 점검 갱신 버튼 — 한쪽이 0이면 그쪽은 빼고", async ({ page }) => {
  await fakeClaims(
    page,
    () => sheet(ALL),
    () => ({ ...PRE_AI, ai_pending: { pre: 0, adjust: 3 } }),
  );
  await page.goto("/claims?tab=pre");
  await expect(
    page.getByRole("button", { name: "AI 점검 갱신하기 (삭감 대응 3건)" }),
  ).toBeVisible();
});

test("점검 결과가 없을 때 AI 칩 주소는 경고 칩으로", async ({ page }) => {
  await fakeClaims(page, () => sheet(ALL));
  await page.goto("/claims?tab=pre&chip=ai");
  await expect(
    page.getByRole("button", { name: "상병 확인 경고(규칙) 1" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(table(page).locator("tbody > tr")).toHaveCount(1);
});
