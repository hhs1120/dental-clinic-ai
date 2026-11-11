"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { LedgerTable } from "@/components/finance/ledger-table";
import { ErrorBand } from "@/components/prep/prep-view";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { fetchRecon, type ReconSheet } from "@/lib/finance-api";

/** 2026-10 → 10월, 연도가 앞 버튼과 다르거나 첫 버튼이면 2026년 10월 — 2년 치라 같은 달 이름이 겹치지 않게 */
const monthLabel = (m: string, prev?: string) =>
  `${!prev || prev.slice(0, 4) !== m.slice(0, 4) ? `${m.slice(0, 4)}년 ` : ""}${Number(m.slice(5))}월`;

/** ⑧ 재무 대조 — 1단계는 일계표만(지출 증빙 탭은 2단계). 달·펼친 날은 URL에 둔다(/finance?month=…&day=…) */
export function FinanceView() {
  const params = useSearchParams();
  const router = useRouter();
  const month = params.get("month");
  const day = params.get("day");
  const [attempt, setAttempt] = useState(0);
  const [sheet, setSheet] = useState<ReconSheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchRecon(month, ac.signal).then(
      (data) => {
        setSheet(data);
        setError(null);
      },
      (e) => !ac.signal.aborted && setError(errorText(e)),
    );
    return () => ac.abort();
  }, [month, attempt]);

  const go = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    router.replace(`/finance${next.size ? `?${next}` : ""}`);
  };

  if (!sheet)
    return error ? (
      <ErrorBand text={error} />
    ) : (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  return (
    <div className="flex flex-col gap-3">
      {error && <ErrorBand text={error} />}
      <div className="flex flex-wrap items-center gap-1.5">
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="달">
          {sheet.months.map((m, k) => (
            <Button
              key={m}
              size="sm"
              variant={m === sheet.month ? "secondary" : "outline"}
              aria-pressed={m === sheet.month}
              onClick={() => go({ month: m, day: null })}
            >
              {monthLabel(m, sheet.months[k - 1])}
            </Button>
          ))}
        </div>
        <span className="flex-1" />
        <span className="text-sm text-muted-foreground">
          영업일 {sheet.chips.days}
        </span>
        <span className="text-sm text-muted-foreground">
          불일치 있는 날 {sheet.chips.mismatch_days}
        </span>
        <span className="text-sm font-bold">
          이 달 확정 안 된 건 {sheet.chips.open_items}
        </span>
        <span className="text-sm text-muted-foreground">
          전체 확정 안 된 건 {sheet.chips.open_all}
        </span>
      </div>
      <LedgerTable
        sheet={sheet}
        open={day}
        onOpen={(d) => go({ day: d })}
        onSaved={() => setAttempt((n) => n + 1)}
      />
    </div>
  );
}
