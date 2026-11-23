"use client";

import Link from "next/link";
import { useState } from "react";

import { ResultCard } from "@/components/ask/result-card";
import { Badge, badgeVariants } from "@/components/ui/badge";
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
import { cn } from "@/lib/utils";

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
        {d.status === "draft" && (
          <span className="text-xs text-muted-foreground">
            승인 전 — 아직 일정에 없습니다
          </span>
        )}
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

type BigCard = Extract<Card, { type: "draft" | "source" }>;
type SourceCard = Extract<Card, { type: "source" }>;
type LinkCard = Exclude<Card, BigCard>;

/** 답 아래 카드 — 예약 초안(승인 단추)과 문서 출처(규정·안내문·청구 기준 = 규칙)는 큰 카드, 환자·통화·청구·일계표
 * 날은 본문이 이미 말한 대상이라 바로 가기 줄, 사례 출처(지난 통화·문자·차트 = 전에 실제로 안내한 기록)는 칩 줄 */
export function ReplyCards({
  cards,
  onRetry,
  readOnly,
}: {
  cards: Card[];
  onRetry: () => void;
  readOnly: boolean;
}) {
  const drafts = cards.filter((c) => c.type === "draft");
  const sources = cards.filter((c): c is SourceCard => c.type === "source");
  const docs = sources.filter((c) => c.data.source_type === "document");
  const cases = sources.filter((c) => c.data.source_type !== "document");
  const links = cards.filter(
    (c): c is LinkCard => c.type !== "draft" && c.type !== "source",
  );
  return (
    <>
      {drafts.map((card) => (
        <AssistantCard
          key={`draft:${card.id}`}
          card={card}
          onRetry={onRetry}
          readOnly={readOnly}
        />
      ))}
      {docs.length > 0 && (
        <section aria-label="문서 출처" className="flex flex-col gap-2">
          <h3 className="text-xs font-medium tracking-tight text-muted-foreground">
            문서 출처
          </h3>
          {docs.map((card) => (
            <ResultCard key={card.id} card={card.data} q="" />
          ))}
        </section>
      )}
      <Shortcuts cards={links} />
      <CaseSources cards={cases} />
    </>
  );
}

const CASE_KIND: Record<string, string> = {
  call: "통화",
  message_thread: "문자",
  chart_note: "차트",
};

/** 사례 출처 줄 — 칩을 누르면 그 기록 발췌가 아래에 펼쳐진다(문자·차트는 따로 갈 화면이 없어 발췌로 본다) */
function CaseSources({ cards }: { cards: SourceCard[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!cards.length) return null;
  const shown = cards.find((c) => c.id === open);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2" aria-label="사례 출처">
        <span className="text-xs text-muted-foreground">사례 출처</span>
        {cards.map((c) => {
          const d = c.data;
          const who = d.patient_name ?? d.caller_name ?? d.chart_no;
          return (
            // 배지 asChild는 상태 점 자리 때문에 Slot에 자식이 둘이 되어 쓰지 않고, 같은 variant 모양만 단추에 입힌다
            <button
              key={c.id}
              type="button"
              aria-expanded={open === c.id}
              onClick={() => setOpen(open === c.id ? null : c.id)}
              className={cn(
                badgeVariants({ variant: "chip" }),
                "cursor-pointer hover:border-primary aria-expanded:border-primary",
              )}
            >
              <span className="font-bold">
                {CASE_KIND[d.source_type] ?? "기록"}
              </span>
              {who}
              {d.event_date && ` · ${md(d.event_date)}`}
            </button>
          );
        })}
      </div>
      {shown && <ResultCard card={shown.data} q="" />}
    </div>
  );
}

const md = (s: string) => `${s.slice(5, 7)}/${s.slice(8, 10)}`;
const chipLink = "font-medium text-link hover:underline";

/** 바로 가기 줄 — 같은 환자의 환자·통화 카드는 칩 하나로(차트 번호, 없으면 통화의 이름). 날짜는 답이 고른 통화만,
 * 누르면 통화 기록 탭에서 그 사람으로 거른 채 그 통화를 연다(할 일 탭이 아니라) */
function Shortcuts({ cards }: { cards: LinkCard[] }) {
  const people = new Map<
    string,
    { name: string; chart: string | null; calls: { uid: string; at: string }[] }
  >();
  const person = (key: string, name: string, chart: string | null) => {
    if (!people.has(key)) people.set(key, { name, chart, calls: [] });
    return people.get(key)!;
  };
  const rest: React.ReactNode[] = [];
  for (const c of cards) {
    if (c.type === "patient")
      person(c.data.chart_no, c.data.name, c.data.chart_no);
    else if (c.type === "call") {
      const d = c.data;
      const key = d.chart_no ?? `call:${d.name ?? d.call_uid}`;
      person(key, d.name ?? "이름 없음", d.chart_no).calls.push({
        uid: d.call_uid,
        at: d.at,
      });
    } else if (c.type === "claim")
      rest.push(
        <Badge key={`claim:${c.id}`} variant="chip">
          {c.data.name} · {md(c.data.service_date)} {c.data.procedure_name}
          <Link href="/claims" className={chipLink}>
            청구 점검
          </Link>
        </Badge>,
      );
    else
      rest.push(
        <Badge key={`recon:${c.id}`} variant="chip">
          {md(c.data.date)} 일계표 불일치 {c.data.count}건
          <Link
            href={`/finance?month=${c.data.date.slice(0, 7)}&day=${c.data.date}`}
            className={chipLink}
          >
            재무 대조
          </Link>
        </Badge>,
      );
  }
  if (!people.size && !rest.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="바로 가기">
      <span className="text-xs text-muted-foreground">바로 가기</span>
      {[...people].map(([key, p]) => (
        <Badge key={key} variant="chip">
          <span className="font-bold">{p.name}</span>
          {p.calls.map((c) => (
            <Link
              key={c.uid}
              href={`/calls?${new URLSearchParams({ tab: "history", q: p.chart ?? p.name, call: c.uid })}`}
              className={chipLink}
            >
              {md(c.at)} 통화
            </Link>
          ))}
          {/* 근거 통화 없이 환자만 나온 답(예: 내일 환자) — 그 환자의 통화 기록으로 */}
          {!p.calls.length && p.chart && (
            <Link
              href={`/calls?tab=history&q=${encodeURIComponent(p.chart)}`}
              className={chipLink}
            >
              통화 기록
            </Link>
          )}
        </Badge>
      ))}
      {rest}
    </div>
  );
}

/** 답 안의 큰 카드 — 예약 초안·기록 출처(기능 정의 §4 AI 비서) */
export function AssistantCard({
  card,
  onRetry,
  readOnly = false,
}: {
  card: BigCard;
  onRetry: () => void;
  readOnly?: boolean;
}) {
  if (card.type === "draft")
    return (
      <DraftCard initial={card.data} onRetry={onRetry} readOnly={readOnly} />
    );
  return <ResultCard card={card.data} q="" />;
}
