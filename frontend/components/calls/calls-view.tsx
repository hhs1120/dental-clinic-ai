"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { AddCallDialog } from "@/components/calls/add-call-dialog";
import { CallDetail } from "@/components/calls/call-detail";
import { CallHistory } from "@/components/calls/call-history";
import { ScreenTabs } from "@/components/screen-tabs";
import { TodayBoard } from "@/components/calls/today-board";
import { Button } from "@/components/ui/button";
import { ApiError, errorText, useFetch } from "@/lib/api";
import {
  fetchBoard,
  fetchCall,
  reclassify,
  revertAuto,
  setTodoDone,
  useLabels,
} from "@/lib/calls-api";
import { readPart } from "@/lib/part";
import { checkCall } from "@/lib/today-api";

/** 통화 화면 — 탭·고른 통화·넓게 보기는 URL에 둔다(/calls?tab=…&call=…&wide=1) */
export function CallsView() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "history" ? "history" : "today";
  const call = params.get("call");
  const wide = params.get("wide") === "1" && call !== null;
  // 좁은 화면(lg 미만)에서 통화를 열면 위에 놓인 상세로 옮긴다 — 넓은 화면은 옆에 붙어 있어 그대로
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (call && !matchMedia("(min-width: 64rem)").matches)
      panelRef.current?.scrollIntoView({ block: "start" });
  }, [call]);
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
  // 통화 기록 탭에서도 불러온다 — "오늘 할 일" 탭의 할 일 수가 사라지지 않게
  const board = useFetch("board", (s) => fetchBoard(s));
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
  const revertClose = async (id: number) => {
    setBusy(true);
    setFail(null);
    try {
      await revertAuto(id, readPart());
      refresh();
    } catch (e) {
      setFail(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const checkReview = async () => {
    if (!call) return;
    setBusy(true);
    setFail(null);
    try {
      await checkCall(call, readPart());
      refresh();
    } catch (e) {
      // 404 = 이미 확인할 통화가 아님(다른 탭에서 먼저 확인함) — 다시 열기처럼 오류 없이 다시 불러온다
      if (e instanceof ApiError && e.status === 404) refresh();
      else setFail(errorText(e));
    } finally {
      setBusy(false);
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
      <p className="rounded-xl border bg-card p-4 text-sm text-muted-foreground">
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
        onRevertClose={revertClose}
        onCheckReview={checkReview}
        onOpen={open}
        busy={busy}
      />
    ) : (
      <p className="p-4 text-sm text-muted-foreground">불러오는 중…</p>
    ));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <ScreenTabs
          tabs={[
            {
              key: "today",
              label: "오늘 할 일",
              count: board.data
                ? board.data.internal.length + board.data.contact.length
                : undefined,
            },
            { key: "history", label: "통화 기록" },
          ]}
          value={tab}
          onChange={(v) =>
            go({
              tab: v === "today" ? null : v,
              call: null,
              wide: null,
              q: null,
            })
          }
        />
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
                  names={names}
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
                initialQ={params.get("q") ?? ""}
                onTyped={() => params.get("q") && go({ q: null })}
                names={names}
                selected={call}
                onOpen={open}
                version={version}
              />
            )}
          </div>
          {panel && (
            // 좁은 화면은 상세를 목록 위에 두고 열 때 그리로 옮긴다(목록 아래 멀리 열리지 않게)
            <div
              ref={panelRef}
              className="order-first scroll-mt-4 md:scroll-mt-[calc(var(--page-band-h,0px)+1rem)] lg:sticky lg:top-[calc(var(--page-band-h,0px)+1rem)] lg:order-none"
            >
              {panel}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
