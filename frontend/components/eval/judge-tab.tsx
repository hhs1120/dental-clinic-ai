"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { Transcript } from "@/components/calls/transcript";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorText, useFetch } from "@/lib/api";
import { DIRECTION_LABEL, type LabelNames, type Labels } from "@/lib/calls-api";
import {
  fetchJudgeNext,
  type Revealed,
  saveJudgment,
  skipJudgment,
} from "@/lib/eval-api";
import { readPart } from "@/lib/part";
import { cn } from "@/lib/utils";

type Opt = { key: string; value: string | boolean | null; label: string };
type Item = { key: string; label: string; digits: number; opts: Opt[] };

const FLAGS: [string, string][] = [
  ["emergency", "응급"],
  ["complaint", "불만"],
  ["price_inquiry", "비용 문의"],
  ["wants_appt", "예약 희망"],
  ["appt_confirmed", "예약 잡힘"],
  ["has_todo", "할 일 있음"],
];
const UNSURE = "모르겠음·애매";

/** 판정 항목과 키 — 앱 설계 §9 판정 키 매핑(목적 2자리 01~ enum 순서·00 모르겠음, 나머지 1자리·0 모르겠음) */
function items(names: LabelNames): Item[] {
  const purpose = Object.entries(names.purpose).map(([v, l], i) => ({
    key: String(i + 1).padStart(2, "0"),
    value: v,
    label: l,
  }));
  const counterpart = Object.entries(names.counterpart).map(([v, l], i) => ({
    key: String(i + 1),
    value: v,
    label: l,
  }));
  return [
    {
      key: "primary_purpose",
      label: "목적",
      digits: 2,
      opts: [...purpose, { key: "00", value: null, label: UNSURE }],
    },
    {
      key: "counterpart",
      label: "상대",
      digits: 1,
      opts: [...counterpart, { key: "0", value: null, label: UNSURE }],
    },
    {
      key: "urgency",
      label: "급함",
      digits: 1,
      opts: [
        { key: "1", value: "low", label: names.urgency.low },
        { key: "2", value: "normal", label: names.urgency.normal },
        { key: "3", value: "urgent", label: names.urgency.urgent },
        { key: "0", value: null, label: UNSURE },
      ],
    },
    ...FLAGS.map(([key, label]) => ({
      key,
      label,
      digits: 1,
      opts: [
        { key: "1", value: false, label: "아니오" },
        { key: "2", value: true, label: "예" },
        { key: "0", value: null, label: UNSURE },
      ],
    })),
  ];
}

const same = (a: unknown, b: unknown) =>
  a === b || (typeof b === "number" && a === (b === 1));

