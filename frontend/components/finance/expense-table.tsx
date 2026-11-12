"use client";

import { Fragment, useEffect, useState } from "react";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { errorText } from "@/lib/api";
import {
  ACCOUNT_FROM_LABEL,
  confirmExpense,
  DOC_KIND_LABEL,
  type ExpenseDoc,
  type ExpenseRow,
  type ExpenseSheet,
  fetchCandidates,
} from "@/lib/finance-api";
import { won } from "@/lib/format";
import { readPart } from "@/lib/part";

export type ExpenseChip = "found" | "none" | "check" | "open";

/** 2026-11-03(…) → 11/03 */
const md = (s: string) => `${s.slice(5, 7)}/${s.slice(8, 10)}`;

const keep: Record<ExpenseChip, (r: ExpenseRow) => boolean> = {
  found: (r) => r.docs.length > 0,
  none: (r) => r.docs.length === 0 && !r.confirmed,
  check: (r) => r.check,
  open: (r) => !r.confirmed,
};

/** 찾은 증빙 — 한 장이면 종류·발행처·일자, 여러 장이면 "거래명세서 n장 합계"(세금계산서는 따로 붙임) */
function DocsText({
  docs,
  confirmed,
}: {
  docs: ExpenseDoc[];
  confirmed: boolean;
}) {
  // 증빙 없이 확정한 줄은 남은 일이 아니다 — 중립 표시
  if (docs.length === 0)
    return confirmed ? (
      <Badge variant="muted">증빙 없음</Badge>
    ) : (
      <Badge variant="danger">증빙 못 찾음</Badge>
    );
  if (docs.length === 1) {
    const d = docs[0];
    return (
      <span>
        {DOC_KIND_LABEL[d.kind] ?? d.kind} · {d.issuer}{" "}
        <span className="text-muted-foreground">{md(d.issued_on)}</span>
      </span>
    );
  }
  const st = docs.filter((d) => d.kind === "statement").length;
  const rest = docs.length - st;
  return (
    <span>
      {st > 0 ? `거래명세서 ${st}장 합계` : `증빙 ${docs.length}장 합계`}
      {st > 0 && rest > 0 && (
        <span className="text-muted-foreground"> + 세금계산서 등 {rest}장</span>
      )}
    </span>
  );
}

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

