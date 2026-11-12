"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorBand, POLL_MS } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import {
  type ClaimCheck,
  fetchPreClaim,
  monthDay,
  type PreSheet,
  runClaimCheck,
  toothText,
} from "@/lib/claims-api";
import { won } from "@/lib/format";
import { fetchJob } from "@/lib/prep-api";

type PreChip = "lines" | "warning" | "ai";

/** AI 칩에 드는 줄 — 요건 근거를 못 찾았거나 차트 기반 누락이 있다(backend/claims.py counts.ai와 같은 조건) */
const flagged = (ai: ClaimCheck | null) =>
  !!ai && (ai.requirement === "missing" || ai.omissions.length > 0);

/** 청구 전 점검 — 대기 청구 줄의 상병 확인 경고(규칙, 설계 §1)와 AI 점검(§2, 점검 결과가 있을 때만 칩). 기본 칩은 경고 줄.
 * AI 점검 갱신은 준비표 갱신처럼 작업을 폴링하고 끝나면 다시 조회한다 */
export function PreClaimTable({
  chip: chipParam,
  onChip,
}: {
  chip: string | null;
  onChip: (chip: string | null) => void;
}) {
  const asked: PreChip =
    chipParam === "lines" || chipParam === "ai" ? chipParam : "warning";
  const [sheet, setSheet] = useState<PreSheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [job, setJob] = useState<number | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchPreClaim(ac.signal).then(
      (data) => {
        setSheet(data);
        setError(null);
      },
      (e) => !ac.signal.aborted && setError(errorText(e)),
    );
    return () => ac.abort();
  }, [attempt]);
  useEffect(() => {
    if (job === null) return;
    const timer = setInterval(() => {
      fetchJob(job).then(
        (j) => {
          if (j.status === "running") return;
          setJob(null);
          if (j.status === "failed") setError(j.error ?? "AI 점검 갱신 실패");
          setAttempt((n) => n + 1);
        },
        (e) => {
          setJob(null);
          setError(errorText(e));
        },
      );
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [job]);

  if (!sheet)
    return error ? (
      <ErrorBand text={error} />
    ) : (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  // 점검 결과가 없으면 AI 칩이 없다 — 주소로 들어와도 경고 칩으로
  const chip: PreChip =
    asked === "ai" && sheet.counts.ai_checked === 0 ? "warning" : asked;
  const rows =
    chip === "warning"
      ? sheet.rows.filter((r) => r.warning)
      : chip === "ai"
        ? sheet.rows.filter((r) => flagged(r.ai))
        : sheet.rows;
  const chips: [PreChip, string][] = [
    ["lines", `청구 전 ${sheet.counts.lines}줄`],
    ["warning", `상병 확인 경고(규칙) ${sheet.counts.warning}`],
    ...(sheet.counts.ai_checked > 0
      ? [
          ["ai", `임상 요건·차트 기반 누락(AI) ${sheet.counts.ai}`] as [
            PreChip,
            string,
          ],
        ]
      : []),
  ];
  const need = sheet.ai_pending.pre + sheet.ai_pending.adjust;
  const start = () => {
    setError(null);
    runClaimCheck().then(
      ({ job_id }) => setJob(job_id),
      (e) => setError(errorText(e)),
    );
  };
  return (
    <div className="flex flex-col gap-3">
      {error && <ErrorBand text={error} />}
      <div className="flex flex-wrap items-center gap-1.5">
        <div className="flex flex-wrap gap-1.5" aria-label="칩">
          {chips.map(([c, label]) => (
            <Button
              key={c}
              size="sm"
              variant={c === chip ? "secondary" : "outline"}
              aria-pressed={c === chip}
              onClick={() => onChip(c === "warning" ? null : c)}
            >
              {label}
            </Button>
          ))}
        </div>
        <span className="flex-1" />
        {sheet.ai_at && (
          <span className="text-sm text-muted-foreground tabular-nums">
            AI 점검: {monthDay(sheet.ai_at)} {sheet.ai_at.slice(11, 16)} 기준
          </span>
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={job !== null || need === 0}
          onClick={start}
        >
          {job !== null
            ? "AI 점검 중…"
            : need > 0
              ? `AI 점검 갱신하기 (${pendingText(sheet.ai_pending)})`
              : "AI 점검 최신"}
        </Button>
      </div>
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2">진료일</th>
              <th className="px-3 py-2">환자</th>
              <th className="px-3 py-2">시술(치아)</th>
              <th className="px-3 py-2 text-right">청구액</th>
              <th className="px-3 py-2">차트 상병</th>
              <th className="px-3 py-2">경고</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.claim_id} className="border-t">
                <td className="px-3 py-2 align-middle whitespace-nowrap tabular-nums">
                  {r.service_date}
                </td>
                <td className="px-3 py-2 align-middle whitespace-nowrap">
                  <Link
                    href={`/emr?chart=${encodeURIComponent(r.chart_no)}`}
                    className="font-bold text-primary hover:underline"
                  >
                    {r.name}
                  </Link>
                </td>
                <td className="px-3 py-2 align-middle">
                  {r.procedure_name}
                  {toothText(r.tooth_no) && ` (${toothText(r.tooth_no)})`}
                </td>
                <td className="px-3 py-2 text-right align-middle tabular-nums">
                  {won(r.claim_amount)}
                </td>
                <td className="px-3 py-2 align-middle">{r.dx_name ?? "-"}</td>
                <td className="px-3 py-2 align-middle">
                  {r.warning || flagged(r.ai) ? (
                    <div className="flex flex-col items-start gap-1">
                      {r.warning && (
                        <>
                          <Badge variant="warn">
                            허용 상병 밖 — 이 시술은{" "}
                            {r.warning.allowed.join(" · ")}
                          </Badge>
                          {r.warning.past_count > 0 && (
                            <span className="text-xs text-muted-foreground tabular-nums">
                              지난 조정에서 이 조합 {r.warning.past_count}건,{" "}
                              {won(r.warning.past_amount)} 못 받음
                            </span>
                          )}
                        </>
                      )}
                      {r.ai && flagged(r.ai) && <AiFinding ai={r.ai} />}
                    </div>
                  ) : (
                    <span className="text-muted-foreground">-</span>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-10 text-center text-muted-foreground"
                >
                  조건에 맞는 청구 줄이 없습니다
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** 갱신 버튼 줄 수 — 버튼 한 번으로 두 탭이 같이 갱신돼 탭별로 나눠 적는다, 0인 쪽은 뺀다 */
const pendingText = (p: PreSheet["ai_pending"]) =>
  [p.pre && `청구 전 ${p.pre}줄`, p.adjust && `삭감 대응 ${p.adjust}건`]
    .filter(Boolean)
    .join(" · ");

/** AI 점검 결과 — 사실 문장 딱지와 인용(기준이 요구하는 것·차트에 있는데 청구에 없는 급여 처치) */
function AiFinding({ ai }: { ai: ClaimCheck }) {
  return (
    <>
      {ai.requirement === "missing" && (
        <>
          <Badge variant="warn">차트에 요건 근거 못 찾음(AI)</Badge>
          {ai.evidence.map((e) => (
            <span key={e.quote} className="text-xs text-muted-foreground">
              {e.source === "standard" ? "기준" : "차트"}: “{e.quote}”
            </span>
          ))}
        </>
      )}
      {ai.omissions.length > 0 && (
        <>
          <Badge variant="warn">차트에 있는데 청구에 없음(AI)</Badge>
          {ai.omissions.map((o) => (
            <span
              key={`${o.procedure_code}-${o.tooth}`}
              className="text-xs text-muted-foreground"
            >
              {o.procedure} {o.tooth}번: “{o.quote}”
            </span>
          ))}
        </>
      )}
    </>
  );
}
