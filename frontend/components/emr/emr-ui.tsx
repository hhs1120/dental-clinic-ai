// 전환 전 EMR 창의 옛 프로그램 부품 — 각진 모서리·회청색 띠·12px. 앱의 shadcn 부품을 쓰지 않는 것은 의도다
// (옛 병원 프로그램처럼 보여야 한다, 전환 전 EMR·일별 예약표 설계안). 색은 --emr-* 토큰만 쓴다
import { cn } from "@/lib/utils";

export const MSG_TRIG: Record<string, string> = {
  appt_confirm: "예약 확정",
  reminder: "리마인드",
  reply: "환자 답장",
  recall: "리콜",
  balance: "미수 안내",
  noshow: "노쇼",
  doc_ready: "서류 준비",
  other: "기타",
};
export const PAY_KIND: Record<string, string> = {
  visit: "진료비",
  settlement: "미수 정산",
};
export const PAY_METHOD: Record<string, string> = {
  card: "카드",
  cash: "현금",
  transfer: "계좌이체",
};
export const CLAIM_ST: Record<string, string> = {
  paid: "지급",
  pending: "청구 전",
  submitted: "심사 중",
  adjusted: "조정",
  denied: "불능",
};
export const PLAN_ST: Record<string, string> = {
  open: "미시행",
  done: "완료",
  replaced: "대체",
};

export const commas = (n: number) => n.toLocaleString("ko-KR");

export function Box({
  title,
  className,
  highlight,
  children,
}: {
  title?: string;
  className?: string;
  highlight?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section
      className={cn(
        "min-w-0 border border-emr-line bg-emr-surface p-1.5",
        highlight && "relative z-10 ring-2 ring-primary ring-offset-2",
        className,
      )}
    >
      {title && <h3 className="mb-1 text-xs font-bold">{title}</h3>}
      {children}
    </section>
  );
}

export function Tabs<T extends string>({
  items,
  value,
  onChange,
}: {
  items: readonly T[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div role="tablist" className="flex flex-wrap">
      {items.map((t) => (
        <button
          key={t}
          type="button"
          role="tab"
          aria-selected={value === t}
          onClick={() => onChange(t)}
          className={cn(
            "-mr-px border border-b-0 border-emr-line px-2.5 py-0.5 text-xs",
            value === t
              ? "bg-emr-surface font-bold"
              : "bg-emr-tab-off text-emr-fg-muted",
          )}
        >
          {t}
        </button>
      ))}
    </div>
  );
}

export function Grid({
  heads,
  rows,
  wrapLast,
}: {
  heads: string[];
  rows: React.ReactNode[][];
  wrapLast?: boolean;
}) {
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr>
          {heads.map((h) => (
            <th
              key={h}
              className="border border-emr-line-soft bg-emr-chrome px-1.5 py-0.5 text-left font-semibold whitespace-nowrap"
            >
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.length ? (
          rows.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td
                  key={j}
                  className={cn(
                    "border border-emr-line-soft px-1.5 py-0.5 align-top",
                    wrapLast && j === r.length - 1
                      ? "min-w-72 whitespace-normal"
                      : "whitespace-nowrap",
                    typeof c === "number" && "text-right tabular-nums",
                  )}
                >
                  {typeof c === "number" ? commas(c) : c}
                </td>
              ))}
            </tr>
          ))
        ) : (
          <tr>
            <td
              colSpan={heads.length}
              className="border border-emr-line-soft px-1.5 py-0.5 text-emr-fg-muted"
            >
              자료 없음
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
