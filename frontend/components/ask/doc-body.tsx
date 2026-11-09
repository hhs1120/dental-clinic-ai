import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// 문서 본문은 마크다운(제목·목록·표·굵게). 원시 HTML은 그리지 않는다(react-markdown 기본값) — 설계 2026-11-09 검색 요약 "화면"
const COMPONENTS: Components = {
  h1: ({ children }) => <h2 className="mb-3 text-xl font-bold">{children}</h2>,
  h2: ({ children }) => (
    <h3 className="mt-6 mb-2 text-base font-bold">{children}</h3>
  ),
  h3: ({ children }) => <h4 className="mt-4 mb-1.5 font-bold">{children}</h4>,
  p: ({ children }) => <p className="my-2 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal pl-5">{children}</ol>,
  li: ({ children }) => <li className="my-1 leading-relaxed">{children}</li>,
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

export function DocBody({ markdown }: { markdown: string }) {
  return (
    <div className="max-w-prose">
      <Markdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {markdown}
      </Markdown>
    </div>
  );
}
