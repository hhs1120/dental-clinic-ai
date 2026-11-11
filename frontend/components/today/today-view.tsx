"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ApiError, errorText, useFetch } from "@/lib/api";
import { revertAuto, shortAt, useLabels } from "@/lib/calls-api";
import { readPart } from "@/lib/part";
import {
  fetchToday,
  monthDay,
  reviewText,
  type TodaySheet,
} from "@/lib/today-api";

// 이미 다시 연 건(다른 창에서 먼저 누름) — 오류가 아니라 다시 조회만 한다(backend/api.py revert_auto 409)

/** 오늘 화면 — 요약 카드 → 사람이 볼 일 → AI가 한 일(기능 정의 §4 오늘 화면). 보여 주고 보내기만 하고, 예외는 다시 열기 */
export function TodayView() {
  const today = useFetch("today", (s) => fetchToday(s));
  if (today.error)
    return (
      <p className="rounded-lg border border-status-danger-border bg-status-danger-bg px-3 py-2 text-sm text-status-danger-fg">
        {today.error}
      </p>
    );
  if (!today.data)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );
  return (
    <div className="flex flex-col gap-6">
      <Cards sheet={today.data} />
      <People sheet={today.data} />
      <AiDone sheet={today.data} reload={today.reload} />
    </div>
  );
}

function Cards({ sheet }: { sheet: TodaySheet }) {
  const c = sheet.cards;
  const a = c.appointments;
  const cards = [
    {
      href: "/prep",
      value: `${a.total}명`,
      label: "오늘 예약",
      sub: `진료 ${a.treatment} · 위생 ${a.hygiene} · 상담 ${a.consult}`,
    },
    { href: "/prep", value: `${c.new_patients}명`, label: "신환" },
    {
      href: "/prep?chip=alert",
      value: `${c.alert_patients}명`,
      label: "주의 환자",
    },
    {
      href: "/calls",
      value: `${c.todo.items}건`,
      label: "오늘 할 일",
      // 급함은 0이 아니면 빨간 배지로 — 숫자 옆 작은 글씨로 두면 놓친다
      sub:
        c.todo.urgent_items > 0 ? (
          <Badge variant="danger">급함 {c.todo.urgent_items}</Badge>
        ) : (
          "급함 없음"
        ),
    },
  ];
  // 이름 → 숫자 → 세부 순 — 숫자부터 보면 무슨 숫자인지 다시 읽어야 한다
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {cards.map((k) => (
        <Link key={k.label} href={k.href} className="group rounded-xl">
          <Card className="h-full gap-1 px-4 shadow-card group-hover:ring-primary">
            <div className="text-sm font-semibold">{k.label}</div>
            <div className="text-3xl font-bold tabular-nums">{k.value}</div>
            {k.sub && (
              <div className="text-sm text-muted-foreground">{k.sub}</div>
            )}
          </Card>
        </Link>
      ))}
    </div>
  );
}

/** 오늘 화면 미예약 환자 줄 — 숫자 없는 고정 링크라 0건 숨김 없이 늘 보인다(기능 정의 §4 오늘 화면) */
export const MISSED_LINK_TEXT = "미예약 환자 · 다시 오실 만한 분께 연락하기";

type Tone = "danger" | "warn" | "info" | "muted";
type Line = {
  key: string;
  tag: string;
  tone: Tone;
  href: string;
  title: string;
  meta?: string;
};

