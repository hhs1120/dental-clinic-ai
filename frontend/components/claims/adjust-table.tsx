"use client";

import { Fragment, useEffect, useState } from "react";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import {
  type AdjustRow,
  type AdjustSheet,
  type Chip,
  chipLabel,
  CHIPS,
  fetchClaims,
  type Judgment,
  AI_SUGGEST_TEXT,
  JUDGMENT_LABEL,
  JUDGMENTS,
  monthDay,
  postJudgment,
  shortDate,
  toothText,
} from "@/lib/claims-api";
import { won } from "@/lib/format";
import { readPart } from "@/lib/part";
import { DocBody } from "@/components/ask/doc-body";
import { AppealDraft } from "@/components/claims/appeal-draft";

type Sort = "deadline" | "amount";
/** 지금 정렬 문구 — 기한 지난 건은 기한 지남 칩에만 있고 서버가 최근에 지난 순으로 준다 */
const sortText = (sort: Sort, chip: Chip) =>
  sort === "amount"
    ? "못 받은 돈 큰 순"
    : chip === "expired"
      ? "최근에 지난 순"
      : "기한 가까운 순";

/** 기한순은 서버 순서 그대로, 금액순은 못 받은 돈 큰 순 */
const sortRows = (rows: AdjustRow[], sort: Sort) =>
  sort === "deadline"
    ? rows
    : [...rows].sort((a, b) => b.unpaid - a.unpaid || a.claim_id - b.claim_id);

