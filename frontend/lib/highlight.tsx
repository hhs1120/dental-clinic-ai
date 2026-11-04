import type { ReactNode } from "react";

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** 검색어 어절이 본문에 그대로 있으면 표시한다. HTML 문자열을 넣지 않고 React 노드로 만든다 */
export function highlight(text: string, q: string): ReactNode[] {
  const words = [...new Set(q.split(/\s+/).filter(Boolean))].sort(
    (a, b) => b.length - a.length,
  );
  if (!words.length) return [text];
  const pattern = new RegExp(`(${words.map(escape).join("|")})`, "gi");
  // 캡처 그룹으로 나누면 홀수 번째 조각이 맞은 부분이다
  return text.split(pattern).map((part, i) =>
    i % 2 ? (
      <mark key={i} className="rounded-sm bg-mark px-0.5 text-inherit">
        {part}
      </mark>
    ) : (
      part
    ),
  );
}
