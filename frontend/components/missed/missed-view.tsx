"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ErrorBand } from "@/components/prep/prep-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { errorText } from "@/lib/api";
import {
  type Channel,
  CHANNEL_LABEL,
  type Chip,
  CHIP_LABEL,
  CHIPS,
  fetchMissed,
  type MissedRow,
  type MissedSheet,
  recordContact,
  REMOVES,
  type Result,
  RESULT_LABEL,
  RESULTS,
} from "@/lib/missed-api";
import { readPart } from "@/lib/part";
import { won } from "@/lib/format";

const CHIP_VARIANT: Record<Chip, "danger" | "warn" | "info" | "muted"> = {
  recall: "info",
  scaling_unused: "info",
  noshow: "danger",
  balance: "warn",
  open_plan: "info",
  other_clinic: "muted",
};
const RESULT_VARIANT: Record<Result, "ok" | "warn" | "muted"> = {
  booked: "ok",
  no_answer: "warn",
  later: "muted",
  declined: "muted",
  other_clinic: "muted",
};

/** 2026-11-10 10:05:00 → 11/10 10:05 */
const shortAt = (at: string) =>
  `${at.slice(5, 7)}/${at.slice(8, 10)} ${at.slice(11, 16)}`;

/** ⑨ 미예약 환자 — 칩 필터·연락함 숨기기는 URL에 둔다(/missed?chip=…&hide=1) */
export function MissedView() {
  const params = useSearchParams();
  const router = useRouter();
  const chipParam = params.get("chip") ?? "";
  const chip = (CHIPS as string[]).includes(chipParam)
    ? (chipParam as Chip)
    : null;
  const hide = params.get("hide") === "1";
  const go = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    setMore(null); // 필터가 바뀌면 다시 처음 page명
    router.replace(`/missed${next.size ? `?${next}` : ""}`);
  };

  const [attempt, setAttempt] = useState(0);
  const [sheet, setSheet] = useState<MissedSheet | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState<number | null>(null); // 더보기로 늘린 보이는 수, null = 처음(page명)
  const [how, setHow] = useState(false);
  const [target, setTarget] = useState<MissedRow | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    fetchMissed(ac.signal).then(
      (data) => {
        setSheet(data);
        setError(null);
      },
      (e) => !ac.signal.aborted && setError(errorText(e)),
    );
    return () => ac.abort();
  }, [attempt]);

  if (!sheet)
    return error ? (
      <ErrorBand text={error} />
    ) : (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  const shown = more ?? sheet.page;
  // 다시 조회한 목록에서 매번 자른다 — 연락 뒤 순위가 바뀌어도 같은 사람이 두 번 보이거나 빠지지 않는다
  const rows = sheet.rows
    .filter((r) => !chip || r.chips.includes(chip))
    .filter((r) => !hide || !r.contact?.active);
  const contacted = sheet.rows.filter((r) => r.contact?.active).length;

  return (
    <div className="flex flex-col gap-3">
      {error && <ErrorBand text={error} />}
      <div className="flex flex-wrap gap-1.5" aria-label="칩">
        <Button
          size="sm"
          variant={!chip ? "secondary" : "outline"}
          aria-pressed={!chip}
          onClick={() => go({ chip: null })}
        >
          전체 {sheet.rows.length}
        </Button>
        {CHIPS.map((c) => (
          <Button
            key={c}
            size="sm"
            variant={c === chip ? "secondary" : "outline"}
            aria-pressed={c === chip}
            onClick={() => go({ chip: c })}
          >
            {CHIP_LABEL[c]} {sheet.chip_counts[c]}
          </Button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant={hide ? "secondary" : "outline"}
          aria-pressed={hide}
          onClick={() => go({ hide: hide ? null : "1" })}
        >
          연락함 숨기기 {contacted}
        </Button>
        <span className="flex-1" />
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={how}
          onClick={() => setHow(!how)}
        >
          순위는 어떻게 정해지나요?
        </Button>
      </div>
      {how && <Rules rules={sheet.rules} />}
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead className="bg-muted text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-right">전체 순위</th>
              <th className="px-3 py-2">환자</th>
              <th className="px-3 py-2">칩</th>
              <th className="px-3 py-2">마지막 내원</th>
              <th className="px-3 py-2 text-right">미수</th>
              <th className="px-3 py-2">연락</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, shown).map((r) => (
              <tr key={r.patient_id} className="border-t">
                <td className="px-3 py-2 text-right align-top font-bold tabular-nums">
                  {r.rank}
                </td>
                <td className="px-3 py-2 align-top whitespace-nowrap">
                  <Link
                    href={`/emr?chart=${encodeURIComponent(r.chart_no)}`}
                    className="font-bold text-primary hover:underline"
                  >
                    {r.name}
                  </Link>
                  <div className="text-xs text-muted-foreground tabular-nums">
                    {r.chart_no} · {r.age}세 · {r.phone}
                  </div>
                </td>
                <td className="px-3 py-2 align-top">
                  <div className="flex flex-wrap gap-1">
                    {r.chips.map((c) => (
                      <Badge key={c} variant={CHIP_VARIANT[c]}>
                        {CHIP_LABEL[c]}
                      </Badge>
                    ))}
                  </div>
                </td>
                <td className="px-3 py-2 align-top whitespace-nowrap tabular-nums">
                  {r.last_visit}
                  <div className="text-xs text-muted-foreground">
                    {Math.floor(r.signals.months_since_visit)}개월 전
                  </div>
                </td>
                <td className="px-3 py-2 text-right align-top tabular-nums">
                  {r.balance ? won(r.balance) : "-"}
                </td>
                <td className="px-3 py-2 align-top whitespace-nowrap">
                  {r.contact && (
                    <div className="mb-1 flex items-center gap-1.5">
                      <Badge variant={RESULT_VARIANT[r.contact.result]}>
                        {RESULT_LABEL[r.contact.result]}
                      </Badge>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {CHANNEL_LABEL[r.contact.channel]} ·{" "}
                        {shortAt(r.contact.at)} · {r.contact.by}
                      </span>
                    </div>
                  )}
                  <Button
                    size="xs"
                    variant="outline"
                    onClick={() => setTarget(r)}
                  >
                    연락 남기기
                  </Button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-10 text-center text-muted-foreground"
                >
                  조건에 맞는 환자가 없습니다
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {rows.length > shown && (
        <Button
          variant="outline"
          className="self-center"
          onClick={() => setMore(shown + sheet.page)}
        >
          더보기 ({Math.min(shown, rows.length)}/{rows.length}명)
        </Button>
      )}
      <ContactDialog
        row={target}
        onClose={() => setTarget(null)}
        onSaved={() => {
          setTarget(null);
          setAttempt((n) => n + 1);
        }}
      />
    </div>
  );
}

function Rules({ rules }: { rules: MissedSheet["rules"] }) {
  return (
    <section
      aria-label="순위 정하는 법"
      className="rounded-xl border bg-card px-4 py-3 text-sm"
    >
      <h2 className="font-bold">순위 정하는 법</h2>
      <p className="text-xs text-muted-foreground">
        연락하면 결과(예약·수납)가 날 가능성 순 — 점수는 코드가 계산하고 AI는
        쓰지 않습니다. 값은 앱 설정에서 바꿉니다.
      </p>
      <ul className="mt-2 flex list-disc flex-col gap-1 pl-5">
        {rules.map((r) => (
          <li key={r.name}>
            <b>{r.name}</b> {r.value}
            <span className="text-muted-foreground">
              {" "}
              — {r.reasons.join(" · ")}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** 연락 결과 남기기 — 수단(전화·문자)을 고르고 결과 5개 중 하나를 누르면 남긴다(설계 §4). 목록에서 빠지는
 * 결과(REMOVES)는 한 번 더 묻고, 부재중·나중에 다시는 바로 남긴다 */
function ContactDialog({
  row,
  onClose,
  onSaved,
}: {
  row: MissedRow | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [channel, setChannel] = useState<Channel>("call");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Result | null>(null);
  const save = (result: Result) => {
    if (!row) return;
    setBusy(true);
    setError(null);
    recordContact(row.patient_id, result, channel, readPart()).then(
      () => {
        setBusy(false);
        setChannel("call");
        setConfirm(null);
        onSaved();
      },
      (e) => {
        setBusy(false);
        setError(errorText(e));
      },
    );
  };
  return (
    <Dialog
      open={row !== null}
      onOpenChange={(v) => {
        if (!v && !busy) {
          setError(null);
          setConfirm(null);
          onClose();
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{row?.name} 연락 결과</DialogTitle>
          <DialogDescription>
            {row?.chart_no} · {row?.phone}
          </DialogDescription>
        </DialogHeader>
        <div className="flex gap-1.5" aria-label="수단">
          {(Object.keys(CHANNEL_LABEL) as Channel[]).map((c) => (
            <Button
              key={c}
              size="sm"
              variant={c === channel ? "secondary" : "outline"}
              aria-pressed={c === channel}
              onClick={() => setChannel(c)}
            >
              {CHANNEL_LABEL[c]}
            </Button>
          ))}
        </div>
        {confirm ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="flex-1 text-sm">
              {RESULT_LABEL[confirm]} — {row?.name}님을 목록에서 뺄까요?
            </span>
            <Button size="sm" disabled={busy} onClick={() => save(confirm)}>
              빼기
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => setConfirm(null)}
            >
              취소
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5" aria-label="결과">
            {RESULTS.map((r) => (
              <Button
                key={r}
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => (REMOVES.includes(r) ? setConfirm(r) : save(r))}
              >
                {RESULT_LABEL[r]}
              </Button>
            ))}
          </div>
        )}
        {error && <ErrorBand text={error} />}
      </DialogContent>
    </Dialog>
  );
}
