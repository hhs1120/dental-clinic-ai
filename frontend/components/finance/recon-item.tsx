"use client";

import { useEffect, useRef, useState } from "react";

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
import { ApiError, errorText } from "@/lib/api";
import { revertAuto } from "@/lib/calls-api";
import {
  agentCauseLabel,
  type Cause,
  CAUSE_LABEL,
  CAUSES,
  LEDGER_CAUSES,
  METHOD_LABEL,
  postConfirm,
  type ReconItem,
  type ReconLine,
  streamInvestigate,
  TOOL_LABEL,
} from "@/lib/finance-api";
import { won } from "@/lib/format";
import { readPart } from "@/lib/part";

/** 2026-11-03 10:00:00 → 11/03 10:00 (날짜만이면 11/03) */
const at = (s: string) =>
  `${s.slice(5, 7)}/${s.slice(8, 10)}${s.length > 10 ? ` ${s.slice(11, 16)}` : ""}`;

const LINE_BADGE: Record<
  Exclude<ReconLine["kind"], "note" | "lookup">,
  { label: string; variant: "muted" | "warn" | "ok" }
> = {
  hypothesis: { label: "가설", variant: "muted" },
  compare: { label: "비교", variant: "muted" },
  blocked: { label: "막힘", variant: "warn" },
  conclusion: { label: "결론", variant: "ok" },
};

/** 조사 과정 줄 하나 — 생각(머리말 없는 글)은 배지 없이 흐리게 */
function Line({ line }: { line: ReconLine }) {
  if (line.kind === "note")
    return <li className="text-sm text-muted-foreground">{line.text}</li>;
  if (line.kind === "lookup")
    return (
      <li className="flex gap-2 text-sm">
        <Badge variant="info">조회 {line.lookup_no}</Badge>
        <span>
          {TOOL_LABEL[line.tool ?? ""] ?? line.tool} — {line.text}
        </span>
      </li>
    );
  const b = LINE_BADGE[line.kind];
  return (
    <li className="flex gap-2 text-sm">
      <Badge variant={b.variant}>{b.label}</Badge>
      <span>
        {line.kind === "conclusion" && (
          <b>
            {agentCauseLabel(line.cause)}
            {line.evidence?.length
              ? ` · 근거 조회 ${line.evidence.join("·")}`
              : ""}{" "}
            —{" "}
          </b>
        )}
        {line.text}
      </span>
    </li>
  );
}

/** 다른 곳(다른 탭·일괄)에서 조사 중인 건을 다시 불러오는 간격 */
const RUNNING_POLL_MS = 3_000;

/** 조사 과정 — 저장된 줄을 보이고, 조사 전이면 `조사 시작`(누르면 줄이 하나씩). 다시 조사는 없다(설계 §3).
 * 조사가 실패하면 오류와 함께 다시 시작할 수 있고, 다른 곳에서 조사 중이면 끝날 때까지 다시 불러온다 */
function Investigation({
  item,
  onDone,
}: {
  item: ReconItem;
  onDone: () => void;
}) {
  const [live, setLive] = useState<ReconLine[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  const start = () => {
    const ctl = new AbortController();
    abort.current = ctl;
    setLive([]);
    setError(null);
    streamInvestigate(
      item.key,
      (e) => {
        if (e.type === "line") setLive((ls) => [...(ls ?? []), e.line]);
        else if (e.type === "error") setError(e.message);
        else onDone();
      },
      ctl.signal,
    ).catch(() => {});
  };
  const elsewhere = live === null && item.agent?.status === "running";
  const reload = useRef(onDone);
  useEffect(() => {
    reload.current = onDone;
  });
  useEffect(() => {
    if (!elsewhere) return;
    const t = setInterval(() => reload.current(), RUNNING_POLL_MS);
    return () => clearInterval(t);
  }, [elsewhere]);
  const saved = item.agent?.lines;
  const lines = live ?? saved;
  const running = elsewhere || (live !== null && !error && item.agent == null);
  return (
    <div className="flex flex-col gap-1 rounded-md bg-muted/40 p-2">
      <div className="flex items-center gap-2">
        <h3 className="font-bold">조사 과정</h3>
        {running && (
          <span className="text-sm text-muted-foreground">조사 중…</span>
        )}
        <span className="flex-1" />
        {item.agent == null && (live === null || error !== null) && (
          <Button size="sm" variant="outline" onClick={start}>
            조사 시작
          </Button>
        )}
      </div>
      {lines && lines.length > 0 && (
        <ol className="flex flex-col gap-1">
          {lines.map((l, k) => (
            <Line key={k} line={l} />
          ))}
        </ol>
      )}
      {error && <ErrorBand text={error} />}
    </div>
  );
}

/** 불일치 건 하나 — 관련 장부·금융 행, 조사 과정, 규칙 추정·에이전트 원인, 자동 마감, 사람 확정(맞음 / 아님 → 원인 고르기).
 * 맞음은 에이전트 결과가 있으면 그 1순위를, 없으면 규칙 추정을 확정한다(원인 미정이면 맞음 없이 고르기만) */
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
  const agent = item.agent;
  const proposed: Cause | null =
    agent && agent.cause && agent.cause !== "unknown"
      ? agent.cause
      : agent?.cause === "unknown"
        ? null
        : item.rule_cause;
  const auto = item.auto;
  const reopen = (id: number) => {
    setBusy(true);
    setError(null);
    revertAuto(id, readPart()).then(
      () => {
        setBusy(false);
        onSaved();
      },
      (e) => {
        setBusy(false);
        // 409 = 다른 사람이 먼저 되돌림 — 오류 없이 다시 조회만
        if (e instanceof ApiError && e.status === 409) onSaved();
        else setError(errorText(e));
      },
    );
  };
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
      <Investigation item={item} onDone={onSaved} />
      <div className="flex flex-wrap items-center gap-2">
        {agent && agent.cause && (
          <span>
            에이전트: <b>{agentCauseLabel(agent.cause)}</b>
            {agent.runner_up && (
              <span className="text-muted-foreground">
                {" "}
                (2순위 {agentCauseLabel(agent.runner_up)})
              </span>
            )}
          </span>
        )}
        <span className={agent?.cause ? "text-muted-foreground" : undefined}>
          규칙 추정: <b>{CAUSE_LABEL[item.rule_cause]}</b>
        </span>
        {auto && (
          <>
            <Badge variant="info">자동 마감 · {at(auto.at)}</Badge>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => reopen(auto.id)}
            >
              되돌리기
            </Button>
          </>
        )}
        {c && (
          <Badge variant="ok">
            {CAUSE_LABEL[c.cause]} · {c.by} {at(c.at)}
          </Badge>
        )}
        <span className="flex-1" />
        <Button
          size="sm"
          variant={c && c.cause === proposed ? "secondary" : "outline"}
          disabled={busy || proposed === null}
          onClick={() => proposed && save(proposed)}
        >
          맞음
        </Button>
        <span className="text-sm text-muted-foreground">아님 →</span>
        <Select
          value={c && c.cause !== proposed ? c.cause : ""}
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
