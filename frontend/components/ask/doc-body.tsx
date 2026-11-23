import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// 문서 본문은 마크다운(제목·목록·표·굵게). 원시 HTML은 그리지 않는다(react-markdown 기본값) — 설계 2026-11-09 검색 요약 "화면"
const COMPONENTS: Components = {
  h1: ({ children }) => (
    <h2 className="mb-3 text-xl font-medium tracking-tight">{children}</h2>
  ),
  h2: ({ children }) => (
    <h3 className="mt-6 mb-2 text-base font-medium tracking-tight">
      {children}
    </h3>
  ),
  h3: ({ children }) => <h4 className="mt-4 mb-1.5 font-bold">{children}</h4>,
  p: ({ children }) => <p className="my-2 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc pl-5">{children}</ul>,
  // 발췌는 "4."처럼 중간 번호로 시작하기도 해서 시작 번호를 넘긴다
  ol: ({ children, start }) => (
    <ol start={start} className="my-2 list-decimal pl-5">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="my-1 leading-relaxed">{children}</li>,
  // 청구 기준의 실제 고시 발췌(generator/claim_standards.py)
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-border pl-3 text-muted-foreground">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-border bg-muted px-3 py-1.5 text-left font-bold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-border px-3 py-1.5 align-top">{children}</td>
  ),
};

// 청구 기준의 고시 발췌는 뺀 곳을 "…"로 적는다(generator/claim_standards.py). 화면에서는 관례 표기 "(중략)"으로 보이고
// 본문 아래에 발췌 안내 한 줄을 붙인다 — 문서 원문(색인·평가가 읽는 값)은 그대로
const CUT = "…";
export const showCuts = (body: string) => body.replaceAll(CUT, "(중략)");
export const hasCuts = (body: string) => body.includes(CUT);

export function ExcerptNote() {
  return (
    <p className="mt-2 text-xs text-muted-foreground">
      고시 원문에서 필요한 부분만 옮겼습니다. (중략)은 생략한 부분입니다.
    </p>
  );
}

export function DocBody({
  markdown,
  className = "max-w-prose",
}: {
  markdown: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <Markdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {showCuts(markdown)}
      </Markdown>
      {hasCuts(markdown) && <ExcerptNote />}
    </div>
  );
}