function People({ sheet }: { sheet: TodaySheet }) {
  const names = useLabels();
  const p = sheet.people;
  // 급한 것부터. 0건인 줄은 숨긴다(목록 줄·숫자 줄 모두, 예외: 미예약 환자 고정 링크)
  const lines: Line[] = [
    ...p.urgent_calls.map((u) => ({
      key: `u-${u.call_uid}`,
      tag: "급한 통화",
      tone: "danger" as const,
      href: `/calls?call=${encodeURIComponent(u.call_uid)}`,
      title: `${u.name ?? "이름 없음"} · ${u.text}`,
      meta: `${monthDay(u.at)}${u.emergency ? " · 응급" : ""}`,
    })),
    ...p.review_calls.map((r) => ({
      key: `r-${r.call_uid}`,
      tag: "확인할 통화",
      tone: "warn" as const,
      href: `/calls?call=${encodeURIComponent(r.call_uid)}`,
      title: `${r.name ?? "이름 없음"} · ${reviewText(r, names)} · ${monthDay(r.at)}`,
      meta: "통화 상세에서 확인함을 누르면 빠집니다",
    })),
  ];
  const count = (
    n: number,
    key: string,
    tag: string,
    tone: Tone,
    href: string,
    title: string,
    meta: string,
  ) => n > 0 && lines.push({ key, tag, tone, href, title, meta });
  count(
    p.unconfirmed,
    "unconfirmed",
    "내원 확인 전",
    "warn",
    "/prep?chip=unconfirmed",
    `내원 확인 전 ${p.unconfirmed}명 — 확인 전화`,
    "준비표에서 내원 확인 전 칩으로 보기",
  );
  count(
    p.alert_patients,
    "alert",
    "주의 환자",
    "info",
    "/prep?chip=alert",
    `주의 환자 ${p.alert_patients}명`,
    "준비표에서 주의 칩으로 보기",
  );
  count(
    p.todo_left.items,
    "todo",
    "남은 할 일",
    "warn",
    "/calls",
    `통화 할 일 ${p.todo_left.items}건 · 통화 ${p.todo_left.calls}건`,
    "급함 먼저 · 끝내면 빠짐",
  );
  count(
    p.appeal_soon,
    "appeal",
    "재심 기한",
    "danger",
    "/claims?chip=soon",
    `재심 신청 기한 임박 ${p.appeal_soon}건`,
    "30일 이내 · 상병 착오 제외",
  );
  count(
    p.preclaim_warning,
    "preclaim",
    "청구 전 경고",
    "warn",
    "/claims?tab=pre",
    `청구 전 상병 경고 ${p.preclaim_warning}건`,
    "청구 점검에서 허용 상병 밖 줄 보기",
  );
  count(
    p.preclaim_ai,
    "preclaim-ai",
    "청구 전 AI",
    "warn",
    "/claims?tab=pre&chip=ai",
    `청구 전 차트에 요건 근거 못 찾음 ${p.preclaim_ai}건(AI)`,
    "청구 점검에서 AI 근거 문장 보기",
  );
  count(
    p.judge_pending,
    "judge",
    "판정 대기",
    "muted",
    "/eval",
    `엇갈림·표본 판정 ${p.judge_pending}건`,
    "AI 평가 판정 탭",
  );
  for (const q of p.refused)
    lines.push({
      key: `q-${q.id}`,
      tag: "근거 없음",
      tone: "muted",
      href: `/ask?${new URLSearchParams({ tab: "chat", draft: q.question })}`,
      title: `“${q.question}”`,
      meta: `${shortAt(q.at)} · 문서에서 근거를 못 찾은 질문`,
    });

  // 미예약 환자는 숫자 없는 고정 링크 — 세지 않으므로 늘 맨 끝에 둔다
  const missedLine: Line = {
    key: "missed",
    tag: "미예약 환자",
    tone: "info",
    href: "/missed",
    title: MISSED_LINK_TEXT,
    meta: "미예약 환자 화면에서 연락 결과 남기기",
  };

  return (
    <section aria-label="사람이 볼 일" className="flex flex-col gap-2">
      <h2 className="text-lg font-bold">사람이 볼 일</h2>
      {lines.length === 0 && (
        <p className="rounded-lg border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
          오늘 남은 일이 없습니다
        </p>
      )}
      <ul className="flex flex-col divide-y rounded-xl border bg-card">
        {[...lines, missedLine].map((l) => (
          <li key={l.key} className="flex items-start gap-3 px-4 py-3">
            <Tag tone={l.tone}>{l.tag}</Tag>
            <Link href={l.href} className="min-w-0 flex-1 hover:underline">
              <div className="font-medium">{l.title}</div>
              {l.meta && (
                <div className="text-xs text-muted-foreground">{l.meta}</div>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Tag({ tone, children }: { tone: Tone; children: string }) {
  return (
    <span className="w-28 shrink-0">
      <Badge variant={tone}>{children}</Badge>
    </span>
  );
}

function AiDone({ sheet, reload }: { sheet: TodaySheet; reload: () => void }) {
  const ai = sheet.ai;
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [fail, setFail] = useState<string | null>(null);
  const failed = ai.prep.failed + ai.classify_failed + ai.followup_failed;
  const summary = [
    ai.autoclose_on ? `자동 처리 ${ai.auto_total}건` : "자동 종결 꺼짐",
    `오늘 준비표 ${ai.prep.rows}행`,
    `실패 ${failed}`,
  ].join(" · ");
  const reopen = async (id: number) => {
    setBusy((b) => new Set(b).add(id)); // 누르는 즉시 꺼서 두 번 보내지 않는다
    setFail(null);
    try {
      await revertAuto(id, readPart());
    } catch (e) {
      // 409 = 다른 사람이 먼저 다시 엶 — 오류 없이 다시 조회만
      if (!(e instanceof ApiError && e.status === 409)) {
        setFail(errorText(e));
        setBusy((b) => new Set([...b].filter((x) => x !== id)));
      }
    }
    reload();
  };
  const results = [
    {
      href: "/prep",
      title: `오늘 준비표 ${ai.prep.rows}행 · 제안 ${ai.prep.suggest} · 후검증 실패 ${ai.prep.failed}`,
      bad: ai.prep.failed,
    },
    {
      href: "/calls?tab=history",
      title: `통화 분류 실패 ${ai.classify_failed}건`,
      bad: ai.classify_failed,
    },
    {
      href: "/calls?tab=history",
      title: `처리안 실패 ${ai.followup_failed}건`,
      bad: ai.followup_failed,
    },
  ];

  return (
    <section aria-label="AI가 한 일" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-bold">AI가 한 일</h2>
        <span className="text-sm text-muted-foreground">{summary}</span>
        <span className="flex-1" />
        <Button
          variant="outline"
          size="sm"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "접기" : "펼치기"}
        </Button>
      </div>
      {fail && (
        <p className="rounded-lg border border-status-danger-border bg-status-danger-bg px-3 py-2 text-sm text-status-danger-fg">
          {fail}
        </p>
      )}
      {open && (
        <div className="flex flex-col gap-4 rounded-xl border bg-card p-4">
          <div className="flex flex-col gap-2">
            <h3 className="font-bold">자동 처리</h3>
            {!ai.autoclose_on ? (
              <p className="text-sm text-muted-foreground">
                자동 종결이 꺼져 있습니다
              </p>
            ) : ai.auto_recent.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                자동으로 닫은 통화가 없습니다
              </p>
            ) : (
              <ul className="flex flex-col divide-y">
                {ai.auto_recent.map((x) => (
                  <li key={x.id} className="flex items-center gap-3 py-2">
                    <Link
                      href={`/calls?call=${encodeURIComponent(x.call_uid)}`}
                      className="min-w-0 flex-1 hover:underline"
                    >
                      <div className="font-medium">
                        {x.name ?? "이름 없음"} · 자동 종결
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {x.reason} · {shortAt(x.at)}
                      </div>
                    </Link>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy.has(x.id)}
                      onClick={() => reopen(x.id)}
                    >
                      다시 열기
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            {ai.autoclose_on && ai.auto_total > 0 && (
              <Link
                href="/calls?tab=history"
                className="text-sm text-link hover:underline"
              >
                전체 보기 → 통화 기록 ({ai.auto_total}건)
              </Link>
            )}
          </div>
          <div className="flex flex-col gap-2">
            <h3 className="font-bold">결과 표에서 센 것</h3>
            <ul className="flex flex-col divide-y">
              {results.map((r) => (
                <li key={r.title} className="flex items-center gap-3 py-2">
                  <span className="w-28 shrink-0">
                    {r.bad > 0 ? (
                      <Badge variant="warn">사람에게 넘김</Badge>
                    ) : (
                      <Badge variant="ok">끝남</Badge>
                    )}
                  </span>
                  <Link
                    href={r.href}
                    className="min-w-0 flex-1 hover:underline"
                  >
                    {r.title}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </section>
  );
}
