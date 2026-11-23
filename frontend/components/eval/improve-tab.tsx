"use client";

import { Badge } from "@/components/ui/badge";
import { useFetch } from "@/lib/api";
import { fetchImproveRuns } from "@/lib/eval-api";

// 못 넘은 조건 이름(evaluation/prompt_gate.py decide·auto_conditions, evaluation/improve.py 검사)
const CONDITIONS: Record<string, string> = {
  target_up: "목표 항목 오름 미달",
  no_new_miss: "응급·불만 새로 놓침",
  false_alarm_cap: "응급·불만 오탐 증가",
  no_regression: "다른 항목 오답 증가",
  autoclose_error: "자동 종결 오류율 초과",
  trust_unmeasured: "입력 신뢰 경계 미측정",
  trust_violation: "입력 신뢰 경계 위반",
  scope: "규칙 줄 밖 변경",
  example: "예시 문장 삽입",
  copy: "재료 문장 베낌",
  leak: "기록 글이 재료와 겹침",
  no_rule: "후보를 내지 못함",
};

/** 자동 개선 — 개선 에이전트가 쓴 후보와 관문 결과(읽기 전용, 기능 정의 §4 ② "자동 개선"). 기준 수정·되돌리기는
 * 화면에 두지 않는다 */
export function ImproveTab() {
  const { data, error } = useFetch("improve-runs", (s) => fetchImproveRuns(s));
  if (error) return <p className="text-sm text-muted-foreground">{error}</p>;
  if (!data)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        개선 에이전트가 재료 구간의 오류로 분류 프롬프트 후보를 쓰고, 관문이
        검증 구간에서 거릅니다. 지금 쓰는 분류 버전 {data.active}.
      </p>
      {data.pending && (
        <p className="rounded-xl border bg-card p-3 text-sm">
          <Badge variant="kind">{data.pending}</Badge> 대기 중 — 사람이
          재분류(make classify V={data.pending})를 끝내면 전환됩니다.
        </p>
      )}
      {data.runs.length === 0 ? (
        <p className="py-10 text-center text-sm text-muted-foreground">
          아직 자동 개선을 돌린 기록이 없습니다.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border bg-card p-4">
          <table className="w-full min-w-4xl text-sm">
            <thead>
              <tr className="text-left whitespace-nowrap text-muted-foreground">
                <th className="pb-2 pr-3 font-medium">시각</th>
                <th className="pb-2 pr-3 font-medium">후보</th>
                <th className="pb-2 pr-3 font-medium">오류 유형</th>
                <th className="pb-2 pr-3 font-medium">바꾼 규칙</th>
                <th className="pb-2 pr-3 font-medium">관문 결과</th>
                <th className="pb-2 font-medium">반영</th>
              </tr>
            </thead>
            <tbody>
              {data.runs.map((r) => (
                <tr key={r.candidate} className="border-t align-top">
                  <td className="py-2 pr-3 whitespace-nowrap tabular-nums">
                    {r.at.replace("T", " ").slice(0, 16)}
                  </td>
                  <td className="py-2 pr-3 whitespace-nowrap">
                    {r.candidate}
                    <span className="block text-xs text-muted-foreground">
                      기준 {r.base}
                    </span>
                  </td>
                  <td className="min-w-40 py-2 pr-3">
                    {r.error_types
                      ? r.error_types.map((t) => (
                          <span key={t.name} className="block">
                            {t.name} ({t.n})
                          </span>
                        ))
                      : "—"}
                  </td>
                  <td className="min-w-72 py-2 pr-3">
                    {r.changed_rule ?? "—"}
                  </td>
                  <td className="min-w-44 py-2 pr-3">
                    {r.adopted
                      ? "합격"
                      : r.failed.map((f) => (
                          <span key={f} className="block">
                            {CONDITIONS[f] ?? f}
                          </span>
                        ))}
                  </td>
                  <td className="py-2 whitespace-nowrap">
                    <Badge variant={r.adopted ? "kind" : "muted"}>
                      {r.adopted ? "대기 버전" : "탈락"}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
