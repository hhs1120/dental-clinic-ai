"use client";

import { useEffect, useState } from "react";

import { Transcript } from "@/components/calls/transcript";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText, useFetch } from "@/lib/api";
import { DIRECTION_LABEL } from "@/lib/calls-api";
import { type Choice, fetchBlindNext, saveBlindVote } from "@/lib/eval-api";
import { readPart } from "@/lib/part";

const CHOICES: [string, Choice, string][] = [
  ["1", "left", "1이 나음"],
  ["2", "right", "2가 나음"],
  ["3", "tie", "비슷"],
  ["4", "both_wrong", "둘 다 틀림"],
];

/** 블라인드 비교 — 왼쪽 요약 두 개와 고르기, 오른쪽 녹취록. 버전은 고른 뒤에만(평가 설계 §2.4) */
export function BlindTab() {
  const next = useFetch("blind", (s) => fetchBlindNext(s).then((n) => ({ n })));
  const pair = next.data?.n ?? null;
  const [picked, setPicked] = useState<Choice | null>(null);
  const [versions, setVersions] = useState<{
    left: string;
    right: string;
  } | null>(null);
  const [fail, setFail] = useState<string | null>(null);

  const vote = async (choice: Choice) => {
    if (!pair || versions || next.loading) return;
    setPicked(choice);
    setFail(null);
    try {
      setVersions(
        await saveBlindVote({
          left_id: pair.left.classification_id,
          right_id: pair.right.classification_id,
          choice,
          part: readPart(),
        }),
      );
    } catch (e) {
      setPicked(null);
      setFail(errorText(e));
    }
  };
  const advance = () => {
    setPicked(null);
    setVersions(null);
    setFail(null);
    next.reload();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!pair || next.loading) return; // 다음 쌍을 불러오는 동안 이전 쌍에 다시 투표하지 않게
      if (e.key === "Enter" && versions) {
        e.preventDefault();
        advance();
        return;
      }
      const c = CHOICES.find(([k]) => k === e.key);
      if (c) {
        e.preventDefault();
        void vote(c[1]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (next.error)
    return <p className="text-sm text-muted-foreground">{next.error}</p>;
  if (!next.data)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        불러오는 중…
      </p>
    );
  if (!pair)
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        남은 비교 없음
      </p>
    );

  const side = (n: 1 | 2) => {
    const s = n === 1 ? pair.left : pair.right;
    const v = versions?.[n === 1 ? "left" : "right"];
    return (
      <div className="flex flex-col gap-1.5 rounded-lg border bg-card p-3">
        <div className="flex items-center gap-2">
          <span className="flex size-6 items-center justify-center rounded-full bg-primary text-sm font-bold text-primary-foreground">
            {n}
          </span>
          {v && <Badge variant="kind">{v}</Badge>}
        </div>
        <p className="text-sm">{s.text}</p>
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        남은 비교 {pair.left_count} · 두 요약 중 원래 통화를 더 정확하고
        빠짐없이 옮긴 쪽을 고릅니다. 좌우는 통화마다 정해지고 버전은 고른 뒤에
        보입니다.
      </p>
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <section className="flex flex-col gap-3" aria-label="요약 비교">
          {side(1)}
          {side(2)}
          <div className="flex flex-wrap gap-2">
            {CHOICES.map(([k, c, l]) => (
              <Button
                key={k}
                variant={picked === c ? "default" : "outline"}
                disabled={!!versions || next.loading}
                onClick={() => vote(c)}
              >
                <kbd className="text-xs opacity-70">{k}</kbd> {l}
              </Button>
            ))}
            {versions && <Button onClick={advance}>다음 (Enter)</Button>}
          </div>
          {fail && <p className="text-sm text-status-danger-fg">{fail}</p>}
        </section>
        <section
          className="max-h-[calc(100vh-14rem)] overflow-y-auto rounded-lg border bg-card p-4"
          aria-label="녹취록"
        >
          <p className="mb-2 text-sm text-muted-foreground">
            {DIRECTION_LABEL[pair.direction]} · {pair.at.slice(0, 16)} · #
            {pair.call_uid}
          </p>
          <Transcript text={pair.transcript} bubbles />
        </section>
      </div>
      <p className="text-xs text-muted-foreground">
        단축키 — 1 · 2 · 3 비슷 · 4 둘 다 틀림 · Enter 다음
      </p>
    </div>
  );
}
