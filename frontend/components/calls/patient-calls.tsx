"use client";

import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { useFetch } from "@/lib/api";
import { DIRECTION_LABEL, fetchPatientCalls, useLabels } from "@/lib/calls-api";

/** 이 환자의 지난 통화 — 통화 상세·환자 요약 카드가 같이 쓴다(기능 정의 §4 ①).
 * 최신순 몇 건만, 더 있으면 통화 기록에서 그 환자 차트번호로 걸러 전체 보기.
 * onOpen이 있으면 같은 화면에서 그 통화를 열고, 없으면 통화 화면으로 간다 */
export function PatientCalls({
  chartNo,
  exclude = null,
  onOpen,
  label = "이 환자의 지난 통화",
  empty = "지난 통화 없음",
}: {
  chartNo: string;
  exclude?: string | null;
  onOpen?: (uid: string) => void;
  label?: string; // 칸 제목과 같은 목록 이름
  empty?: string;
}) {
  const names = useLabels();
  const { data, error } = useFetch(`${chartNo}|${exclude ?? ""}`, (signal) =>
    fetchPatientCalls(chartNo, exclude, signal),
  );
  if (error) return <p className="text-sm text-muted-foreground">{error}</p>;
  if (!data)
    return <p className="text-sm text-muted-foreground">불러오는 중…</p>;
  if (!data.calls.length)
    return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      <ul className="-mx-2 flex flex-col gap-1.5" aria-label={label}>
        {data.calls.map((c) => {
          const line = (
            <>
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="text-muted-foreground">
                  {c.at.slice(0, 16)} · {DIRECTION_LABEL[c.direction]}
                  {c.purpose
                    ? ` · ${names?.purpose[c.purpose] ?? c.purpose}`
                    : ""}
                </span>
                {c.emergency && <Badge variant="danger">응급</Badge>}
                {c.complaint && <Badge variant="warn">불만</Badge>}
                {c.status !== "ok" && <Badge variant="muted">분류 없음</Badge>}
              </span>
              {c.summary && <span className="line-clamp-1">{c.summary}</span>}
            </>
          );
          const cls =
            "flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left hover:bg-muted";
          return (
            <li key={c.call_uid}>
              {onOpen ? (
                <button
                  type="button"
                  className={cls}
                  onClick={() => onOpen(c.call_uid)}
                >
                  {line}
                </button>
              ) : (
                <Link
                  className={cls}
                  href={`/calls?tab=history&call=${encodeURIComponent(c.call_uid)}`}
                >
                  {line}
                </Link>
              )}
            </li>
          );
        })}
      </ul>
      {/* total은 지금 연 통화까지 센다 — 링크로 연 통화 기록의 건수와 같다 */}
      {data.total - (exclude ? 1 : 0) > data.calls.length && (
        <Link
          className="text-link hover:underline"
          href={`/calls?tab=history&q=${encodeURIComponent(chartNo)}${
            onOpen && exclude ? `&call=${encodeURIComponent(exclude)}` : ""
          }`}
        >
          통화 기록에서 전체 보기({data.total}건)
        </Link>
      )}
    </div>
  );
}
