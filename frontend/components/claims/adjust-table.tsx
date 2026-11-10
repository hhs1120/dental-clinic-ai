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
  CHIP_LABEL,
  CHIPS,
  fetchClaims,
  type Judgment,
  JUDGMENT_LABEL,
  JUDGMENTS,
  krw,
  monthDay,
  postJudgment,
  toothText,
} from "@/lib/claims-api";
import { readPart } from "@/lib/part";

// 꼬리표 경계 — backend/claims.py SOON_DAYS·WATCH_DAYS와 같다(칩 집계는 서버가 낸다)
const SOON_DAYS = 30;
const WATCH_DAYS = 60;

/** 삭감 대응 — 조정·불능 청구 5칸 목록(설계 §1). 칩은 서버가 행마다 붙인 chips로 거르고, 순서는 서버 그대로(기한 순) */
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

  const rows = sheet.rows.filter((r) => r.chips.includes(chip));
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
              {CHIP_LABEL[c]} {sheet.chips[c].count}
            </Button>
          ))}
        </div>
        <span className="flex-1" />
        <span className="text-sm font-bold tabular-nums">
          못 받은 돈 합계 {krw(sheet.chips[chip].amount)}
        </span>
      </div>
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2">재심 신청 기한</th>
              <th className="px-3 py-2">환자 · 시술</th>
              <th className="px-3 py-2 text-right">못 받은 돈</th>
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
                  <td className="px-3 py-2 align-top whitespace-nowrap tabular-nums">
                    <Deadline row={r} />
                  </td>
                  <td className="px-3 py-2 align-top">
                    <div className="font-bold">
                      {r.name} · {r.procedure_name}
                    </div>
                    <div className="text-xs text-muted-foreground tabular-nums">
                      {[r.service_date, toothText(r.tooth_no)]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-right align-top whitespace-nowrap tabular-nums">
                    {krw(r.unpaid)}
                    {r.status === "denied" && " (전액)"}
                  </td>
                  <td className="px-3 py-2 align-top">{r.adjust_note}</td>
                  <td className="px-3 py-2 align-top whitespace-nowrap">
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

/** 기한 칸 — 기한 지남, 상병 착오는 날짜 대신 재청구 불가, 그 밖은 남은 날과 30·60일 꼬리표 */
function Deadline({ row: r }: { row: AdjustRow }) {
  const day = monthDay(r.deadline);
  if (r.expired) return <>{day} · 기한 지남</>;
  if (r.type === "diagnosis") return <Badge variant="muted">재청구 불가</Badge>;
  return (
    <div className="flex flex-col items-start gap-1">
      <span>
        {day} · {r.days_left === 0 ? "오늘까지" : `${r.days_left}일 남음`}
      </span>
      {r.days_left <= SOON_DAYS ? (
        <Badge variant="danger">30일 이내</Badge>
      ) : r.days_left <= WATCH_DAYS ? (
        <Badge variant="warn">60일 이내</Badge>
      ) : null}
    </div>
  );
}

const judgedText = (j: NonNullable<AdjustRow["judgment"]>) =>
  `${JUDGMENT_LABEL[j.value]} · ${j.by} ${monthDay(j.at)}`;

/** 할 일 칸 — 사람이 정한 판단, 없으면 규칙 제안(요건 사유는 차트 확인 필요), 기한 지난 건은 남긴 판단만 */
function Todo({ row: r }: { row: AdjustRow }) {
  if (r.judgment) return <Badge variant="ok">{judgedText(r.judgment)}</Badge>;
  if (r.expired) return <span className="text-muted-foreground">—</span>;
  if (r.suggestion)
    return (
      <span className="text-muted-foreground">
        {JUDGMENT_LABEL[r.suggestion]}(제안)
      </span>
    );
  return <span className="text-muted-foreground">차트 확인 필요</span>;
}

/** 펼친 곳 — ① 왜 깎였나 ② 어떻게 할까(제안·이유·판단 버튼). 상병 착오는 정당만, 기한 지난 건은 버튼 없이 */
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
  return (
    <section
      aria-label={`${r.name} 청구 상세`}
      className="grid gap-4 md:grid-cols-2"
    >
      <div className="flex flex-col gap-1">
        <h3 className="font-bold">왜 깎였나</h3>
        <p>{r.adjust_note}</p>
        <p className="tabular-nums">
          청구 {krw(r.claim_amount)} → 지급 {krw(r.paid_amount)} ·{" "}
          {r.status === "denied" ? "전액 불능" : "일부 조정"}
        </p>
        <p className="text-xs text-muted-foreground tabular-nums">
          심사결과 통보 {r.reviewed_at} · 재심 신청 기한 {r.deadline}
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <h3 className="font-bold">어떻게 할까</h3>
        <p>
          {r.suggestion
            ? `제안: ${JUDGMENT_LABEL[r.suggestion]}`
            : "차트 확인 필요"}
          <span className="text-muted-foreground"> — {r.reason}</span>
        </p>
        {r.fill_item && <p>채울 항목: {r.fill_item}</p>}
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
                disabled={busy}
                onClick={() => save(j)}
              >
                {JUDGMENT_LABEL[j]}
              </Button>
            ))}
          </div>
        )}
        {error && <ErrorBand text={error} />}
      </div>
    </section>
  );
}
