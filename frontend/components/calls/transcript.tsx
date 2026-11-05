import { Badge } from "@/components/ui/badge";
import { transcriptLines } from "@/lib/calls-api";
import { cn } from "@/lib/utils";

/** 녹취록 — 줄 목록 또는 말풍선(직원 왼쪽, 상대 오른쪽). 통화 상세·판정·블라인드가 같이 쓴다.
 * 직원과 상대는 이름표 색·줄 바탕(목록)과 위치·색(말풍선)으로 가른다 */
export function Transcript({
  text,
  bubbles,
  counterpart = null,
}: {
  text: string;
  bubbles: boolean;
  counterpart?: string | null; // 상대 이름 — 말풍선 아래 "김지훈(환자)"
}) {
  const lines = transcriptLines(text);
  if (!bubbles)
    return (
      <div className="flex flex-col gap-0.5 text-sm">
        {lines.map((l, i) => {
          const staff = l.speaker === "직원";
          return (
            <p
              key={i}
              className={cn(
                "flex items-start gap-2 rounded-md px-2 py-1",
                staff && "bg-accent/40",
              )}
            >
              <Badge
                variant={staff ? "kind" : "muted"}
                className="mt-0.5 w-11 shrink-0"
              >
                {l.speaker || "—"}
              </Badge>
              <span>{l.text}</span>
            </p>
          );
        })}
      </div>
    );
  // 메신저처럼 화자가 바뀔 때 말풍선 아래에 작은 이름 — 색은 말풍선과 다르게(직원 브랜드색·상대 정보색)
  const who = (speaker: string) =>
    speaker === "환자" && counterpart ? `${counterpart}(환자)` : speaker || "—";
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      {lines.map((l, i) => {
        const staff = l.speaker === "직원";
        const last =
          i === lines.length - 1 || lines[i + 1].speaker !== l.speaker;
        return (
          <div
            key={i}
            className={cn(
              "flex max-w-[85%] flex-col gap-0.5",
              !staff && "ml-auto items-end",
            )}
          >
            <p
              className={cn(
                "rounded-xl px-3 py-1.5",
                staff ? "bg-accent text-accent-foreground" : "bg-muted",
              )}
            >
              {l.text}
            </p>
            {last && (
              <span
                className={cn(
                  "px-1 text-xs font-bold",
                  staff ? "text-primary" : "text-status-info-fg",
                )}
              >
                {who(l.speaker)}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
