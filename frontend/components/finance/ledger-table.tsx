"use client";

import { Fragment } from "react";

import { ReconItemView } from "@/components/finance/recon-item";
import { Badge } from "@/components/ui/badge";
import type { ReconDay, ReconSheet, Sums } from "@/lib/finance-api";
import { won } from "@/lib/format";

const WEEKDAY = ["일", "월", "화", "수", "목", "금", "토"];

/** 2026-11-03 → 11/03 (화) */
const dayText = (d: string) =>
  `${d.slice(5, 7)}/${d.slice(8, 10)} (${WEEKDAY[new Date(`${d}T00:00:00`).getDay()]})`;

/** 차이 — 0이면 흐리게, 아니면 부호와 함께 */
function Diff({ n }: { n: number | null }) {
  if (n === null) return <span className="text-muted-foreground">—</span>;
  if (n === 0) return <span className="text-muted-foreground">0</span>;
  return (
    <span className="font-bold text-status-danger-fg">
      {n > 0 ? "+" : "−"}
      {won(Math.abs(n))}
    </span>
  );
}

const cells = (s: Sums) => [
  <td key="l" className="px-2 py-2 text-right whitespace-nowrap">
    {won(s.ledger)}
  </td>,
  <td key="f" className="px-2 py-2 text-right whitespace-nowrap">
    {won(s.fin)}
  </td>,
  <td key="d" className="px-2 py-2 text-right whitespace-nowrap">
    <Diff n={s.diff} />
  </td>,
];

/** 상태 — 정산 대기 / 차이 없음 / 확정 안 된 불일치 n건(합계 차이가 0이어도) / 모두 확정이면 확인함 */
function Status({ day }: { day: ReconDay }) {
  if (day.status === "pending") return <Badge variant="muted">정산 대기</Badge>;
  if (day.items.length === 0)
    return <span className="text-muted-foreground">차이 없음</span>;
  const open = day.items.filter((i) => !i.confirmation).length;
  return open ? (
    <Badge variant="danger">불일치 {open}건</Badge>
  ) : (
    <Badge variant="ok">확인함</Badge>
  );
}

/** 일계표 — 날짜당 한 줄, 결제수단별 장부 vs 금융(설계 §1). 불일치 날을 누르면 그 날의 건이 아래에 펼쳐진다 */
export function LedgerTable({
  sheet,
  open,
  onOpen,
  onSaved,
}: {
  sheet: ReconSheet;
  open: string | null;
  onOpen: (day: string | null) => void;
  onSaved: () => void;
}) {
  return (
    <div className="scroll-shadow-x overflow-x-auto rounded-xl border">
      <table className="w-full min-w-4xl text-sm tabular-nums">
        <thead className="bg-muted whitespace-nowrap text-muted-foreground">
          <tr>
            <th rowSpan={2} className="px-3 py-2 text-left">
              날짜
            </th>
            <th colSpan={3} className="border-l px-2 pt-2 text-center">
              카드
            </th>
            <th colSpan={3} className="border-l px-2 pt-2 text-center">
              현금
            </th>
            <th colSpan={3} className="border-l px-2 pt-2 text-center">
              이체
            </th>
            <th rowSpan={2} className="border-l px-3 py-2 text-left">
              상태
            </th>
          </tr>
          <tr className="text-right">
            {["승인", "입금", "입금"].map((fin, k) => (
              <Fragment key={k}>
                <th className="border-l px-2 pb-2 font-normal">장부</th>
                <th className="px-2 pb-2 font-normal">{fin}</th>
                <th className="px-2 pb-2 font-normal">차이</th>
              </Fragment>
            ))}
          </tr>
        </thead>
        <tbody>
          {sheet.days.map((d) => {
            const clickable = d.status === "ok" && d.items.length > 0;
            const toggle = () =>
              clickable && onOpen(open === d.date ? null : d.date);
            return (
              <Fragment key={d.date}>
                <tr
                  data-day={d.date}
                  tabIndex={clickable ? 0 : undefined}
                  aria-expanded={clickable ? open === d.date : undefined}
                  className={`border-t ${clickable ? "cursor-pointer hover:bg-muted/50" : ""}`}
                  onClick={toggle}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggle();
                    }
                  }}
                >
                  <td className="px-3 py-2 whitespace-nowrap">
                    {dayText(d.date)}
                  </td>
                  {cells(d.card)}
                  <td className="border-l px-2 py-2 text-right whitespace-nowrap">
                    {won(d.cash.ledger)}
                  </td>
                  <td className="px-2 py-2 text-right whitespace-nowrap">
                    {d.cash.pending ? (
                      // 그날 현금이 든 입금 기간에 정산 대기 날이 섞여 아직 맞춰 보지 않음
                      <span className="text-muted-foreground">대조 전</span>
                    ) : d.cash.fin === null || !d.cash.deposit ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      <>
                        {won(d.cash.fin)}
                        <div className="text-xs text-muted-foreground">
                          {d.cash.deposit.days}일 치 합산
                        </div>
                      </>
                    )}
                  </td>
                  <td className="px-2 py-2 text-right whitespace-nowrap">
                    <Diff n={d.cash.diff} />
                  </td>
                  {cells(d.transfer)}
                  <td className="border-l px-3 py-2 whitespace-nowrap">
                    <Status day={d} />
                  </td>
                </tr>
                {clickable && open === d.date && (
                  <tr>
                    <td colSpan={11} className="bg-muted/30 px-3 py-3">
                      <div className="flex flex-col gap-3">
                        {d.items.map((i) => (
                          <ReconItemView
                            key={i.key}
                            item={i}
                            onSaved={onSaved}
                          />
                        ))}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {sheet.days.length === 0 && (
            <tr>
              <td
                colSpan={11}
                className="px-3 py-10 text-center text-muted-foreground"
              >
                장부가 없는 달입니다
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
