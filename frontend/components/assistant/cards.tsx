"use client";

import Link from "next/link";
import { useState } from "react";

import { ResultCard } from "@/components/ask/result-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card as Box } from "@/components/ui/card";
import {
  approveDraft,
  cancelDraft,
  type Card,
  type DraftData,
} from "@/lib/assistant-api";
import { ApiError, errorText } from "@/lib/api";
import { readPart } from "@/lib/part";

/** 승인 검사 코드(backend/emr_bridge.py)의 짧은 말 */
const REASON: Record<string, string> = {
  closed_day: "휴진일입니다",
  outside_hours: "영업시간 밖입니다",
  lunch: "점심시간과 겹칩니다",
  before_today: "이미 지난 시각입니다",
  wrong_role: "그 시술을 맡을 수 없는 담당입니다",
  staff_busy: "담당이 그 시간에 다른 예약이 있습니다",
  no_chair: "빈 의자가 없습니다",
  patient_same_day: "환자가 그날 이미 예약이 있습니다",
  unknown_patient: "없는 환자입니다",
  off_slot: "예약 칸에 맞지 않는 시각입니다",
};

const hhmm = (s: string) => s.slice(11, 16);
const endTime = (d: DraftData) => {
  const [h, m] = hhmm(d.scheduled_at).split(":").map(Number);
  const t = h * 60 + m + d.duration_min;
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
};

const linkClass = "text-sm font-medium text-link hover:underline";

/** 예약 초안 카드 — 승인 때 사람의 파트가 기록된다(AI 비서 설계 §3.2). 한 번 처리된 초안과 지운 대화(읽기 전용, §6)의
 * 초안은 버튼이 없다 */
function DraftCard({
  initial,
  onRetry,
  readOnly,
}: {
  initial: DraftData;
  onRetry: () => void;
  readOnly: boolean;
}) {
  const [d, setD] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const take = (card: Card) => {
    if (card.type === "draft") setD(card.data);
  };
  const run = async (act: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await act();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "이미 처리된 초안입니다 — 대화를 다시 열어 주세요"
          : errorText(e),
      );
    } finally {
      setBusy(false);
    }
  };
  const date = d.scheduled_at.slice(0, 10);
  const reasons = (d.reason ?? "").split(",").filter(Boolean);
  return (
    <Box size="sm" className="gap-2 px-4">
      <div className="flex flex-wrap items-center gap-2">
        {d.status === "approved" ? (
          <Badge variant="ok">확정</Badge>
        ) : d.status === "cancelled" ? (
          <Badge variant="muted">취소됨</Badge>
        ) : d.status === "rejected" ? (
          <Badge variant="danger">승인 못 함</Badge>
        ) : (
          <Badge variant="kind">AI</Badge>
        )}
        <strong>
          {date} {hhmm(d.scheduled_at)}–{endTime(d)}
        </strong>
        <span>
          {d.staff} · {d.procedure ?? d.type}
        </span>
      </div>
      <p>
        {d.name} ({d.chart_no}){d.chair_no ? ` · 의자 ${d.chair_no}` : ""}
      </p>
      {d.status === "draft" && (
        <p className="text-muted-foreground">
          겹침 없음 · 영업시간 안
          {d.source_call_uid && (
            <>
              {" · 근거 통화 "}
              {d.source_call_pool ? (
                d.source_call_uid
              ) : (
                <Link
                  href={`/calls?call=${encodeURIComponent(d.source_call_uid)}`}
                  className={linkClass}
                >
                  {d.source_call_uid}
                </Link>
              )}
            </>
          )}
        </p>
      )}
      {d.status === "draft" && !readOnly && (
        <div className="flex gap-2">
          <Button
            size="sm"
            disabled={busy}
            onClick={() =>
              run(async () => {
                const got = await approveDraft(d.id, readPart());
                take(got.card);
              })
            }
          >
            승인
          </Button>
          <Button size="sm" variant="outline" onClick={onRetry}>
            다른 시간
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() =>
              run(async () => take((await cancelDraft(d.id, readPart())).card))
            }
          >
            취소
          </Button>
        </div>
      )}
      {d.status === "approved" && (
        <p className="text-muted-foreground">
          AX가 넣은 예약 · {d.decided_by} 승인 ·{" "}
          <Link href={`/schedule?date=${date}`} className={linkClass}>
            일정에서 보기
          </Link>
        </p>
      )}
      {d.status === "rejected" && (
        <div className="flex flex-col items-start gap-2">
          <p className="text-destructive">
            승인하지 못했어요 —{" "}
            {reasons.map((c) => REASON[c] ?? c).join(" · ") ||
              "검사에 걸렸습니다"}
          </p>
          {!readOnly && (
            <Button size="sm" variant="outline" onClick={onRetry}>
              다른 시간으로 다시 만들기
            </Button>
          )}
        </div>
      )}
      {error && <p className="text-destructive">{error}</p>}
    </Box>
  );
}

const money = (n: number) => `${n.toLocaleString("ko-KR")}원`;

/** 답 안의 카드 하나 — 환자·통화·청구·재무·출처·예약 초안(AI 비서 설계 §3.2) */
export function AssistantCard({
  card,
  onRetry,
  readOnly = false,
}: {
  card: Card;
  onRetry: () => void;
  readOnly?: boolean;
}) {
  if (card.type === "draft")
    return (
      <DraftCard initial={card.data} onRetry={onRetry} readOnly={readOnly} />
    );
  if (card.type === "source") return <ResultCard card={card.data} q="" />;
  const row = (
    kind: string,
    body: React.ReactNode,
    href: string,
    link: string,
  ) => (
    <Box size="sm" className="flex-row flex-wrap items-center gap-2 px-4">
      <Badge variant="muted">{kind}</Badge>
      <span className="min-w-0 flex-1">{body}</span>
      <Link href={href} className={linkClass}>
        {link}
      </Link>
    </Box>
  );
  if (card.type === "patient") {
    const p = card.data;
    return row(
      "환자",
      <>
        {p.name} ({p.chart_no}){p.birth_date ? ` · ${p.birth_date}` : ""}
      </>,
      `/calls?tab=history&q=${encodeURIComponent(p.chart_no)}`,
      "통화 보기",
    );
  }
  if (card.type === "call") {
    const c = card.data;
    return row(
      "통화",
      <>
        {c.at.slice(0, 16)} · {c.direction}
        {c.name ? ` · ${c.name}` : ""}
        {c.summary ? ` — ${c.summary}` : ""}
      </>,
      `/calls?call=${encodeURIComponent(c.call_uid)}`,
      "통화 화면",
    );
  }
  if (card.type === "claim") {
    const c = card.data;
    return row(
      "청구",
      <>
        {c.service_date} · {c.name} · {c.procedure_name} ·{" "}
        {money(c.claim_amount)}
        {c.adjust_note ? ` — ${c.adjust_note}` : ""}
      </>,
      "/claims",
      "청구 점검",
    );
  }
  const r = card.data;
  return row(
    "재무",
    <>
      {r.date} 불러온 불일치 {r.count}건
    </>,
    `/finance?month=${r.date.slice(0, 7)}&day=${r.date}`,
    "재무 대조",
  );
}
