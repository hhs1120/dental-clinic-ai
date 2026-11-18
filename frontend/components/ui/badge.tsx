import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Slot } from "radix-ui";

const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-4xl border border-transparent px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a]:hover:bg-primary/80",
        secondary:
          "bg-secondary text-secondary-foreground [a]:hover:bg-secondary/80",
        destructive:
          "bg-destructive/10 text-destructive focus-visible:ring-destructive/20 dark:bg-destructive/20 dark:focus-visible:ring-destructive/40 [a]:hover:bg-destructive/20",
        outline:
          "border-border text-foreground [a]:hover:bg-muted [a]:hover:text-muted-foreground",
        ghost:
          "hover:bg-muted hover:text-muted-foreground dark:hover:bg-muted/50",
        link: "text-primary underline-offset-4 hover:underline",
        // 앱 뼈대 설계안 '스타일' — 종류 표시, 옛 버전, 상태 4종(진한 배경 + 같은 색 테두리 + 점 + 굵은 글자)
        kind: "rounded-md bg-accent font-bold text-accent-foreground",
        muted: "rounded-md bg-secondary font-bold text-secondary-foreground",
        outdated:
          "rounded-md border-dashed border-muted-foreground text-muted-foreground",
        warn: "border-status-warn-border bg-status-warn-bg font-bold text-status-warn-fg",
        danger:
          "border-status-danger-border bg-status-danger-bg font-bold text-status-danger-fg",
        ok: "border-status-ok-border bg-status-ok-bg font-bold text-status-ok-fg",
        info: "border-status-info-border bg-status-info-bg font-bold text-status-info-fg",
        // AI 비서 답 아래 바로 가기·사례 출처 칩 — 이름과 링크 여러 개가 들어가 줄바꿈된다
        chip: "h-auto flex-wrap justify-start gap-x-2 overflow-visible rounded-md border-border bg-card px-2.5 py-1 font-normal whitespace-normal",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

const DOT = {
  warn: "bg-status-warn-dot",
  danger: "bg-status-danger-dot",
  ok: "bg-status-ok-dot",
  info: "bg-status-info-dot",
} as const;

type Status = keyof typeof DOT;

/** 상태 표시의 점 — 배지와 안내 띠가 함께 쓴다 */
function StatusDot({ status }: { status: Status }) {
  return (
    <span
      aria-hidden="true"
      className={cn("size-1.5 shrink-0 rounded-full", DOT[status])}
    />
  );
}

function Badge({
  className,
  variant = "default",
  asChild = false,
  children,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot.Root : "span";

  return (
    <Comp
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    >
      {variant && variant in DOT && <StatusDot status={variant as Status} />}
      {children}
    </Comp>
  );
}

export { Badge, badgeVariants, StatusDot };
