"use client";

import { ChevronsRightIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { saveListCollapsed } from "@/lib/theme";
import { cn } from "@/lib/utils";

/** 접은 목록 칸을 펴는 단추 — 넓은 화면에서 목록 칸이 접혔을 때만 보인다.
 * 업무·자료실·AI 평가는 제목 띠 안, AI 비서는 본문 왼쪽 위(셸) */
export function ListExpandButton({ className }: { className?: string }) {
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label="목록 펴기"
      title="목록 펴기"
      onClick={() => saveListCollapsed(false)}
      className={cn(
        "hidden text-muted-foreground md:list-collapsed:inline-flex print:hidden!",
        className,
      )}
    >
      <ChevronsRightIcon />
    </Button>
  );
}
