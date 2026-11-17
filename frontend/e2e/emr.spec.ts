// 전환 전 EMR 따라 보기 — README "진료 전 확인"의 화면 이동 5회를 지킨다(따라 보기 단계 frontend/components/emr/tour.ts).
// 하루 수는 5회 × 기준일 예약 환자(설명 카드 계산), 실데이터 복사본 기준
import { expect, test } from "@playwright/test";

test("따라 보기 — 환자 한 명 화면 이동 5회, 하루는 × 오늘 예약 환자", async ({
  page,
}) => {
  await page.goto("/emr");
  await page
    .getByRole("button", { name: /따라 보기/ })
    .first()
    .click();
  for (let i = 0; i < 4; i++) {
    const done = page.getByRole("button", { name: "마치기" });
    if (await done.isVisible()) {
      await done.click();
      break;
    }
    await page.getByRole("button", { name: "다음" }).click();
  }
  const result = page.locator("dl").filter({ hasText: "데스크의 하루" });
  await expect(result).toContainText("화면 이동 5회");
  const n = Number(
    (await result.getByText(/^\d+명$/).innerText()).replace("명", ""),
  );
  await expect(result).toContainText(`${5 * n}회`);
});

// AX 예약(AI 비서가 승인해 잡은 예약, AI 비서 설계 §3.3) — 예약표 응답을 가짜로 준다
const AX_APPT = {
  id: "ax-4",
  time: "10:00",
  duration: 30,
  staff_id: 1,
  staff_name: "이원장",
  staff_role: "원장",
  type: "treatment",
  procedure: "스케일링",
  tooth: null,
  status: "scheduled",
  unconfirmed: false,
  confirm: "no_message",
  plan_item_id: null,
  chart_no: "C-00001",
  name: "김철수",
  new_patient: false,
  plan_step: null,
  booked_via: "phone",
  booked_at: "2026-11-30 09:10",
  source: "ax",
  ax: {
    decided_by: "데스크",
    decided_at: "2026-11-30 09:12:00",
    source_call_uid: "c-0319",
    conversation_id: 5,
  },
  call: {
    uid: "c-0319",
    started_at: "2026-11-30 09:00:00",
    pool: false,
    emergency: false,
  },
};

test("schedule: AX 예약 꼬리표와 예약 대화 기록 링크", async ({ page }) => {
  await page.route("**/api/emr/schedule*", (r) =>
    r.fulfill({
      json: {
        date: "2026-12-01",
        hours: { open: "09:00", close: "18:00", lunch: null, closed: false },
        staff: [{ id: 1, name: "이원장", role: "원장" }],
        appointments: [
          AX_APPT,
          {
            ...AX_APPT,
            id: 11885,
            time: "11:00",
            name: "박순자",
            source: "emr",
            ax: null,
          },
        ],
        range: { from: "2026-11-01", to: "2026-12-31" },
        counts: {
          total: 2,
          treatment: 2,
          hygiene: 0,
          consult: 0,
          unconfirmed: 0,
        },
      },
    }),
  );
  await page.route("**/api/emr/patients/C-00001", (r) =>
    r.fulfill({
      json: {
        patient: {
          chart_no: "C-00001",
          name: "김철수",
          age: 56,
          sex: "M",
          birth: "1970-01-01",
          phone: "010-0000-0000",
          registered: "2020-01-01",
          private_insurance: false,
          family: [],
          guardian: null,
        },
        summary: {
          balance: 0,
          alerts: [],
          last_visit: null,
          upcoming: [],
          noshow_1y: 0,
          cancelled_1y: 0,
          open_items: [],
        },
      },
    }),
  );
  await page.route("**/api/calls/patient/**", (r) =>
    r.fulfill({ json: { calls: [], total: 0 } }),
  );
  await page.goto("/schedule?date=2026-12-01&appt=ax-4");
  const grid = page.getByRole("button", { name: /김철수/ });
  await expect(grid.getByText("AX", { exact: true })).toBeVisible();
  await expect(
    page
      .getByRole("button", { name: /박순자/ })
      .getByText("AX", { exact: true }),
  ).toHaveCount(0);
  const card = page.getByLabel("환자 요약");
  await expect(card.getByText(/AI 비서로 잡음/)).toBeVisible();
  await expect(card.getByText(/승인 2026-11-30 09:12 · 데스크/)).toBeVisible();
  await expect(card.getByRole("link", { name: /통화 있음/ })).toHaveAttribute(
    "href",
    /call=c-0319/,
  );
  await expect(
    card.getByRole("link", { name: "예약 대화 기록 보기" }),
  ).toHaveAttribute("href", "/?c=5");
});
