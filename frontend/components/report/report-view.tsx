"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { ChipRow } from "@/components/chip-row";
import { ErrorBand } from "@/components/prep/prep-view";
import { ScreenTabs } from "@/components/screen-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useFetch } from "@/lib/api";
import { won } from "@/lib/format";
import {
  CATEGORY,
  fetchReport,
  metricTag,
  type Metrics,
  type ReportSheet,
  SCOPE_LABEL,
  type Scope,
} from "@/lib/report-api";
import { cn } from "@/lib/utils";

const SCOPES = Object.keys(SCOPE_LABEL) as Scope[];

/** 기간 라벨 — 월 "2026년 9월"·"10월"(앞 버튼과 연도가 같으면 생략), 주 "11/23 주", 일 "11/24" */
function periodLabel(scope: Scope, p: string, prev?: string) {
  if (scope === "monthly")
    return `${!prev || prev.slice(0, 4) !== p.slice(0, 4) ? `${p.slice(0, 4)}년 ` : ""}${Number(p.slice(5))}월`;
  const md = `${Number(p.slice(5, 7))}/${Number(p.slice(8))}`;
  return scope === "weekly" ? `${md} 주` : md;
}

const pct = (n: number | undefined) =>
  n === undefined ? null : `${n > 0 ? "+" : ""}${n}%`;