/** 판정 — 왼쪽 9항목 고르기, 오른쪽 녹취록. 모델 라벨은 저장 뒤에만(기능 정의 §4 ② "판정") */
export function JudgeTab({ names }: { names: LabelNames | null }) {
  // null(남은 건 없음)과 불러오는 중을 가르려고 한 겹 싼다
  const next = useFetch("judge", (s) => fetchJudgeNext(s).then((n) => ({ n })));
  const call = next.data?.n ?? null;
  const list = useMemo(() => (names ? items(names) : []), [names]);
  const [answers, setAnswers] = useState<Record<string, Opt>>({});
  // 현재 항목·입력 중인 숫자 — 키를 빠르게 이어 치면 다시 그리기 전에 다음 키가 오므로 ref가 원본, state는 화면용
  const [active, setActiveState] = useState(0);
  const [buf, setBufState] = useState("");
  const activeRef = useRef(0);
  const bufRef = useRef("");
  const setActive = (f: number | ((a: number) => number)) => {
    activeRef.current = typeof f === "function" ? f(activeRef.current) : f;
    setActiveState(activeRef.current);
  };
  const setBuf = (v: string) => {
    bufRef.current = v;
    setBufState(v);
  };
  const [memo, setMemo] = useState("");
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const [fail, setFail] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const memoRef = useRef<HTMLInputElement>(null);
  const done = list.length > 0 && list.every((it) => it.key in answers);

  const reset = () => {
    setAnswers({});
    setActive(0);
    setBuf("");
    setMemo("");
    setRevealed(null);
    setFail(null);
  };
  const pick = (i: number, opt: Opt) => {
    setAnswers((a) => ({ ...a, [list[i].key]: opt }));
    setBuf("");
    setActive(Math.min(i + 1, list.length - 1));
  };
  const save = async () => {
    if (!call || !done || busy || next.loading) return;
    setBusy(true);
    setFail(null);
    try {
      const labels: Labels = {};
      for (const it of list) {
        const v = answers[it.key].value;
        if (v !== null) labels[it.key] = v;
      }
      setRevealed(
        await saveJudgment({
          call_uid: call.call_uid,
          labels,
          note: memo.trim() || null,
          part: readPart(),
        }),
      );
    } catch (e) {
      setFail(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const advance = () => {
    reset();
    next.reload();
  };

  // 렌더마다 다시 건다 — 핸들러가 늘 지금 상태를 본다
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!call || list.length === 0 || next.loading) return; // 다음 건을 불러오는 동안 이전 건에 저장하지 않게
      const typing = document.activeElement === memoRef.current;
      if (e.key === "Enter") {
        e.preventDefault();
        if (revealed) advance();
        else void save();
        return;
      }
      if (typing || revealed) return;
      if (e.key === "/") {
        e.preventDefault();
        memoRef.current?.focus();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((a) => Math.min(a + 1, list.length - 1));
        setBuf("");
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((a) => Math.max(a - 1, 0));
        setBuf("");
      } else if (/^[0-9]$/.test(e.key)) {
        const it = list[activeRef.current];
        const typed = bufRef.current + e.key;
        if (typed.length < it.digits) {
          setBuf(typed);
          return;
        }
        const opt = it.opts.find((o) => o.key === typed);
        if (opt) pick(activeRef.current, opt);
        else setBuf("");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (next.error)
    return <p className="text-sm text-muted-foreground">{next.error}</p>;
  if (next.data && call === null)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        남은 건 없음 — 판정할 엇갈림·표본이 없습니다
      </p>
    );
  if (!call || !names)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        남은 엇갈림·표본 {call.left} · 오늘 {call.done_today} · 누적{" "}
        {call.done_total}
      </p>
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <section
          className="flex flex-col gap-1 rounded-lg border bg-card p-4 shadow-card"
          aria-label="판정"
        >
          {list.map((it, i) => {
            const mine = revealed?.classifier?.[it.key];
            const judge = revealed?.judge?.[it.key];
            return (
              <div
                key={it.key}
                className={cn(
                  "flex flex-col gap-1 rounded-md px-2 py-1.5",
                  active === i && !revealed && "bg-accent",
                )}
              >
                <div className="flex items-center gap-2 text-sm font-bold">
                  {it.label}
                  {active === i && buf && (
                    <span className="text-primary">{buf}_</span>
                  )}
                </div>
                <div className="flex flex-wrap gap-1">
                  {it.opts.map((o) => {
                    const on = answers[it.key]?.key === o.key;
                    const model =
                      revealed && o.value !== null && same(o.value, mine);
                    return (
                      <Button
                        key={o.key}
                        type="button"
                        size="xs"
                        variant={on ? "default" : "outline"}
                        className={cn(model && "ring-2 ring-ring")}
                        disabled={!!revealed}
                        onClick={() => pick(i, o)}
                      >
                        <kbd className="text-[0.65rem] opacity-70">{o.key}</kbd>
                        {o.label}
                      </Button>
                    );
                  })}
                </div>
                {revealed && (
                  <p className="text-xs text-muted-foreground">
                    분류기: {label(it, mine)} · 판정기: {label(it, judge)}
                  </p>
                )}
              </div>
            );
          })}
          <Input
            ref={memoRef}
            className="mt-2"
            value={memo}
            onChange={(e) => setMemo(e.target.value)}
            placeholder="메모(선택) — 애매한 이유, 원장 기준 필요 등  (/ 키로 들어가기, Enter 저장)"
            aria-label="메모"
          />
          {fail && <p className="text-sm text-status-danger-fg">{fail}</p>}
          <div className="mt-2 flex items-center gap-2">
            {revealed ? (
              <Button onClick={advance}>다음 건 (Enter)</Button>
            ) : (
              <>
                <Button onClick={save} disabled={!done || busy}>
                  저장·다음 (Enter)
                </Button>
                <Button
                  variant="outline"
                  onClick={async () => {
                    await skipJudgment(call.call_uid);
                    advance();
                  }}
                >
                  건너뛰기
                </Button>
                <span className="text-xs text-muted-foreground">
                  {Object.keys(answers).length}/{list.length} 항목
                </span>
              </>
            )}
          </div>
        </section>
        <section
          className="max-h-[calc(100vh-14rem)] overflow-y-auto rounded-lg border bg-card p-4"
          aria-label="녹취록"
        >
          <p className="mb-2 text-sm text-muted-foreground">
            {DIRECTION_LABEL[call.direction]} · {call.at.slice(0, 16)} · #
            {call.call_uid}
          </p>
          <Transcript text={call.transcript} bubbles />
        </section>
      </div>
      <p className="text-xs text-muted-foreground">
        단축키 — 숫자로 선택(자동으로 다음 항목) · 목적은 두 자리(01 = 첫 목적,
        00 = 모르겠음) · 나머지는 한 자리, 0 = 모르겠음 · ↑↓ 항목 이동 · / 메모
        · Enter 저장·다음. 모델 판정은 저장 뒤에만 보입니다(먼저 보면 끌려감).
      </p>
    </div>
  );
}

function label(it: Item, v: unknown): string {
  if (v === undefined || v === null) return "—";
  return (
    it.opts.find((o) => o.value !== null && same(o.value, v))?.label ??
    String(v)
  );
}
