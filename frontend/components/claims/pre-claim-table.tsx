"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText } from "@/lib/api";
import { fetchPreClaim, type PreSheet, toothText } from "@/lib/claims-api";
import { won } from "@/lib/format";

type PreChip = "lines" | "warning";

/** 청구 전 점검 — 대기 청구 줄의 상병 확인 경고(규칙, 설계 §1). 기본 칩은 경고 줄 */
export function PreClaimTable({
  chip: chipParam,
  onChip,
}: {
  chip: string | null;
  onChip: (chip: string | null) => void;
}) {
  const chip: PreChip = chipParam === "lines" ? "lines" : "warning";
  const [sheet, setSheet] = useState<PreSheet | null>(null);
  const [error, setError] = useState<string | null>(null);
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
  }, []);

  if (!sheet)
    return error ? (
      <ErrorBand text={error} />
    ) : (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  const rows =
    chip === "warning" ? sheet.rows.filter((r) => r.warning) : sheet.rows;
  const chips: [PreChip, string][] = [
    ["lines", `청구 전 ${sheet.counts.lines}줄`],
    ["warning", `상병 확인 경고(규칙) ${sheet.counts.warning}`],
  ];
  return (
    <div className="flex flex-col gap-3">
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
                <td className="px-3 py-2 align-top whitespace-nowrap tabular-nums">
                  {r.service_date}
                </td>
                <td className="px-3 py-2 align-top whitespace-nowrap">
                  <Link
                    href={`/emr?chart=${encodeURIComponent(r.chart_no)}`}
                    className="font-bold text-primary hover:underline"
                  >
                    {r.name}
                  </Link>
                </td>
                <td className="px-3 py-2 align-top">
                  {r.procedure_name}
                  {toothText(r.tooth_no) && ` (${toothText(r.tooth_no)})`}
                </td>
                <td className="px-3 py-2 text-right align-top tabular-nums">
                  {won(r.claim_amount)}
                </td>
                <td className="px-3 py-2 align-top">{r.dx_name ?? "-"}</td>
                <td className="px-3 py-2 align-top">
                  {r.warning ? (
                    <div className="flex flex-col items-start gap-1">
                      <Badge variant="warn">
                        허용 상병 밖 — 이 시술은 {r.warning.allowed.join(" · ")}
                      </Badge>
                      {r.warning.past_count > 0 && (
                        <span className="text-xs text-muted-foreground tabular-nums">
                          지난 조정에서 이 조합 {r.warning.past_count}건,{" "}
                          {won(r.warning.past_amount)} 못 받음
                        </span>
                      )}
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
