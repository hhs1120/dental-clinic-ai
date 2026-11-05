import { transcriptLines } from "@/lib/calls-api";
import { cn } from "@/lib/utils";

/** 녹취록 — 줄 목록 또는 말풍선(직원 왼쪽, 상대 오른쪽). 통화 상세·판정·블라인드가 같이 쓴다 */
export function Transcript({
  text,
  bubbles,
}: {
  text: string;
  bubbles: boolean;
}) {
  const lines = transcriptLines(text);
  if (!bubbles)
    return (
      <div className="flex flex-col gap-1 text-sm">
        {lines.map((l, i) => (
          <p key={i}>
            <b className="mr-2 inline-block w-8 text-muted-foreground">
              {l.speaker}
            </b>
            {l.text}
          </p>
        ))}
      </div>
    );
  return (
    <div className="flex flex-col gap-1.5 text-sm">
      {lines.map((l, i) => {
        const staff = l.speaker === "직원";
        return (
          <p
            key={i}
            className={cn(
              "max-w-[85%] rounded-xl px-3 py-1.5",
              staff ? "bg-accent text-accent-foreground" : "ml-auto bg-muted",
            )}
          >
            {!staff && l.speaker && l.speaker !== "환자" && (
              <span className="mr-1 text-xs text-muted-foreground">
                {l.speaker}
              </span>
            )}
            {l.text}
          </p>
        );
      })}
    </div>
  );
}