function Stat({
  value,
  label,
  sub,
}: {
  value: string;
  label: string;
  sub?: string | null;
}) {
  return (
    <Card className="py-4">
      <CardContent className="flex flex-col gap-1 px-4">
        <div className="text-xl font-bold tabular-nums">{value}</div>
        <div className="text-sm text-muted-foreground">{label}</div>
        {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

/** 비교한 이전 기간 — 월 "10월", 주 "11/16 주", 일 "지난주 같은 요일(11/23)"(백엔드 prev_period) */
function prevLabel(scope: Scope, prev: string) {
  if (scope === "monthly") return `${Number(prev.slice(5))}월`;
  const md = periodLabel("daily", prev);
  return scope === "weekly" ? `${md} 주` : `지난주 같은 요일(${md})`;
}

function Stats({
  m,
  scope,
  prev,
}: {
  m: Metrics;
  scope: Scope;
  prev: string | null;
}) {
  const monthly = scope === "monthly";
  const prevSub = (k: string) =>
    prev && pct(m[`pct.${k}`])
      ? `${prevLabel(scope, prev)} 대비 ${pct(m[`pct.${k}`])}`
      : null;
  // 월간은 6칸을 3칸씩 두 줄, 주간·일간은 4칸 한 줄 — 빈 칸이 남지 않게
  return (
    <>
      <div
        className={cn(
          "grid grid-cols-2 gap-3",
          monthly ? "lg:grid-cols-3" : "lg:grid-cols-4",
        )}
      >
        <Stat
          value={won(m.sales)}
          label="매출(진료 생산)"
          sub={prevSub("sales")}
        />
        <Stat
          value={won(m.collected)}
          label="수금"
          sub={`환자 ${won(m.patient_paid)} · 공단 ${won(m.insurer_paid)}`}
        />
        <Stat
          value={`${m.noshow_rate}%`}
          label="노쇼율"
          sub={`${m.noshow}/${m.visits}건`}
        />
        <Stat
          value={`${m.new_patients}명`}
          label="신환"
          sub={prevSub("new_patients")}
        />
        {monthly && (
          <>
            <Stat
              value={won(m.material)}
              label="재료비"
              sub={[
                `기공 ${won(m.lab)}`,
                m.material_unconfirmed > 0 &&
                  `미확정 ${m.material_unconfirmed}건 포함`,
                m.account_none > 0 &&
                  `과목 없는 출금 ${won(m.account_none_amount)} 제외`,
              ]
                .filter(Boolean)
                .join(" · ")}
            />
            <Stat
              value={won(m.profit_ex_labor)}
              label="인건비 제외 손익"
              sub="수금 − 출금 · 인건비는 병원 기록에 없어 빠짐"
            />
          </>
        )}
      </div>
      {!monthly && (
        <p className="text-sm text-muted-foreground">
          재료비·손익은 월말 이체 비중이 커서 월간에서만 봅니다. 공단 지급은
          심사 결과 통보일에 잡힙니다.
        </p>
      )}
    </>
  );
}

function Interpretation({ sheet }: { sheet: ReportSheet }) {
  const it = sheet.interpretation;
  return (
    <Card className="border-primary py-4">
      <CardContent className="flex flex-col gap-3 px-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="kind">AI</Badge>
          <h2 className="text-lg font-medium tracking-tight">
            AI가 고른 주목할 점
          </h2>
          <span className="text-xs text-muted-foreground">
            문장의 숫자는 점마다 근거 보기의 값에서 — 후검증을 통과한 해석만
            보입니다
          </span>
          {it?.stale && (
            <Badge variant="warn">
              해석 이후 숫자가 바뀜 — 다시 만들기 필요
            </Badge>
          )}
        </div>
        {!it ? (
          <p className="text-sm text-muted-foreground">
            해석 없음 — 아래 숫자는 그대로 볼 수 있습니다
          </p>
        ) : it.points.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            이전 기간과 비교해 눈에 띄는 변화가 없습니다
          </p>
        ) : (
          <ol className="flex flex-col gap-3">
            {it.points.map((p, k) => (
              <li key={k} className="flex flex-col gap-1">
                <p>{p.text}</p>
                <details className="text-xs text-muted-foreground">
                  <summary className="w-fit cursor-pointer">근거 보기</summary>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {p.used.map((u) => (
                      <span
                        key={u}
                        className="rounded-sm bg-muted px-1.5 py-0.5"
                      >
                        {metricTag(u, it.values[u] ?? null)}
                      </span>
                    ))}
                  </div>
                </details>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

function DxCard({ sheet }: { sheet: ReportSheet }) {
  const dx = sheet.dx_top;
  if (sheet.scope !== "monthly") return null;
  return (
    // 한 장뿐이라 전체 너비에 가로로 — 넓은 화면에서 반쪽 카드 옆 빈칸이 남지 않게
    <Card className="py-4">
      <CardContent className="flex flex-col gap-2 px-4 lg:flex-row lg:items-center lg:gap-6">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-medium tracking-tight">상병 착오</h2>
          <span className="text-xs text-muted-foreground">재청구 불가</span>
        </div>
        {dx ? (
          <div className="flex flex-col gap-1">
            <p className="text-sm">
              {sheet.metrics.dx_errors}건 · 못 받은 금액{" "}
              {won(sheet.metrics.dx_lost)}
            </p>
            <p className="text-xs text-muted-foreground">
              금액이 가장 큰 조합: {dx.name} {dx.n}건 · {won(dx.lost)}
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">이 달 상병 착오 없음</p>
        )}
        <Button
          asChild
          size="sm"
          variant="outline"
          className="w-fit lg:ml-auto"
        >
          <Link href="/claims?tab=pre">청구 전 점검 보기</Link>
        </Button>
      </CardContent>
    </Card>
  );
}

// 시술별 표(기능 정의 §5 ⑥) — 왼쪽 매출 비중은 위 SHARE_TOP개만, 오른쪽 변화는 작으면 한 줄로 묶는다
const SHARE_TOP = 5;
const SMALL_CHANGE_WON = 100_000; // 매출 증감이 이보다 작거나
const SMALL_CHANGE_PCT = 1; // 증감률(%, 반올림 전)이 이보다 작으면 "변화 작음" — 매출이 없는 분류(상담)는 건수가 그대로일 때만

/** 정수 비중 — 합이 100이 되게 소수점이 큰 쪽부터 1씩 더한다(최대 나머지) */
function shares(values: number[]): number[] {
  const total = values.reduce((n, v) => n + v, 0);
  if (!total) return values.map(() => 0);
  const raw = values.map((v) => (v * 100) / total);
  const out = raw.map(Math.floor);
  const order = raw
    .map((r, i) => [r - Math.floor(r), i] as const)
    .sort((a, b) => b[0] - a[0]);
  const need = 100 - out.reduce((n, v) => n + v, 0);
  for (let k = 0; k < need; k++) out[order[k][1]] += 1;
  return out;
}

const MINUS = "−";
const sign = (n: number) => (n > 0 ? "+" : n < 0 ? MINUS : "");

function ProcedureCard({ sheet }: { sheet: ReportSheet }) {
  const rows = sheet.procedures.filter((r) => r.sales > 0);
  const top = rows.slice(0, SHARE_TOP);
  const rest = rows.slice(SHARE_TOP);
  const restSales = rest.reduce((n, r) => n + r.sales, 0);
  const restCount = rest.reduce((n, r) => n + r.count, 0);
  const pcts = shares([...top.map((r) => r.sales), restSales]);
  const max = top[0]?.sales ?? 0;
  const prev = sheet.prev_period;
  const changes = sheet.procedures.map((r) => {
    const prevSales = r.prev_sales ?? 0;
    const diff = r.sales - prevSales;
    const countDiff = r.count - (r.prev_count ?? 0);
    // 증감률은 백엔드 metrics 값 그대로 — AI 해석 근거와 같은 숫자(이전 매출 0이면 없음)
    const pct: number | undefined = sheet.metrics[`pct.sales.${r.category}`];
    const small =
      r.sales === 0 && prevSales === 0
        ? countDiff === 0
        : Math.abs(diff) < SMALL_CHANGE_WON ||
          (prevSales > 0 &&
            Math.abs((diff * 100) / prevSales) < SMALL_CHANGE_PCT);
    return { ...r, diff, countDiff, pct, prevSales, small };
  });
  // 늘어난 것 큰 순 → 줄어든 것 큰 순(매출 그대로·건수만 바뀐 분류는 끝)
  const moved = changes
    .filter((c) => !c.small)
    .sort((a, b) =>
      a.diff > 0 === b.diff > 0
        ? Math.abs(b.diff) - Math.abs(a.diff)
        : b.diff - a.diff,
    );
  const quiet = changes.filter((c) => c.small);
  const name = (c: string) => CATEGORY[c] ?? c;
  const money = (n: number) => `${sign(n)}${won(Math.abs(n))}`;
  const tone = (n: number) =>
    n > 0
      ? "text-status-ok-fg"
      : n < 0
        ? "text-status-danger-fg"
        : "text-muted-foreground";
  const salesChange = (c: (typeof changes)[number]) =>
    c.sales === 0 && c.prevSales === 0
      ? "매출 없음"
      : c.prevSales === 0
        ? `${money(c.diff)} (새로)`
        : `${money(c.diff)}${c.pct !== undefined ? ` (${sign(c.pct)}${Math.abs(c.pct)}%)` : ""}`;

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card className="py-4">
        <CardContent className="flex flex-col gap-2 px-4">
          <h2 className="text-lg font-medium tracking-tight">매출 비중</h2>
          {top.length === 0 ? (
            <p className="text-sm text-muted-foreground">이 기간 매출 없음</p>
          ) : (
            <ul
              className="flex flex-col gap-1.5 text-sm"
              aria-label="매출 비중"
            >
              {top.map((r, i) => (
                <li
                  key={r.category}
                  className="grid grid-cols-[5rem_1fr_auto] items-center gap-2"
                >
                  <span>{name(r.category)}</span>
                  <span
                    className="h-2.5 rounded-sm bg-primary"
                    style={{ width: `${max ? (r.sales * 100) / max : 0}%` }}
                  />
                  <span className="tabular-nums">
                    {won(r.sales)} · {pcts[i]}% · {r.count}건
                  </span>
                </li>
              ))}
              {rest.length > 0 && (
                <li className="grid grid-cols-[5rem_1fr_auto] gap-2 text-muted-foreground">
                  <span>나머지 {rest.length}</span>
                  <span />
                  <span className="tabular-nums">
                    {won(restSales)} · {pcts[top.length]}% · {restCount}건
                  </span>
                </li>
              )}
            </ul>
          )}
        </CardContent>
      </Card>
      <Card className="py-4">
        <CardContent className="flex flex-col gap-2 px-4">
          <h2 className="text-lg font-medium tracking-tight">
            {prev ? `${prevLabel(sheet.scope, prev)} 대비` : "이전 기간 대비"}
          </h2>
          {!prev ? (
            <p className="text-sm text-muted-foreground">비교 기간 없음</p>
          ) : changes.length === 0 ? (
            <p className="text-sm text-muted-foreground">비교할 진료 없음</p>
          ) : (
            <ul
              className="flex flex-col gap-1.5 text-sm"
              aria-label="이전 기간 대비"
            >
              {moved.map((c) => (
                <li
                  key={c.category}
                  className="grid grid-cols-[5rem_1fr_auto] items-center gap-2"
                >
                  <span>{name(c.category)}</span>
                  <span className={cn("tabular-nums", tone(c.diff))}>
                    {salesChange(c)}
                  </span>
                  <span className={cn("tabular-nums", tone(c.countDiff))}>
                    {sign(c.countDiff)}
                    {Math.abs(c.countDiff)}건
                  </span>
                </li>
              ))}
              {quiet.length > 0 && (
                <li className="text-muted-foreground">
                  {quiet.map((c) => name(c.category)).join("·")} — 변화 작음
                </li>
              )}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Trend({
  sheet,
  onPick,
}: {
  sheet: ReportSheet;
  onPick: (p: string) => void;
}) {
  const monthly = sheet.scope === "monthly";
  const cell = (v: number | null | undefined) =>
    v === null || v === undefined ? "—" : v.toLocaleString("ko-KR");
  const rows = [...sheet.trend].reverse();
  return (
    <div className="scroll-shadow-x overflow-x-auto rounded-xl border">
      <table className="w-full min-w-2xl text-sm whitespace-nowrap tabular-nums">
        <thead className="bg-muted whitespace-nowrap text-muted-foreground">
          <tr>
            <th className="px-3 py-2 text-left">기간</th>
            <th className="px-3 py-2 text-right">매출</th>
            <th className="px-3 py-2 text-right">수금</th>
            <th className="px-3 py-2 text-right">노쇼율</th>
            <th className="px-3 py-2 text-right">신환</th>
            {monthly && (
              <>
                <th className="px-3 py-2 text-right">재료비</th>
                <th className="px-3 py-2 text-right">인건비 제외 손익</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const i = sheet.periods.indexOf(r.period);
            return (
              <tr
                key={r.period}
                onClick={() => onPick(r.period)}
                aria-selected={r.period === sheet.period}
                className="cursor-pointer border-t hover:bg-muted aria-selected:bg-accent"
              >
                <td className="px-3 py-2 whitespace-nowrap">
                  {periodLabel(sheet.scope, r.period, sheet.periods[i - 1])}
                </td>
                <td className="px-3 py-2 text-right">{cell(r.sales)}</td>
                <td className="px-3 py-2 text-right">{cell(r.collected)}</td>
                <td className="px-3 py-2 text-right">{r.noshow_rate}%</td>
                <td className="px-3 py-2 text-right">{cell(r.new_patients)}</td>
                {monthly && (
                  <>
                    <td className="px-3 py-2 text-right">{cell(r.material)}</td>
                    <td className="px-3 py-2 text-right">
                      {cell(r.profit_ex_labor)}
                    </td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ReportView() {
  const params = useSearchParams();
  const router = useRouter();
  const raw = params.get("scope");
  const scope: Scope = SCOPES.includes(raw as Scope)
    ? (raw as Scope)
    : "monthly";
  const period = params.get("period");
  const { data: sheet, error } = useFetch(`${scope}|${period ?? ""}`, (s) =>
    fetchReport(scope, period, s),
  );

  const go = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    router.replace(`/report${next.size ? `?${next}` : ""}`);
  };

  return (
    <div className="flex flex-col gap-4">
      <ScreenTabs
        tabs={SCOPES.map((s) => ({ key: s, label: SCOPE_LABEL[s] }))}
        value={scope}
        // 기간 종류마다 기간 목록이 달라 기간을 비운다
        onChange={(v) =>
          go({ scope: v === "monthly" ? null : v, period: null })
        }
      />
      {error && <ErrorBand text={error} />}
      {!sheet ? (
        !error && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            불러오는 중…
          </p>
        )
      ) : (
        <>
          <ChipRow label="기간" selected={sheet.period}>
            {sheet.periods.map((p, k) => (
              <Button
                key={p}
                size="sm"
                variant={p === sheet.period ? "secondary" : "outline"}
                aria-pressed={p === sheet.period}
                onClick={() => go({ period: p })}
              >
                {periodLabel(sheet.scope, p, sheet.periods[k - 1])}
              </Button>
            ))}
          </ChipRow>
          <Interpretation sheet={sheet} />
          <Stats
            m={sheet.metrics}
            scope={sheet.scope}
            prev={sheet.prev_period}
          />
          <ProcedureCard sheet={sheet} />
          <DxCard sheet={sheet} />
          <Trend sheet={sheet} onPick={(p) => go({ period: p })} />
        </>
      )}
    </div>
  );
}
