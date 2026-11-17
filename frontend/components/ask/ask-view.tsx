"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { DocsPanel } from "@/components/ask/docs-panel";
import { SearchForm } from "@/components/ask/search-form";
import { SearchSummary } from "@/components/ask/search-summary";
import {
  SearchResults,
  type SearchStatus,
} from "@/components/ask/search-results";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApiError, getJson, type SearchResponse } from "@/lib/api";
import { readSearch, toApiParams, writeSearch } from "@/lib/search-query";

const TABS = [
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

  // 검색은 URL이 바뀔 때 돈다 — 새로고침·뒤로 가기·공유 링크가 같은 경로를 탄다.
  // 화면에 쓰는 검색은 URL을 한 번 정리한 값이다(공백만 있는 q는 검색 없음)
  const key = writeSearch(readSearch(params)).toString();
  const search = readSearch(new URLSearchParams(key));
  // 같은 검색을 다시 누르면 URL이 그대로라 시도 번호로 다시 요청한다(오류 뒤 다시 시도)
  const [attempt, setAttempt] = useState(0);
  const request = `${key}#${attempt}`;

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
    const request = `${key}#${attempt}`;
    if (!s.q) return;
    const ac = new AbortController(); // 늦게 온 옛 응답은 버린다
    getJson<SearchResponse>("/search", toApiParams(s), ac.signal).then(
      (data) => setAnswer({ key: request, data }),
      (e) => {
        if (!ac.signal.aborted)
          setAnswer({
            key: request,
            error:
              e instanceof ApiError ? e.message : "서버에 연결하지 못했습니다",
          });
      },
    );
    return () => ac.abort();
  }, [key, attempt]);

  const status: SearchStatus = !search.q
    ? { kind: "idle" }
    : answer?.key !== request
      ? { kind: "loading" }
      : "error" in answer
        ? { kind: "error", message: answer.error }
        : { kind: "done", data: answer.data };

  const hasResults =
    status.kind === "done" &&
    status.data.manual_results.length + status.data.case_results.length > 0;

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
          onSubmit={() => {
            const next = writeSearch(draft).toString();
            if (next === key) setAttempt((n) => n + 1);
            else router.push(`/ask?${next}`);
          }}
        />
        {hasResults && <SearchSummary search={search} requestKey={request} />}
        <SearchResults status={status} search={search} />
      </TabsContent>
      <TabsContent value="docs">
        <DocsPanel />
      </TabsContent>
    </Tabs>
  );
}
