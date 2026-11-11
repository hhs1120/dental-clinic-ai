"use client";

import { useState } from "react";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { errorText } from "@/lib/api";
import {
  type Cause,
  CAUSE_LABEL,
  CAUSES,
  LEDGER_CAUSES,
  METHOD_LABEL,
  postConfirm,
  type ReconItem,
} from "@/lib/finance-api";
import { won } from "@/lib/format";
import { readPart } from "@/lib/part";

/** 2026-11-03 10:00:00 → 11/03 10:00 (날짜만이면 11/03) */
const at = (s: string) =>
  `${s.slice(5, 7)}/${s.slice(8, 10)}${s.length > 10 ? ` ${s.slice(11, 16)}` : ""}`;

/** 불일치 건 하나 — 관련 장부·금융 행, 규칙 추정 원인, 사람 확정(맞음 / 아님 → 원인 고르기) */
export function ReconItemView({
  item,
  onSaved,
}: {
  item: ReconItem;
  onSaved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = (cause: Cause) => {
    setBusy(true);
    setError(null);
    postConfirm(item.key, cause, readPart()).then(
      () => {
        setBusy(false);
        onSaved();
      },
      (e) => {
        setBusy(false);
        setError(errorText(e));
      },
    );
  };
  const c = item.confirmation;
  return (
    <section
      aria-label={`불일치 건 ${at(item.date)}`}
      className="flex flex-col gap-3 rounded-md border bg-card p-3"
    >
      <div className="grid gap-3 md:grid-cols-2">
        <div className="flex flex-col gap-1">
          <h3 className="font-bold">장부</h3>
          {item.ledger_rows.length === 0 ? (
            <p className="text-muted-foreground">장부에 해당 수납 없음</p>
          ) : (
            <ul className="flex flex-col gap-0.5 tabular-nums">
              {item.ledger_rows.map((r) => (
                <li key={r.id}>
                  {at(r.paid_at)} · {r.name} ·{" "}
                  {METHOD_LABEL[r.method] ?? r.method} 받은 돈{" "}
                  {won(r.amount_paid)}
                  <span className="text-muted-foreground">
                    {" "}
                    (낼 돈 {won(r.amount_due)}
                    {r.approval_no ? ` · 승인 ${r.approval_no}` : ""})
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <h3 className="font-bold">카드·통장</h3>
          {item.fin_rows.length === 0 ? (
            <p className="text-muted-foreground">카드·통장에 해당 내역 없음</p>
          ) : (
            <ul className="flex flex-col gap-0.5 tabular-nums">
              {item.fin_rows.map((r, k) => (
                <li key={k}>
                  {at(r.at)} · {r.label} · {won(r.amount)}
                  {r.detail && (
                    <div className="text-muted-foreground">
                      매출 {won(r.detail.sales_total)} (
                      {at(r.detail.sales_date)}) · 수수료 {won(r.detail.fee)}{" "}
                      (기대 {won(r.detail.expected_fee)}) · 예상 입금{" "}
                      {at(r.detail.expected_on)}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span>
          규칙 추정: <b>{CAUSE_LABEL[item.rule_cause]}</b>
        </span>
        {c && (
          <Badge variant="ok">
            {CAUSE_LABEL[c.cause]} · {c.by} {at(c.at)}
          </Badge>
        )}
        <span className="flex-1" />
        <Button
          size="sm"
          variant={c?.cause === item.rule_cause ? "secondary" : "outline"}
          disabled={busy}
          onClick={() => save(item.rule_cause)}
        >
          맞음
        </Button>
        <span className="text-sm text-muted-foreground">아님 →</span>
        <Select
          value={c && c.cause !== item.rule_cause ? c.cause : ""}
          onValueChange={(v) => save(v as Cause)}
          disabled={busy}
        >
          <SelectTrigger aria-label="원인 고르기" className="w-40 bg-card">
            <SelectValue placeholder="원인 고르기" />
          </SelectTrigger>
          <SelectContent>
            {CAUSES.map((k) => (
              <SelectItem key={k} value={k}>
                {CAUSE_LABEL[k]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {c && LEDGER_CAUSES.includes(c.cause) && (
        <p className="text-sm text-muted-foreground">
          장부 수정은 전자차트에서 합니다
        </p>
      )}
      {error && <ErrorBand text={error} />}
    </section>
  );
}