/** 삭감 대응 — 조정·불능 청구 5칸 목록(설계 §1). 칩은 서버가 행마다 붙인 chips로 거르고, 정렬은 기한순(서버 순서)·금액순 */
export function AdjustTable({
  chip: chipParam,
  onChip,
}: {
  chip: string | null;
  onChip: (chip: string | null) => void;
}) {
  const chip: Chip = (CHIPS as string[]).includes(chipParam ?? "")
    ? (chipParam as Chip)
    : "all";
  const [attempt, setAttempt] = useState(0);
  const [sheet, setSheet] = useState<AdjustSheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [sort, setSort] = useState<Sort>("deadline");
  useEffect(() => {
    const ac = new AbortController();
    fetchClaims(ac.signal).then(
      (data) => {
        setSheet(data);
        setError(null);
      },
      (e) => !ac.signal.aborted && setError(errorText(e)),
    );
    return () => ac.abort();
  }, [attempt]);

  if (!sheet)
    return error ? (
      <ErrorBand text={error} />
    ) : (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  const rows = sortRows(
    sheet.rows.filter((r) => r.chips.includes(chip)),
    sort,
  );
  const sortHead = (s: Sort, label: string, className = "") => (
    <th
      className={`px-3 py-2 ${className}`}
      aria-sort={
        sort !== s
          ? undefined
          : s === "deadline" && chip !== "expired"
            ? "ascending"
            : "descending"
      }
    >
      <button
        type="button"
        className={sort === s ? "font-bold text-foreground" : "hover:underline"}
        onClick={() => setSort(s)}
      >
        {label}
      </button>
      {sort === s && <span aria-hidden> ↓</span>}
    </th>
  );
  return (
    <div className="flex flex-col gap-3">
      {error && <ErrorBand text={error} />}
      <div className="flex flex-wrap items-center gap-1.5">
        <div className="flex flex-wrap gap-1.5" aria-label="칩">
          {CHIPS.map((c) => (
            <Button
              key={c}
              size="sm"
              variant={c === chip ? "secondary" : "outline"}
              aria-pressed={c === chip}
              onClick={() => {
                setOpen(null);
                onChip(c === "all" ? null : c);
              }}
            >
              {chipLabel(c, sheet.soon_days)} {sheet.chips[c].count}
            </Button>
          ))}
        </div>
        <span className="flex-1" />
        <span className="text-sm text-muted-foreground">
          {sortText(sort, chip)}
        </span>
        <span className="text-sm font-bold tabular-nums">
          못 받은 돈 합계 {won(sheet.chips[chip].amount)}
        </span>
      </div>
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted text-left text-muted-foreground">
            <tr>
              {sortHead("deadline", "재심 신청 기한")}
              <th className="px-3 py-2">환자 · 시술</th>
              {sortHead("amount", "못 받은 돈", "text-right")}
              <th className="px-3 py-2">왜 깎였나</th>
              <th className="px-3 py-2">할 일</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Fragment key={r.claim_id}>
                <tr
                  data-claim={r.claim_id}
                  tabIndex={0}
                  aria-expanded={open === r.claim_id}
                  className="cursor-pointer border-t hover:bg-muted/50"
                  onClick={() =>
                    setOpen(open === r.claim_id ? null : r.claim_id)
                  }
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setOpen(open === r.claim_id ? null : r.claim_id);
                    }
                  }}
                >
                  <td className="px-3 py-2 align-middle whitespace-nowrap tabular-nums">
                    <Deadline row={r} sheet={sheet} />
                  </td>
                  <td className="px-3 py-2 align-middle">
                    <div className="font-bold">
                      {r.name} · {r.procedure_name}
                    </div>
                    <div className="text-sm tabular-nums">
                      {[
                        `진료일 ${shortDate(r.service_date, sheet.date)}`,
                        toothText(r.tooth_no),
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right align-middle whitespace-nowrap tabular-nums">
                    {won(r.unpaid)}
                    {r.status === "denied" && " (전액)"}
                  </td>
                  <td className="px-3 py-2 align-middle">{r.adjust_note}</td>
                  <td className="px-3 py-2 align-middle whitespace-nowrap">
                    <Todo row={r} />
                  </td>
                </tr>
                {open === r.claim_id && (
                  <tr>
                    <td colSpan={5} className="bg-muted/30 px-3 py-3">
                      <Detail
                        row={r}
                        onSaved={() => setAttempt((n) => n + 1)}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={5}
                  className="px-3 py-10 text-center text-muted-foreground"
                >
                  조건에 맞는 청구가 없습니다
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** 기한 칸 — 날짜와 남은 날(지났으면 기한 지남 딱지), 상병 착오는 재청구 불가 딱지, 그 밖은 위험·주의 꼬리표(경계는 서버 값) */
function Deadline({ row: r, sheet }: { row: AdjustRow; sheet: AdjustSheet }) {
  const day = shortDate(r.deadline, sheet.date);
  return (
    <div className="flex flex-col items-start gap-1">
      {r.expired ? (
        <span className="flex items-center gap-1">
          {day}
          <Badge variant="muted">기한 지남</Badge>
        </span>
      ) : (
        <span>
          {day} · {r.days_left === 0 ? "오늘까지" : `${r.days_left}일 남음`}
        </span>
      )}
      {r.type === "diagnosis" ? (
        <Badge variant="muted">재청구 불가</Badge>
      ) : r.expired ? null : r.days_left <= sheet.soon_days ? (
        <Badge variant="danger">{sheet.soon_days}일 이내</Badge>
      ) : r.days_left <= sheet.watch_days ? (
        <Badge variant="warn">{sheet.watch_days}일 이내</Badge>
      ) : null}
    </div>
  );
}

const judgedText = (j: NonNullable<AdjustRow["judgment"]>) =>
  `${JUDGMENT_LABEL[j.value]} · ${j.by} ${monthDay(j.at)}`;

/** 할 일 칸 — 사람이 정한 판단, 없으면 판단 필요와 규칙 추천(요건 사유는 차트 확인 후 결정), 기한 지난 건은 남긴 판단만 */
function Todo({ row: r }: { row: AdjustRow }) {
  if (r.judgment) return <Badge variant="ok">{judgedText(r.judgment)}</Badge>;
  if (r.expired) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="flex flex-col items-start gap-1">
      <Badge variant="warn">판단 필요</Badge>
      <span className="text-muted-foreground">{suggestText(r)}</span>
    </div>
  );
}

/** 규칙 추천은 "추천: …", AI 추천은 AI가 본 사실 문장(요건 사유, 설계 §2), 없으면 차트 확인 후 결정 */
const suggestText = (r: AdjustRow) =>
  !r.suggestion
    ? "차트 확인 후 결정"
    : r.suggest_by === "ai"
      ? (AI_SUGGEST_TEXT[r.suggestion] ??
        `추천: ${JUDGMENT_LABEL[r.suggestion]}`)
      : `추천: ${JUDGMENT_LABEL[r.suggestion]}`;

/** 펼친 곳 — ① 왜 깎였나 ② 어떻게 할까(추천·이유·판단 버튼). 상병 착오는 삭감 수용만, 기한 지난 건은 버튼 없이 */
function Detail({ row: r, onSaved }: { row: AdjustRow; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const choices: Judgment[] =
    r.type === "diagnosis" ? ["justified"] : JUDGMENTS;
  const save = (j: Judgment) => {
    setBusy(true);
    setError(null);
    postJudgment(r.claim_id, j, readPart()).then(
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
  // 재심 신청 건 — 기한이 지나면 초안을 쓸 수 없지만 제출 결정한 사유서는 읽기 전용으로 남긴다
  const contested =
    r.judgment?.value === "contestable" &&
    (!r.expired || !!r.appeal?.submitted);
  const locked = !!r.appeal?.submitted; // 제출 결정한 건은 판단을 바꾸지 않는다(설계 §4)
  return (
    <section
      aria-label={`${r.name} 청구 상세`}
      className={`grid gap-4 ${contested ? "md:grid-cols-3" : "md:grid-cols-2"}`}
    >
      <div className="flex flex-col gap-1">
        <h3 className="font-bold">왜 깎였나</h3>
        <p>{r.adjust_note}</p>
        <p className="tabular-nums">
          청구 {won(r.claim_amount)} → 지급 {won(r.paid_amount)} ·{" "}
          {r.status === "denied" ? "전액 불능" : "일부 조정"}
        </p>
        <p className="text-xs text-muted-foreground tabular-nums">
          심사결과 통보 {r.reviewed_at} · 재심 신청 기한 {r.deadline}
        </p>
        {r.standard && <StandardBox card={r.standard} />}
      </div>
      <div className="flex flex-col gap-1.5">
        <h3 className="font-bold">어떻게 할까</h3>
        <p>
          {suggestText(r)}
          <span className="text-muted-foreground"> — {r.reason}</span>
        </p>
        {r.ai_evidence?.map((e) => (
          <p key={e.quote} className="text-sm text-muted-foreground">
            {e.source === "chart" ? "차트" : "기준"}: “{e.quote}”
          </p>
        ))}
        {r.fill_item && <p>채울 항목: {r.fill_item}</p>}
        {r.chart_quotes && <ChartQuotes quotes={r.chart_quotes} />}
        {r.judgment && (
          <p>
            남긴 판단: <b>{judgedText(r.judgment)}</b>
          </p>
        )}
        {r.expired ? (
          <p className="text-xs text-muted-foreground">
            재심 신청 기한이 지나 판단을 남길 수 없습니다
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5" aria-label="판단">
            {choices.map((j) => (
              <Button
                key={j}
                size="sm"
                variant={r.judgment?.value === j ? "secondary" : "outline"}
                aria-pressed={r.judgment?.value === j}
                disabled={busy || locked}
                onClick={() => save(j)}
              >
                {JUDGMENT_LABEL[j]}
              </Button>
            ))}
          </div>
        )}
        {locked && (
          <p className="text-xs text-muted-foreground">
            재심 사유서를 제출 결정해 판단을 바꿀 수 없습니다
          </p>
        )}
        {error && <ErrorBand text={error} />}
      </div>
      {contested && (
        <AppealDraft claimId={r.claim_id} appeal={r.appeal} onSaved={onSaved} />
      )}
    </section>
  );
}

/** 기준 조항 카드 — 그 사유의 청구 기준 절 전문(설계 §3), 머리는 `문서 › 절`. 본문은 문서함과 같은 마크다운(고시 발췌는 인용 블록) */
export function StandardBox({
  card,
}: {
  card: { doc_title: string; heading: string; body: string };
}) {
  return (
    <div className="mt-1 rounded-lg border bg-card p-2.5 text-sm">
      <p className="text-xs text-muted-foreground">
        {card.doc_title} › {card.heading}
      </p>
      <DocBody markdown={card.body} className="" />
    </div>
  );
}

/** 서류 사유 차트 문장(AI) — 보완 서류에 옮겨 적을 차트 인용, 빈 답이면 없음 한 줄 */
function ChartQuotes({ quotes }: { quotes: string[] }) {
  if (!quotes.length)
    return (
      <p className="text-sm text-muted-foreground">
        차트에 옮겨 적을 문장 없음
      </p>
    );
  return (
    <div className="rounded-lg border border-status-warn-border bg-status-warn-bg p-2.5 text-sm text-status-warn-fg">
      <p className="text-xs font-bold">차트에서 옮겨 적을 문장(AI)</p>
      {quotes.map((q, i) => (
        <p key={`${i}-${q}`}>“{q}”</p>
      ))}
    </div>
  );
}
