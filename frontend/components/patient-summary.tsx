import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  type Appt,
  mdw,
  type PatientRecord,
  type PatientSummary as Summary,
  STATUS_LABEL,
  won,
} from "@/lib/emr-api";

/** 환자 요약 카드 — 예약표 패널. 준비표 체어 보기에서 다시 쓴다(전환 전 EMR·일별 예약표 설계안).
 * 카드 하나를 얇은 구분선으로 나누고 상자 안에 상자를 넣지 않는다 */
export function PatientSummary({
  patient,
  summary,
  appt,
  date,
}: {
  patient: PatientRecord["patient"];
  summary: Summary;
  appt: Appt | null;
  date: string; // appt의 날짜
}) {
  const upcoming = summary.upcoming
    .filter((u) => u.id !== appt?.id)
    .slice(0, 3);
  const flags = summary.alerts.length > 0 || summary.balance > 0;
  // 이 예약이 이행하는 항목을 뺀 열린 치료계획 항목 — 다음 회차를 잡을 때 본다
  const remaining = summary.open_items.filter(
    (i) => i.id !== appt?.plan_item_id,
  );
  const remainingText = remaining.length
    ? remaining
        .slice(0, 2)
        .map((i) => `${i.procedure}${i.tooth !== "00" ? ` #${i.tooth}` : ""}`)
        .join(", ") +
      (remaining.length > 2 ? ` 외 ${remaining.length - 2}건` : "")
    : "없음";

  return (
    <Card aria-label="환자 요약" className="gap-0 p-5 text-sm shadow-card">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h2 className="text-lg font-bold">{patient.name}</h2>
        <span className="text-muted-foreground">
          {patient.chart_no} · {patient.age}세 {patient.sex}
        </span>
      </div>

      {appt && (
        <Section>
          <p className="text-base font-bold">
            {mdw(date)} {appt.time} · {appt.duration}분
          </p>
          <p>
            {appt.procedure}
            {appt.tooth ? ` #${appt.tooth}` : ""} · {appt.staff_role}{" "}
            {appt.staff_name}
          </p>
          {appt.plan_step && (
            <p className="text-muted-foreground">
              치료계획 {appt.plan_step.total}개 중 {appt.plan_step.seq}번째
            </p>
          )}
          {appt.plan_step && (
            <p className="text-muted-foreground">남은 항목 · {remainingText}</p>
          )}
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Badge variant="muted">{STATUS_LABEL[appt.status]}</Badge>
            {appt.unconfirmed && appt.confirm === "no_reply" && (
              <Badge variant="warn">예약 확인 문자 무응답</Badge>
            )}
            {appt.unconfirmed && appt.confirm === "no_message" && (
              <Badge variant="warn">예약 미확인 · 확인 문자 없음</Badge>
            )}
            {appt.new_patient && <Badge variant="info">신환</Badge>}
          </div>
        </Section>
      )}

      {appt && (
        <Section title="어떻게 잡혔나">
          <p>
            {appt.booked_via === "phone" ? "전화" : "데스크"} · {appt.booked_at}
          </p>
          {appt.call && (
            <>
              <p>통화 있음 · {appt.call.started_at}</p>
              <p className="flex items-center gap-2 text-muted-foreground">
                응급 여부 <Badge variant="outdated">분류 전</Badge>
              </p>
            </>
          )}
        </Section>
      )}

      <Section title="이 환자의 일정">
        <p>마지막 내원 · {summary.last_visit ?? "—"}</p>
        <p>
          앞으로 ·{" "}
          {upcoming.length
            ? upcoming
                .map((u) => `${mdw(u.date)} ${u.time} ${u.procedure}`)
                .join(", ")
            : "없음"}
        </p>
        <p>
          최근 1년 · 노쇼 {summary.noshow_1y} · 취소 {summary.cancelled_1y}
        </p>
      </Section>

      <Section title="연락">
        <p>
          {patient.phone}
          {patient.guardian &&
            ` · 보호자 ${patient.guardian.name}(${patient.guardian.chart_no})`}
        </p>
      </Section>

      {flags && (
        <Section>
          <div className="flex flex-wrap gap-1.5">
            {summary.alerts.map((a) => (
              <Badge key={a.code} variant="danger">
                주의 · {a.label}
              </Badge>
            ))}
            {summary.balance > 0 && (
              <Badge variant="warn">미수 {won(summary.balance)}</Badge>
            )}
          </div>
        </Section>
      )}

      <Button asChild className="mt-5 h-11 font-bold">
        <Link
          href={`/emr?chart=${encodeURIComponent(patient.chart_no)}&view=chart`}
        >
          전자차트 열기
        </Link>
      </Button>
    </Card>
  );
}

function Section({
  title,
  children,
}: {
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-4 flex flex-col gap-1.5 border-t pt-4">
      {title && (
        <h3 className="text-xs font-bold text-muted-foreground">{title}</h3>
      )}
      {children}
    </section>
  );
}
