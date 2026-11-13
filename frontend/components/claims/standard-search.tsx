"use client";

import { useState } from "react";

import { StandardBox } from "@/components/claims/adjust-table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorText } from "@/lib/api";
import { searchStandards, type StandardHit } from "@/lib/claims-api";

/** ⑤-3 청구 기준 찾기 — 두 탭 공통 검색칸, 검색했을 때만 "찾은 조항" 상자(설계 §3). AI 답 문장 없이 절 원문만 */
export function StandardSearch() {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<StandardHit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = () => {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    searchStandards(q.trim()).then(
      (r) => {
        setHits(r.results);
        setBusy(false);
      },
      (e) => {
        setHits(null);
        setError(errorText(e));
        setBusy(false);
      },
    );
  };
  return (
    <div className="flex flex-col gap-2">
      <form
        role="search"
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
      >
        <Input
          type="search"
          aria-label="청구 기준 찾기"
          placeholder="청구 기준 찾기 — 예: 치석제거 다음 달로 넘어가면 왜 깎여?"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <Button type="submit" variant="outline" disabled={busy}>
          찾기
        </Button>
      </form>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {hits && (
        <section
          aria-label="찾은 조항"
          className="flex flex-col gap-2 rounded-lg border bg-muted p-3"
        >
          <div className="flex items-center justify-between">
            <h3 className="font-bold">찾은 조항</h3>
            <Button size="sm" variant="ghost" onClick={() => setHits(null)}>
              닫기
            </Button>
          </div>
          {hits.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              맞는 조항이 없습니다
            </p>
          ) : (
            hits.map((h) => (
              <StandardBox
                key={h.chunk_id}
                card={{
                  doc_title: h.doc_title,
                  heading: h.heading,
                  body: h.text,
                }}
              />
            ))
          )}
        </section>
      )}
    </div>
  );
}
