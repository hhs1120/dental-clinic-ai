"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import {
  type ExpenseChip,
  ExpenseTable,
} from "@/components/finance/expense-table";
import { LedgerTable } from "@/components/finance/ledger-table";
import { ErrorBand } from "@/components/prep/prep-view";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorText } from "@/lib/api";
import {
  fetchExpenses,
  fetchRecon,
  type ExpenseSheet,
  type ReconSheet,
} from "@/lib/finance-api";
import { won } from "@/lib/format";

const TABS = [
  { value: "ledger", label: "일계표" },
  { value: "expense", label: "지출 증빙" },
];

type Go = (patch: Record<string, string | null>) => void;

/** 2026-10 → 10월, 연도가 앞 버튼과 다르거나 첫 버튼이면 2026년 10월 — 2년 치라 같은 달 이름이 겹치지 않게 */
const monthLabel = (m: string, prev?: string) =>
  `${!prev || prev.slice(0, 4) !== m.slice(0, 4) ? `${m.slice(0, 4)}년 ` : ""}${Number(m.slice(5))}월`;

/** 달 응답 하나 — 달·다시 부르기(attempt)가 바뀌면 다시 받는다 */
function useSheet<T>(
  fetcher: (month: string | null, signal: AbortSignal) => Promise<T>,
  month: string | null,
  attempt: number,
) {
  const [sheet, setSheet] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetcher(month, ac.signal).then(
      (data) => {
        setSheet(data);
        setError(null);
      },
      (e) => !ac.signal.aborted && setError(errorText(e)),
    );
    return () => ac.abort();
  }, [fetcher, month, attempt]);
  return { sheet, error };
}

function Loading({ error }: { error: string | null }) {
  return error ? (
    <ErrorBand text={error} />
  ) : (
    <p className="py-10 text-center text-sm text-muted-foreground">
      불러오는 중…
    </p>
  );
}

function MonthButtons({
  months,
  month,
  onPick,
}: {
  months: string[];
  month: string | null;
  onPick: (m: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5" role="group" aria-label="달">
      {months.map((m, k) => (
        <Button
          key={m}
          size="sm"
          variant={m === month ? "secondary" : "outline"}
          aria-pressed={m === month}
          onClick={() => onPick(m)}
        >
          {monthLabel(m, months[k - 1])}
        </Button>
      ))}
    </div>
  );
}

/** 일계표 — 날짜별 장부 vs 금융과 불일치 건(설계 §1). 펼친 날은 URL day */
function LedgerTab({
  month,
  day,
  go,
}: {
  month: string | null;
  day: string | null;
  go: Go;
}) {
  const [attempt, setAttempt] = useState(0);
  const { sheet, error } = useSheet<ReconSheet>(fetchRecon, month, attempt);
  if (!sheet) return <Loading error={error} />;
  return (
    <div className="flex flex-col gap-3">
      {error && <ErrorBand text={error} />}
      <div className="flex flex-wrap items-center gap-1.5">
        <MonthButtons
          months={sheet.months}
          month={sheet.month}
          onPick={(m) => go({ month: m, day: null })}
        />
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

/** 칩 — 출금 전체(거름 없음)와 네 가지 거름(설계 §2) */
const expenseChips = (s: ExpenseSheet) => [
  {
    key: null,
    text: `출금 ${s.chips.outflows} · ${won(s.chips.amount)}`,
  },
  { key: "found", text: `증빙 찾음 ${s.chips.found}` },
  { key: "none", text: `증빙 못 찾음 ${s.chips.none}` },
  { key: "check", text: `과목 확인 필요 ${s.chips.check}` },
  { key: "open", text: `미확정 ${s.chips.open}` },
];

/** 지출 증빙 — 출금마다 증빙·계정 과목, 사람 확정(설계 §2). 거름 칩은 URL chip */
function ExpenseTab({
  month,
  chip,
  go,
}: {
  month: string | null;
  chip: string | null;
  go: Go;
}) {
  const [attempt, setAttempt] = useState(0);
  const { sheet, error } = useSheet<ExpenseSheet>(
    fetchExpenses,
    month,
    attempt,
  );
  if (!sheet) return <Loading error={error} />;
  const on = (["found", "none", "check", "open"] as string[]).includes(
    chip ?? "",
  )
    ? (chip as ExpenseChip)
    : null;
  return (
    <div className="flex flex-col gap-3">
      {error && <ErrorBand text={error} />}
      <MonthButtons
        months={sheet.months}
        month={sheet.month}
        onPick={(m) => go({ month: m, chip: null })}
      />
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="거르기">
        {expenseChips(sheet).map((c) => (
          <Button
            key={c.key ?? "all"}
            size="sm"
            variant={c.key === on ? "secondary" : "outline"}
            aria-pressed={c.key === on}
            onClick={() => go({ chip: c.key })}
          >
            {c.text}
          </Button>
        ))}
      </div>
      <ExpenseTable
        sheet={sheet}
        chip={on}
        onSaved={() => setAttempt((n) => n + 1)}
      />
    </div>
  );
}

/** ⑧ 재무 대조 — 탭 일계표 | 지출 증빙. 탭·달·펼친 날·칩은 URL에 둔다(/finance?tab=expense&month=…&chip=…) */
export function FinanceView() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "expense" ? "expense" : "ledger";
  const month = params.get("month");

  const go: Go = (patch) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    router.replace(`/finance${next.size ? `?${next}` : ""}`);
  };

  return (
    <Tabs
      value={tab}
      // 탭마다 달 목록이 달라 달·펼친 날·칩을 비운다
      onValueChange={(v) =>
        go({
          tab: v === "ledger" ? null : v,
          month: null,
          day: null,
          chip: null,
        })
      }
      className="gap-4"
    >
      <TabsList variant="line" className="w-full justify-start border-b">
        {TABS.map((t) => (
          <TabsTrigger key={t.value} value={t.value} className="flex-none px-4">
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="ledger">
        {tab === "ledger" && (
          <LedgerTab month={month} day={params.get("day")} go={go} />
        )}
      </TabsContent>
      <TabsContent value="expense">
        {tab === "expense" && (
          <ExpenseTab month={month} chip={params.get("chip")} go={go} />
        )}
      </TabsContent>
    </Tabs>
  );
}
