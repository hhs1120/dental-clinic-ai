// ④ 진료 준비표 — 준비표·체어 보기(설계 2026-11-10 준비표 §5). 제안은 과금이라 /prep을 page.route 가짜 응답으로 준다
import { expect, type Page, test } from "@playwright/test";

const DAYS = [
  "2026-11-24",
  "2026-11-25",
  "2026-11-26",
  "2026-11-27",
  "2026-11-28",
  "2026-11-29",
  "2026-11-30",
  "2026-12-01",
].map((date) => ({ date, count: date === "2026-12-01" ? 3 : 0 }));

const base = {
  duration_min: 30,
  status: "scheduled",
  confirmed: true,
  confirm: "replied",
  last_visit: "2026-11-20",
  balance: 0,
  alerts: [],
  scaling_unused: false,
  family_recall: [],
  open_plan: [],
  generated: true,
  suggestion_valid: true,
  refresh: false,
  stale: false,
  changed: [],
  made_at: "2026-12-01T05:30:00",
};

const ROWS = [
  {
    ...base,
    appointment_id: 101,
    patient_id: 1,
    time: "10:00",
    chair_no: 1,
    type: "treatment",
    procedure: "단순발치",
    procedure_code: "U0011",
    staff: "강태현",
    confirmed: false,
    confirm: "no_reply",
    patient: {
      name: "김철수",
      chart_no: "C-00001",
      age: 56,
      sex: "M",
      new_patient: false,
    },
    balance: 30000,
    alerts: [
      {
        code: "ANTICOAG",
        label: "항응고제 복용",
        related: true,
        action: "복용 여부 확인, 지혈 준비",
      },
      {
        code: "GAG",
        label: "구역반사",
        related: false,
        action: "상체를 세우고 구역반사 대비",
      },
    ],
    scaling_unused: true,
    open_plan: [{ procedure: "크라운", tooth: "46", est_fee: 450000 }],
    chips: [
      "appointment",
      "insurance",
      "balance",
      "alert",
      "open_plan",
      "unconfirmed",
    ],
    suggestion: {
      action: "suggest",
      text: "항응고제 복용 확인, 미수 3만 원 안내",
      used_facts: [
        {
          key: "alert.ANTICOAG",
          value: "항응고제 복용 — 복용 여부 확인, 지혈 준비",
        },
        { key: "balance", value: "30000" },
      ],
    },
  },
  {
    ...base,
    appointment_id: 102,
    patient_id: 2,
    time: "11:00",
    chair_no: 2,
    type: "hygiene",
    procedure: "스케일링",
    procedure_code: "U0001",
    staff: "임재윤",
    patient: {
      name: "박영희",
      chart_no: "C-00002",
      age: 54,
      sex: "F",
      new_patient: false,
    },
    alerts: [
      { code: "HTN", label: "고혈압", related: false, action: "혈압 확인" },
    ],
    chips: ["appointment"],
    suggestion: { action: "none", text: "", used_facts: [] },
  },
  {
    ...base,
    appointment_id: 103,
    patient_id: 4,
    time: "14:00",
    chair_no: null,
    type: "consult",
    procedure: "상담",
    procedure_code: "U0019",
    staff: "강태현",
    confirmed: false,
    confirm: "no_message",
    last_visit: null,
    patient: {
      name: "정다은",
      chart_no: "C-00004",
      age: 16,
      sex: "F",
      new_patient: true,
    },
    chips: ["appointment", "unconfirmed"],
    suggestion: null,
    suggestion_valid: false,
    refresh: true,
  },
];

const LATER = {
  ...ROWS[1],
  appointment_id: 104,
  patient_id: 3,
  time: "11:30",
  chair_no: 1,
  procedure: "구강검진",
  alerts: [],
  patient: {
    name: "김민수",
    chart_no: "C-00003",
    age: 26,
    sex: "M",
    new_patient: false,
  },
};

async function fakePrep(
  page: Page,
  rowsOf: (date: string | null) => unknown[] = (date) =>
    date === "2026-11-30" ? [] : ROWS,
) {
  await page.route("**/api/prep**", (r) => {
    const date = new URL(r.request().url()).searchParams.get("date");
    if (date === "2026-11-01")
      return r.fulfill({
        status: 400,
        json: { detail: "준비표는 기준일 포함 최근 8일만" },
      });
    const rows = rowsOf(date);
    return r.fulfill({
      json: {
        date: date ?? "2026-12-01",
        generated_at: "2026-12-01T05:30:00",
        days: DAYS,
        rows,
      },
    });
  });
}

