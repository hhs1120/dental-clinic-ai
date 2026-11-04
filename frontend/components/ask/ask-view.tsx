"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { SearchForm } from "@/components/ask/search-form";
import {
  SearchResults,
  type SearchStatus,
} from "@/components/ask/search-results";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError, getJson, type SearchResponse } from "@/lib/api";
import { readSearch, toApiParams, writeSearch } from "@/lib/search-query";

const TABS = [
  { value: "chat", label: "질문하기" },
  { value: "search", label: "기록 검색" },
  { value: "docs", label: "문서함" },
];

type Answer =
  { key: string; data: SearchResponse } | { key: string; error: string };

export function AskView() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = TABS.some((t) => t.value === params.get("tab"))
    ? params.get("tab")!
    : "search";

  // 검색은 URL이 바뀔 때만 돈다 — 새로고침·뒤로 가기·공유 링크가 같은 경로를 탄다
  const search = readSearch(params);
  const key = writeSearch(search).toString();

  // 입력 초안은 탭을 바꿔도 남고, URL의 검색이 바뀌면 그 값으로 다시 맞춘다
  const [draft, setDraft] = useState(search);
  const [draftKey, setDraftKey] = useState(key);
  if (draftKey !== key) {
    setDraftKey(key);
    setDraft(search);
  }

  const [answer, setAnswer] = useState<Answer | null>(null);
  useEffect(() => {
    const s = readSearch(new URLSearchParams(key));
    if (!s.q) return;
    const ac = new AbortController(); // 늦게 온 옛 응답은 버린다
    getJson<SearchResponse>("/search", toApiParams(s), ac.signal).then(
      (data) => setAnswer({ key, data }),
      (e) => {
        if (!ac.signal.aborted)
          setAnswer({
            key,
            error:
              e instanceof ApiError ? e.message : "서버에 연결하지 못했습니다",
          });
      },
    );
    return () => ac.abort();
  }, [key]);

  const status: SearchStatus = !search.q
    ? { kind: "idle" }
    : answer?.key !== key
      ? { kind: "loading" }
      : "error" in answer
        ? { kind: "error", message: answer.error }
        : { kind: "done", data: answer.data };

  const changeTab = (value: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", value);
    router.replace(`/ask?${next}`);
  };

  return (
    <Tabs value={tab} onValueChange={changeTab} className="gap-6">
      <TabsList variant="line" className="w-full justify-start border-b">
        {TABS.map((t) => (
          <TabsTrigger key={t.value} value={t.value} className="flex-none px-4">
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="search" className="flex flex-col gap-6">
        <SearchForm
          draft={draft}
          onChange={setDraft}
          onSubmit={() => router.push(`/ask?${writeSearch(draft)}`)}
        />
        <SearchResults status={status} search={search} />
      </TabsContent>
      {TABS.filter((t) => t.value !== "search").map((t) => (
        <TabsContent key={t.value} value={t.value}>
          <p className="text-muted-foreground">준비 중입니다.</p>
        </TabsContent>
      ))}
    </Tabs>
  );
}
