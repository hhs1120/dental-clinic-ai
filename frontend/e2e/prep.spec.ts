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
  last_visit: "2026-11-20",
  balance: 0,
  alerts: [],
  scaling_unused: false,
  family_recall: [],
  open_plan: [],
  generated: true,
  suggestion_valid: true,
  stale: false,
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
