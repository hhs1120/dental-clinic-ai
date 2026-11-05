"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { AddCallDialog } from "@/components/calls/add-call-dialog";
import { CallDetail } from "@/components/calls/call-detail";
import { CallHistory } from "@/components/calls/call-history";
import { TodayBoard } from "@/components/calls/today-board";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorText, useFetch } from "@/lib/api";
import {
  fetchBoard,
  fetchCall,
  reclassify,
  setTodoDone,
  useLabels,
} from "@/lib/calls-api";
import { readPart } from "@/lib/part";

const TABS = [
  ["today", "오늘"],
  ["history", "통화 기록"],
] as const;

/** 통화 화면 — 탭·고른 통화·넓게 보기는 URL에 둔다(/calls?tab=…&call=…&wide=1) */
export function CallsView() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "history" ? "history" : "today";
  const call = params.get("call");
  const wide = params.get("wide") === "1" && call !== null;
  const go = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    // 고른 통화·넓게 보기는 같은 화면 안의 상태라 스크롤을 맨 위로 올리지 않는다
    router.push(`/calls${next.size ? `?${next}` : ""}`, { scroll: false });
  };
  const open = (uid: string) => go({ call: uid });
  const names = useLabels();
  const board = useFetch(tab === "today" ? "board" : null, (s) =>
    fetchBoard(s),
  );
  const detail = useFetch(call, (s) => fetchCall(call as string, s));
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [fail, setFail] = useState<string | null>(null);
  const refresh = () => {
    board.reload();
    detail.reload();
    setVersion((v) => v + 1);
  };
  const check = async (uid: string, item: number, done: boolean) => {
    setFail(null);
    try {
      await setTodoDone(uid, item, done, readPart());
      refresh();
    } catch (e) {
      setFail(errorText(e));
    }
  };
  const redo = async () => {
    if (!call) return;
    setBusy(true);
    setFail(null);
    try {
      await reclassify(call);
      refresh();
    } catch (e) {
      setFail(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  const panel =
    call &&
    (detail.error ? (
      <p className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
        {detail.error}
        <Button variant="ghost" size="sm" onClick={() => go({ call: null })}>
          닫기
        </Button>
      </p>
    ) : detail.data ? (
      <CallDetail
        detail={detail.data}
        names={names}
        wide={wide}
        onWide={(w) => go({ wide: w ? "1" : null })}
        onClose={() => go({ call: null, wide: null })}
        onCheck={(t, done) => check(call, t.item, done)}
        onReclassify={redo}
        busy={busy}
      />
    ) : (
      <p className="p-4 text-sm text-muted-foreground">불러오는 중…</p>
    ));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Tabs
          value={tab}
          onValueChange={(v) =>
            go({ tab: v === "today" ? null : v, call: null, wide: null })
          }
        >
          {/* 처음 보는 사람도 누를 곳으로 알아보게 버튼 모양 두 칸 — 고른 쪽은 채운다 */}
          <TabsList className="h-11 gap-1 bg-transparent p-0">
            {TABS.map(([k, l]) => (
              <TabsTrigger
                key={k}
                value={k}
                className="h-11 rounded-lg border border-border bg-card px-5 text-base font-bold shadow-control hover:bg-muted data-active:border-primary data-active:bg-primary data-active:text-primary-foreground dark:data-active:border-primary dark:data-active:bg-primary"
              >
                {l}
                {k === "today" && board.data && (
                  <span className="rounded-full bg-background px-1.5 text-xs text-foreground">
                    {board.data.internal.length + board.data.contact.length}
                  </span>
                )}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <span className="flex-1" />
        <AddCallDialog
          onAdded={(uid) => {
            refresh();
            open(uid);
          }}
        />
      </div>
      {fail && (
        <p className="rounded-lg border border-status-danger-border bg-status-danger-bg px-3 py-2 text-sm text-status-danger-fg">
          {fail}
        </p>
      )}
      {wide ? (
        panel
      ) : (
        <div
          className={
            call ? "grid items-start gap-4 lg:grid-cols-[1fr_28rem]" : ""
          }
        >
          <div className="min-w-0">
            {tab === "today" ? (
              board.error ? (
                <p className="text-sm text-muted-foreground">{board.error}</p>
              ) : board.data ? (
                <TodayBoard
                  board={board.data}
                  selected={call}
                  onOpen={open}
                  onCheck={(it, done) => check(it.call_uid, it.item, done)}
                />
              ) : (
                <p className="py-10 text-center text-sm text-muted-foreground">
                  불러오는 중…
                </p>
              )
            ) : (
              <CallHistory
                names={names}
                selected={call}
                onOpen={open}
                version={version}
              />
            )}
          </div>
          {panel && <div className="lg:sticky lg:top-4">{panel}</div>}
        </div>
      )}
    </div>
  );
}
