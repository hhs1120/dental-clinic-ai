"use client";

import { Fragment } from "react";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

export type ScreenTab = {
  key: string;
  label: string;
  count?: number; // 있으면 이름 옆 숫자
};

/** 화면 안 큰 탭 — 한 상자 안의 칸(세그먼트), 칸 사이 세로 구분선, 글자를 키우고 고른 칸은 떠 있는 카드로.
 * 처음 보는 사람도 누를 곳으로 알아보게(통화·AI 평가 화면이 같이 쓴다) */
export function ScreenTabs({
  tabs,
  value,
  onChange,
}: {
  tabs: ScreenTab[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <Tabs value={value} onValueChange={onChange}>
      <TabsList className="h-12 gap-1 rounded-xl p-1">
        {tabs.map(({ key, label, count }, i) => (
          <Fragment key={key}>
            {i > 0 && (
              <span
                aria-hidden
                className="h-5 w-px shrink-0 bg-muted-foreground/40"
              />
            )}
            <TabsTrigger
              value={key}
              className="h-full gap-2 rounded-lg px-5 text-base font-bold data-active:shadow-card"
            >
              {label}
              {count !== undefined && (
                <span className="rounded-full bg-primary px-2 text-xs leading-5 text-primary-foreground">
                  {count}
                </span>
              )}
            </TabsTrigger>
          </Fragment>
        ))}
      </TabsList>
    </Tabs>
  );
}
