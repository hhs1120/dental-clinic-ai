"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useFetch } from "@/lib/api";
import { won } from "@/lib/format";
import {
  fetchReport,
  isEst,
  type Metrics,
  type ReportSheet,
  ROLE_LABEL,
  SCOPE_LABEL,
  type Scope,
} from "@/lib/report-api";

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

function Est() {
  return <Badge variant="warn">[추정]</Badge>;
}

function Stat({
  value,
  label,
  sub,
  est = false,
}: {
  value: string;
  label: string;
  sub?: string | null;
  est?: boolean;
}) {
  return (
    <Card data-testid={est ? "stat-est" : undefined} className="py-4">
      <CardContent className="flex flex-col gap-1 px-4">
        <div className="flex items-center gap-1.5 text-xl font-bold tabular-nums">
          {est && <Est />}
          {value}
        </div>
        <div className="text-sm text-muted-foreground">{label}</div>
        {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function Stats({ m, monthly }: { m: Metrics; monthly: boolean }) {
  const prevSub = (k: string) =>
    pct(m[`pct.${k}`]) && `이전 대비 ${pct(m[`pct.${k}`])}`;
  return (
    <>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
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
      </div>
      {monthly ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
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
            sub="수금 − 출금"
          />
          <Stat
            est
            value={won(m["est.profit"])}
            label="추정 손익"
            sub={`인건비 ${won(m["est.labor"])} 반영 · 대표 원장 보수·세금 전`}
          />
          <Stat
            est
            value={m["est.bep"] === undefined ? "—" : won(m["est.bep"])}
            label="손익분기 매출"
            sub="대표 원장 보수·세금 전"
          />
        </div>
      ) : (
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
    <Card className="py-4">
      <CardContent className="flex flex-col gap-3 px-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-lg font-bold">주목할 점</h2>
          <Badge variant="kind">AI</Badge>
          <span className="text-xs text-muted-foreground">
            문장의 숫자는 점마다 아래 꼬리표 값에서 — 후검증을 통과한 해석만
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
            해석 없음 — 숫자는 위와 같습니다
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
                <div className="flex flex-wrap gap-1">
                  {p.used.map((u) => (
                    <code
                      key={u}
                      className="rounded-sm bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                    >
                      {u} {it.values[u]?.toLocaleString("ko-KR") ?? "—"}
                    </code>
                  ))}
                </div>
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

function MonthlyCards({ sheet }: { sheet: ReportSheet }) {
  const { labor, dx_top: dx } = sheet;
  if (!labor) return null;
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card className="py-4">
        <CardContent className="flex flex-col gap-2 px-4">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-bold">인건비 가정값</h2>
            <Badge variant="warn">가정값</Badge>
          </div>
          <p className="text-sm">
            {Object.entries(labor.wage)
              .map(
                ([r, v]) =>
                  `${ROLE_LABEL[r] ?? r} ${labor.headcount[r] ?? 0}명 × ${won(v)}`,
              )
              .join(" · ")}
          </p>
          <p className="text-xs text-muted-foreground">
            실제 급여가 아닙니다. 대표 원장 보수는 넣지 않았고, 값은 앱
            설정(ax_settings)에서 바꿉니다.
          </p>
        </CardContent>
      </Card>
      <Card className="py-4">
        <CardContent className="flex flex-col gap-2 px-4">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-bold">상병 착오</h2>
            <span className="text-xs text-muted-foreground">재청구 불가</span>
          </div>
          {dx ? (
            <>
              <p className="text-sm">
                {sheet.metrics.dx_errors}건 · 못 받은 금액{" "}
                {won(sheet.metrics.dx_lost)}
              </p>
              <p className="text-xs text-muted-foreground">
                금액이 가장 큰 조합: {dx.name} {dx.n}건 · {won(dx.lost)}
              </p>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              이 달 상병 착오 없음
            </p>
          )}
          <Button asChild size="sm" variant="outline" className="w-fit">
            <Link href="/claims?tab=pre">청구 전 점검 보기</Link>
          </Button>
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
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm tabular-nums">
        <thead className="bg-muted text-muted-foreground">
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
                <th className="px-3 py-2 text-right">추정 손익 [추정]</th>
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
                <td className="px-3 py-2">
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
                    <td className="px-3 py-2 text-right">
                      {cell(r["est.profit"])}
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
      <Tabs
        value={scope}
        // 기간 종류마다 기간 목록이 달라 기간을 비운다
        onValueChange={(v) =>
          go({ scope: v === "monthly" ? null : v, period: null })
        }
      >
        <TabsList variant="line" className="w-full justify-start border-b">
          {SCOPES.map((s) => (
            <TabsTrigger key={s} value={s} className="flex-none px-4">
              {SCOPE_LABEL[s]}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
      {error && <ErrorBand text={error} />}
      {!sheet ? (
        !error && (
          <p className="py-10 text-center text-sm text-muted-foreground">
            불러오는 중…
          </p>
        )
      ) : (
        <>
          <div
            className="flex flex-wrap gap-1.5"
            role="group"
            aria-label="기간"
          >
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
          </div>
          <Stats m={sheet.metrics} monthly={sheet.scope === "monthly"} />
          <Interpretation sheet={sheet} />
          <MonthlyCards sheet={sheet} />
          <Trend sheet={sheet} onPick={(p) => go({ period: p })} />
          {Object.keys(sheet.metrics).some(isEst) && (
            <p className="text-xs text-muted-foreground">
              [추정] 값은 인건비 가정값으로 계산했고 성과 지표로 쓰지 않습니다.
            </p>
          )}
        </>
      )}
    </div>
  );
}
