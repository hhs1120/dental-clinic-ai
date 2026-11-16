"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { errorText } from "@/lib/api";
import {
  type Appeal,
  type AppealBasis,
  draftAppeal,
  reviewAppeal,
  submitAppeal,
} from "@/lib/claims-api";
import { readPart } from "@/lib/part";

const CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩";
const circled = (n: number) => CIRCLED[n - 1] ?? `(${n})`;

/** 문장 꼬리표 — 근거 종류(backend/claim_appeal.py BASES) */
const tag = (basis: AppealBasis, n: number | null) =>
  basis === "assumption"
    ? `추측 ${circled(n ?? 0)}`
    : {
        adjustment: "조정 내역 인용",
        standard: "기준 절 인용",
        chart: "차트 인용",
      }[basis];

/** ⑤-4 재심 사유서 초안(설계 §4) — 재심 신청을 기록한 건만. 문장마다 인용·추측 꼬리표, 추측 목록, 검토 확인 뒤에만 복사·
 * 제출 결정(기록만 — AI는 제출하지 않는다). 화면에서 문장을 고치지 않는다 — 고치기는 붙여 넣은 곳에서 */
export function AppealDraft({
  claimId,
  appeal,
  internal,
  onSaved,
}: {
  claimId: number;
  appeal: Appeal | null;
  internal: boolean; // 이 사유의 기준 절이 병원 내부 점검 기준 — 사유서는 기준을 인용하지 않는다
  onSaved: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 어느 초안·검토 상태를 복사했는지 — 초안이 바뀌거나 검토 확인이 풀리면 "복사함"이 저절로 사라진다
  // (옛 글을 복사한 줄 알고 붙여 넣지 않게)
  const copyKey = `${appeal?.at}|${appeal?.reviewed?.at ?? ""}`;
  const [copiedFor, setCopiedFor] = useState<string | null>(null);
  const copied = copiedFor === copyKey;
  const [confirming, setConfirming] = useState(false);
  const run = (label: string, call: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    call().then(
      () => {
        setBusy(null);
        onSaved();
      },
      (e) => {
        setBusy(null);
        setError(errorText(e));
        onSaved(); // 실패해도 서버 상태로 다시 그린다
      },
    );
  };
  const draft = () => {
    setConfirming(false);
    run("draft", () => draftAppeal(claimId));
  };
  const sentences = appeal?.sentences ?? null;
  const reviewed = !!appeal?.reviewed;
  const submitted = appeal?.submitted ?? null;
  // 사유서 본문 = 번호·꼬리표 없이 문장을 한 문단으로 — 화면 상자와 복사하는 글이 같다
  const body = sentences?.map((s) => s.text).join(" ") ?? "";
  const copy = () =>
    navigator.clipboard.writeText(body).then(
      () => setCopiedFor(copyKey),
      () => setError("복사하지 못했습니다"),
    );
  return (
    <section aria-label="재심 사유서" className="flex flex-col gap-2">
      <h3 className="font-bold">재심 사유서</h3>
      {internal && (
        <p className="rounded-lg border border-dashed px-2.5 py-2 text-xs text-muted-foreground">
          이 사유의 기준은 고시에 같은 문장이 없는 병원 내부 점검 기준입니다.
          사유서는 기준을 인용하지 않고 조정 사유와 차트 기록을 근거로 씁니다 —
          다툴 근거가 약할 수 있습니다.
        </p>
      )}
      {!appeal ? (
        <Button size="sm" variant="outline" disabled={!!busy} onClick={draft}>
          {busy === "draft" ? "쓰는 중…" : "초안 쓰기"}
        </Button>
      ) : !sentences ? (
        <p className="text-sm text-muted-foreground">
          {appeal.stale
            ? "규칙이 바뀌어 다시 써야 합니다 — 이전 초안은 지금 기준에 맞지 않습니다"
            : "초안을 만들지 못했습니다"}
        </p>
      ) : (
        <>
          <section
            aria-label="사유서 본문"
            className="flex flex-col gap-1.5 rounded-lg border bg-card p-2.5"
          >
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs font-bold">사유서 본문</p>
              <Button
                size="sm"
                variant="outline"
                disabled={!reviewed}
                onClick={copy}
              >
                {copied ? "복사함" : "복사"}
              </Button>
            </div>
            <p className="text-sm">{body}</p>
            <p className="text-xs text-muted-foreground">
              [복사]하면 이 글이 그대로 복사됩니다
            </p>
          </section>
          <p className="text-xs font-bold text-muted-foreground">
            문장별 근거 (확인용)
          </p>
          <ol className="flex list-decimal flex-col gap-1 pl-5 text-sm">
            {sentences.map((s, i) => (
              <li key={i}>
                {s.text}{" "}
                <Badge variant={s.basis === "assumption" ? "warn" : "muted"}>
                  {tag(s.basis, s.assumption)}
                </Badge>
              </li>
            ))}
          </ol>
          {!!appeal.assumptions?.length && (
            <div className="rounded-lg border border-status-warn-border bg-status-warn-bg p-2.5 text-sm text-status-warn-fg">
              <p className="text-xs font-bold">
                AI가 추측한 부분 — 여기만 확인하면 됩니다
              </p>
              <ul className="flex flex-col gap-0.5">
                {appeal.assumptions.map((a, i) => (
                  <li key={i}>
                    {circled(i + 1)} {a}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={reviewed}
              disabled={!!busy || !!submitted}
              onCheckedChange={(v) =>
                run("review", () =>
                  reviewAppeal(claimId, readPart(), v === true),
                )
              }
              aria-label="검토했고 정확함"
            />
            검토했고 정확함
          </label>
          <div className="flex flex-wrap items-center gap-1.5">
            {submitted ? (
              <Badge variant="muted">제출 결정 · {submitted.by}</Badge>
            ) : (
              <Button
                size="sm"
                disabled={!reviewed || !!busy}
                onClick={() =>
                  run("submit", () => submitAppeal(claimId, readPart()))
                }
              >
                제출 결정
              </Button>
            )}
          </div>
          {!submitted && (
            <p className="text-xs text-muted-foreground">
              검토 확인 전에는 복사·제출할 수 없습니다 · AI는 제출하지 않습니다
            </p>
          )}
        </>
      )}
      {appeal && !submitted && confirming && (
        <div className="flex flex-wrap items-center gap-1.5 text-sm">
          검토한 초안을 버리고 새로 쓸까요?
          <Button size="sm" variant="outline" disabled={!!busy} onClick={draft}>
            새로 쓰기
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setConfirming(false)}
          >
            취소
          </Button>
        </div>
      )}
      {appeal && !submitted && !confirming && (
        <Button
          size="sm"
          variant="ghost"
          disabled={!!busy}
          // 검토 확인한 초안은 한 번 묻고 버린다
          onClick={() => (reviewed ? setConfirming(true) : draft())}
        >
          {busy === "draft" ? "쓰는 중…" : "다시 쓰기"}
        </Button>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </section>
  );
}