test("준비표 — 칩을 누르면 그 칩이 켜진 환자만 남는다", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01");
  const rows = page.getByRole("table").getByRole("row");
  await expect(rows).toHaveCount(4); // 머리줄 + 3명
  await page.getByRole("button", { name: /^미수 잔액 1$/ }).click();
  await expect(rows).toHaveCount(2);
  await expect(page.getByRole("table")).toContainText("김철수");
});

test("행을 펼치면 근거와 후검증이 보인다", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01");
  await page.getByRole("row", { name: /김철수/ }).click();
  // 근거는 내부 키가 아니라 사람이 읽는 이름으로
  await expect(page.getByText("주의: 항응고제 복용")).toBeVisible();
  await expect(page.getByText("alert.ANTICOAG")).toHaveCount(0);
  await expect(page.getByText("후검증 통과")).toBeVisible();
  await expect(page.getByText(/^신환$/)).toBeVisible(); // 정다은 행의 딱지
  // 내원 확인은 누가 무엇을 했는지 사실로
  await expect(page.getByText("문자에 답 없음 — 확인 필요")).toBeVisible();
  await page.getByRole("row", { name: /정다은/ }).click();
  await expect(
    page.getByText("문자 발송 시각이 지난 뒤 잡힌 예약 — 확인 필요"),
  ).toBeVisible();
});

test("지난 날의 내원 확인은 비운다", async ({ page }) => {
  await fakePrep(page, (date) =>
    date === "2026-11-29" ? [{ ...ROWS[1], confirm: null }] : ROWS,
  );
  await page.goto("/prep?date=2026-11-29");
  await page.getByRole("row", { name: /박영희/ }).click();
  const fact = page.getByText("내원 확인", { exact: true }).locator("..");
  await expect(fact).toContainText("—");
});

test("그대로 진행·제안 없음 표시", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01");
  await expect(page.getByRole("row", { name: /박영희/ })).toContainText(
    "그대로 진행",
  );
  await expect(page.getByRole("row", { name: /정다은/ })).toContainText(
    "제안 없음 — 후검증 실패",
  );
});

test("예약 없는 날짜는 예약 없음", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-11-30");
  await expect(page.getByText("이 날은 예약이 없습니다")).toBeVisible();
});

test("체어 보기 — 체어를 바꾸면 그 체어 환자만", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01&view=chair&chair=2");
  await expect(page.getByRole("heading", { name: "박영희" })).toBeVisible();
  await page.getByRole("button", { name: /^1번 1$/ }).click();
  await expect(page.getByRole("heading", { name: "김철수" })).toBeVisible();
});

test("체어 보기 — 주의사항이 큰 칸에 행동과 함께", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01&view=chair&chair=1");
  const card = page.getByRole("region", { name: "지금 환자" });
  await expect(card).toContainText("항응고제 복용");
  await expect(card).toContainText("복용 여부 확인, 지혈 준비");
  // 오늘 시술과 무관한 주의사항은 큰 칸이 아니라 아래 작은 줄에
  await expect(card).toContainText(
    "그 밖의 주의사항(오늘 시술과 무관): 구역반사",
  );
});

test("체어 보기 — 지금 전에는 첫 환자가 다음", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-12-01T08:00:00"));
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01&view=chair&chair=1");
  await expect(page.getByRole("button", { name: /10:00/ })).toContainText(
    "다음",
  );
});

test("날짜 탭 밖 날짜로 들어오면 기준일로 돌아간다", async ({ page }) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-11-01");
  await expect(page.getByRole("table")).toContainText("김철수");
  await expect(page).not.toHaveURL(/date=2026-11-01/);
});

test("체어 보기 — 무관한 주의사항만 있으면 관련 주의사항 없음과 함께 작게", async ({
  page,
}) => {
  await fakePrep(page);
  await page.goto("/prep?date=2026-12-01&view=chair&chair=2");
  const card = page.getByRole("region", { name: "지금 환자" });
  await expect(card).toContainText("오늘 시술 관련 주의사항 없음");
  await expect(card).toContainText("고혈압");
});

