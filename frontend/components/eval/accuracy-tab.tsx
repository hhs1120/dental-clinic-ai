"use client";

import { useFetch } from "@/lib/api";
import { fetchAccuracy, type Rates } from "@/lib/eval-api";

const ITEMS: [string, string][] = [
  ["primary_purpose", "목적"],
  ["counterpart", "상대"],
  ["urgency", "급함"],
  ["emergency", "응급"],
  ["complaint", "불만"],
  ["price_inquiry", "비용 문의"],
  ["wants_appt", "예약 희망"],
  ["appt_confirmed", "예약 잡힘"],
  ["has_todo", "할 일 있음"],
];

const pct = (k: number, n: number) => (n ? Math.round((k / n) * 100) : null);

function Bar({ k, n }: { k: number; n: number }) {
  const p = pct(k, n);
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div className="h-full bg-primary" style={{ width: `${p ?? 0}%` }} />
      </div>
      <span className="w-24 shrink-0 text-right text-xs whitespace-nowrap tabular-nums text-muted-foreground">
        {p === null ? "—" : `${p}% (${k}/${n})`}
      </span>
    </div>
  );
}

/** 정확도 표 — 분류기 vs 판정기 일치율(dev 판정 풀)·블라인드 승률·사람이 보는 몫(기능 정의 §4 ② "정확도 표").
 * 골드 지표는 앱 DB에 정답이 없어 eval.py에서만 */
export function AccuracyTab() {
  const { data, error } = useFetch("accuracy", (s) => fetchAccuracy(s));
  if (error) return <p className="text-sm text-muted-foreground">{error}</p>;
  if (!data)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );
  const versions = Object.keys(data.agreement);
  const [a, b] = data.versions.blind ?? ["", ""];
  return (
    <div className="flex flex-col gap-5">
      <p className="text-sm text-muted-foreground">
        분류기 vs 판정기({data.versions.judge ?? "없음"}) 일치율 · 모수는 dev
        판정 풀 · 지금 쓰는 분류 버전 {data.versions.active}. 정답과 비교한
        정확도는 평가 리포트(eval.py)에서만 봅니다.
      </p>
      <div className="grid items-start gap-5 lg:grid-cols-[3fr_2fr]">
        <div className="overflow-x-auto rounded-lg border bg-card p-4">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="pb-2 font-medium">항목</th>
                {versions.map((v) => (
                  <th key={v} className="pb-2 font-medium">
                    {v}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ITEMS.map(([key, label]) => (
                <tr key={key} className="border-t">
                  <td className="py-1.5 pr-3 whitespace-nowrap">{label}</td>
                  {versions.map((v) => (
                    <td key={v} className="min-w-44 py-1.5 pr-3">
                      <Bar {...data.agreement[v][key]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex flex-col gap-4">
          {data.blind && (
            <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
              <h2 className="font-bold">
                블라인드 승률 ({b} vs {a})
              </h2>
              <RateBars title="LLM 쌍 비교" r={data.blind.llm} a={a} b={b} />
              <RateBars title="사람 투표" r={data.blind.human} a={a} b={b} />
              <p className="text-xs text-muted-foreground">
                같은 쌍에서 사람과 LLM이 같은 답 {data.blind.agree.k}/
                {data.blind.agree.n} · LLM은 좌우를 바꿔 두 번 묻고 다르면
                비슷으로 셉니다. 쌍 비교 판정기(Haiku)는 요약을 쓴 모델과 같아
                자기 글 쪽으로 기울 수 있습니다.
              </p>
            </section>
          )}
          <section className="flex flex-col gap-2 rounded-lg border bg-card p-4">
            <h2 className="font-bold">사람이 보는 몫</h2>
            <Bar {...data.human_share} />
            <p className="text-xs text-muted-foreground">
              판정 풀 밖 통화 중 두 AI가 엇갈렸거나 표본으로 뽑힌 통화
            </p>
          </section>
        </div>
      </div>
    </div>
  );
}

function RateBars({
  title,
  r,
  a,
  b,
}: {
  title: string;
  r: Rates;
  a: string;
  b: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-sm font-medium">
        {title} <span className="text-muted-foreground">n = {r.n}</span>
      </p>
      {(
        [
          [`${b}이 나음`, r.b_better],
          [`${a}이 나음`, r.a_better],
          ["비슷", r.tie],
          ["둘 다 틀림", r.both_wrong],
        ] as [string, number][]
      ).map(([l, k]) => (
        <div
          key={l}
          className="grid grid-cols-[6rem_1fr] items-center gap-2 text-sm"
        >
          <span>{l}</span>
          <Bar k={k} n={r.n} />
        </div>
      ))}
      <p className="text-xs text-muted-foreground">
        좌우 선택 — 왼쪽 {r.left} · 오른쪽 {r.right} (쏠림 확인용)
      </p>
    </div>
  );
}