/** 펼친 줄 — 지금 증빙 본문과 출금일 근처 후보(체크해서 증빙을 바꾼다) */
function Detail({
  row,
  picked,
  onToggle,
}: {
  row: ExpenseRow;
  picked: number[];
  onToggle: (id: number) => void;
}) {
  const [cands, setCands] = useState<ExpenseDoc[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchCandidates(row.table, row.id, ac.signal).then(
      (r) => setCands(r.docs),
      (e) => !ac.signal.aborted && setError(errorText(e)),
    );
    return () => ac.abort();
  }, [row.table, row.id]);
  return (
    <section
      aria-label={`출금 상세 ${row.label}`}
      className="grid gap-3 rounded-md border bg-card p-3 md:grid-cols-2"
    >
      <div className="flex flex-col gap-2">
        <h3 className="font-bold">증빙 본문</h3>
        {row.docs.length === 0 ? (
          <p className="text-muted-foreground">찾은 증빙 없음</p>
        ) : (
          row.docs.map((d) => (
            <div key={d.id} className="flex flex-col gap-1">
              <span className="text-muted-foreground">
                {DOC_KIND_LABEL[d.kind] ?? d.kind} · {d.issuer} ·{" "}
                {md(d.issued_on)} · {won(d.amount)}
              </span>
              <pre className="font-sans text-sm whitespace-pre-wrap">
                {d.body}
              </pre>
            </div>
          ))
        )}
      </div>
      <div className="flex flex-col gap-2">
        <h3 className="font-bold">출금일 근처 증빙</h3>
        <p className="text-sm text-muted-foreground">
          체크한 증빙으로 확정합니다
        </p>
        {error && <ErrorBand text={error} />}
        {cands === null && !error && (
          <p className="text-muted-foreground">불러오는 중…</p>
        )}
        {cands?.length === 0 && (
          <p className="text-muted-foreground">근처 증빙 없음</p>
        )}
        <ul className="flex flex-col gap-1 tabular-nums">
          {cands?.map((d) => {
            const text = `${md(d.issued_on)} · ${won(d.amount)} · ${DOC_KIND_LABEL[d.kind] ?? d.kind} · ${d.issuer}`;
            return (
              <li key={d.id} className="flex items-center gap-2">
                <Checkbox
                  aria-label={text}
                  checked={picked.includes(d.id)}
                  onCheckedChange={() => onToggle(d.id)}
                />
                <span>{text}</span>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}

/** 출금 한 줄 — 계정 과목 고르기(기본값과 그 출처)와 확정. 펼치면 증빙 바꾸기 */
function Row({
  row,
  accounts,
  onSaved,
}: {
  row: ExpenseRow;
  accounts: string[];
  onSaved: () => void;
}) {
  // 고른 값이 없으면 줄의 지금 값을 따른다 — 다른 줄 확정으로 기억이 바뀌어도 펼침·고르던 값이 남게
  const [chosen, setChosen] = useState<string | null>(null);
  const [pickedState, setPicked] = useState<number[] | null>(null);
  const account = chosen ?? row.account ?? "";
  const picked = pickedState ?? row.docs.map((d) => d.id);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = () => {
    setBusy(true);
    setError(null);
    confirmExpense(
      row.table,
      row.id,
      [...picked].sort((a, b) => a - b),
      account,
      readPart(),
    ).then(
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
  const c = row.confirmed;
  return (
    <Fragment>
      <tr data-outflow={`${row.table}-${row.id}`} className="border-t">
        <td className="px-3 py-2 whitespace-nowrap">{md(row.at)}</td>
        <td className="px-2 py-2">
          {row.label}
          <div className="text-xs text-muted-foreground">
            {row.table === "fin_bank_transactions" ? "통장" : "카드"}
            {row.vendor && row.vendor !== row.label && ` · ${row.vendor}`}
          </div>
        </td>
        <td className="px-2 py-2 text-right">{won(row.amount)}</td>
        <td className="px-2 py-2">
          <DocsText docs={row.docs} confirmed={!!row.confirmed} />
        </td>
        <td className="px-2 py-2 text-right">
          <Diff n={row.diff} />
        </td>
        <td className="px-2 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Select value={account} onValueChange={setChosen} disabled={busy}>
              <SelectTrigger aria-label="계정 과목" className="w-32 bg-card">
                <SelectValue placeholder="고르기" />
              </SelectTrigger>
              <SelectContent>
                {accounts.map((a) => (
                  <SelectItem key={a} value={a}>
                    {a}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {row.account_from && account === row.account && (
              <span className="text-xs text-muted-foreground">
                {ACCOUNT_FROM_LABEL[row.account_from]}
              </span>
            )}
            {row.check && !c && <Badge variant="warn">과목 확인 필요</Badge>}
          </div>
        </td>
        <td className="px-2 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {c && (
              <Badge variant="ok">
                확정함 · {c.by} {md(c.at)}
              </Badge>
            )}
            <Button
              size="sm"
              variant={c ? "outline" : "default"}
              disabled={busy || !account}
              onClick={save}
            >
              확정
            </Button>
            <Button
              size="sm"
              variant="ghost"
              aria-expanded={open}
              onClick={() => setOpen((v) => !v)}
            >
              {open ? "접기" : "펼치기"}
            </Button>
          </div>
        </td>
      </tr>
      {(open || error) && (
        <tr>
          <td colSpan={7} className="bg-muted/40 px-3 py-3">
            {error && <ErrorBand text={error} />}
            {open && (
              <Detail
                row={row}
                picked={picked}
                onToggle={(id) =>
                  setPicked(
                    picked.includes(id)
                      ? picked.filter((x) => x !== id)
                      : [...picked, id],
                  )
                }
              />
            )}
          </td>
        </tr>
      )}
    </Fragment>
  );
}

/** 지출 증빙 표 — 출금일 · 출금처 · 금액 · 찾은 증빙 · 금액 차이 · 계정 과목 · 확정(설계 §2) */
export function ExpenseTable({
  sheet,
  chip,
  onSaved,
}: {
  sheet: ExpenseSheet;
  chip: ExpenseChip | null;
  onSaved: () => void;
}) {
  const rows = chip ? sheet.rows.filter(keep[chip]) : sheet.rows;
  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm tabular-nums">
        <thead className="bg-muted text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-left">출금일</th>
            <th className="px-2 py-2 text-left">출금처</th>
            <th className="px-2 py-2 text-right">금액</th>
            <th className="px-2 py-2 text-left">찾은 증빙</th>
            <th className="px-2 py-2 text-right">금액 차이</th>
            <th className="px-2 py-2 text-left">계정 과목</th>
            <th className="px-2 py-2 text-left">확정</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td
                colSpan={7}
                className="px-3 py-6 text-center text-muted-foreground"
              >
                해당하는 출금이 없습니다
              </td>
            </tr>
          )}
          {rows.map((r) => (
            // 이 줄을 확정하면 고른 값을 새로 시작한다
            <Row
              key={`${r.table}-${r.id}-${r.confirmed?.at}`}
              row={r}
              accounts={sheet.accounts}
              onSaved={onSaved}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