test("체어 보기 — 지금 환자가 있으면 바로 뒤 환자에 다음", async ({ page }) => {
  await page.clock.setFixedTime(new Date("2026-12-01T10:10:00"));
  await fakePrep(page, () => [...ROWS, LATER]);
  await page.goto("/prep?date=2026-12-01&view=chair&chair=1");
  await expect(page.getByRole("button", { name: /10:00/ })).toContainText(
    "지금",
  );
  await expect(page.getByRole("button", { name: /11:30/ })).toContainText(
    "다음",
  );
});

test("갱신 전 — 바뀐 사실과 갱신할 인원", async ({ page }) => {
  const stale = {
    ...ROWS[0],
    refresh: true,
    stale: true,
    changed: [{ key: "balance", before: "50000", after: "30000" }],
  };
  await fakePrep(page, () => [stale, ROWS[1], ROWS[2]]);
  await page.goto("/prep?date=2026-12-01");
  await expect(page.getByRole("row", { name: /김철수/ })).toContainText(
    "제안 만든 뒤 바뀜: 미수 잔액 50,000원 → 30,000원",
  );
  await expect(page.getByText(/AI 제안: 12\/1\(화\) 05:30 기준/)).toContainText(
    "사실이 바뀐 환자 1명",
  );
  // 사실이 바뀐 김철수 + 후검증 실패 정다은
  await expect(
    page.getByRole("button", { name: "AI 제안 갱신하기 (2명)" }),
  ).toBeEnabled();
});

test("모두 최신이면 갱신 버튼이 꺼진다", async ({ page }) => {
  await fakePrep(page, () => [ROWS[0], ROWS[1]]);
  await page.goto("/prep?date=2026-12-01");
  await expect(
    page.getByRole("button", { name: "AI 제안 최신" }),
  ).toBeDisabled();
});

test("갱신 뒤 — 다시 만든 행에만 방금 갱신과 이전 제안", async ({ page }) => {
  let refreshed = false;
  // 갱신 대상은 후검증에 실패했던 정다은뿐 — 다시 만들어 만든 시각이 바뀌고, 김철수는 건너뛰어 그대로
  const renewed = {
    ...ROWS[2],
    suggestion: { action: "none", text: "", used_facts: [] },
    suggestion_valid: true,
    refresh: false,
    made_at: "2026-12-01T09:00:00",
  };
  await fakePrep(page, () => (refreshed ? [ROWS[0], ROWS[1], renewed] : ROWS));
  await page.route("**/api/jobs/**", (r) => {
    if (r.request().method() === "POST") {
      refreshed = true;
      return r.fulfill({ json: { job_id: 1 } });
    }
    return r.fulfill({ json: { id: 1, status: "done", error: null } });
  });
  await page.goto("/prep?date=2026-12-01");
  await page.getByRole("button", { name: "AI 제안 갱신하기 (1명)" }).click();
  const row = page.getByRole("row", { name: /정다은/ });
  await expect(row).toContainText("방금 갱신");
  await expect(row).toContainText("이전: 제안 없음 — 후검증 실패");
  await expect(page.getByText("1명 갱신함")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "AI 제안 최신" }),
  ).toBeDisabled();
  await expect(page.getByRole("row", { name: /김철수/ })).not.toContainText(
    "방금 갱신",
  );
  // 체어 보기도 같은 제안 칸
  await page.getByRole("tab", { name: "체어 보기" }).click();
  await page.getByRole("button", { name: /^상담실/ }).click();
  await expect(page.getByRole("region", { name: "지금 환자" })).toContainText(
    "방금 갱신",
  );
});

test("인쇄 — A4 너비에서 표가 가로로 넘치지 않는다", async ({ page }) => {
  await fakePrep(page);
  // A4 세로 인쇄 영역(여백 뺀 약 190mm ≈ 718px)
  await page.setViewportSize({ width: 718, height: 1000 });
  await page.goto("/prep?date=2026-12-01");
  await expect(page.getByRole("table")).toContainText("김철수");
  await page.emulateMedia({ media: "print" });
  const overflow = await page.evaluate(() => {
    const table = document.querySelector("table")!;
    const box = table.parentElement!;
    return {
      page:
        document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
      box: box.scrollWidth - box.clientWidth,
      right:
        table.getBoundingClientRect().right -
        document.documentElement.clientWidth,
    };
  });
  expect(overflow.page).toBeLessThanOrEqual(0);
  expect(overflow.box).toBeLessThanOrEqual(0);
  expect(overflow.right).toBeLessThanOrEqual(0);
});
