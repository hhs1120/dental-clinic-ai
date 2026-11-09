"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { DocBody } from "@/components/ask/doc-body";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  getJson,
  useFetch,
  type DocumentDetail,
  type DocumentItem,
  type DocType,
} from "@/lib/api";
import { DOC_TYPE, DOC_TYPE_ORDER, docHref } from "@/lib/doc-types";
import { cn } from "@/lib/utils";

const Note = ({ children }: { children: React.ReactNode }) => (
  <p className="py-6 text-center text-sm text-muted-foreground">{children}</p>
);

/** 문서함 — 종류 칩으로 한 종류씩, 공지는 최신 버전만·시행일 표시, 읽기 전용(기능 정의 §4 ③, 설계 2026-11-09 검색 요약 "화면").
 * 상태는 URL(?doc=)에 둔다 — 검색 카드·요약 출처에서 바로 연다 */
export function DocsPanel() {
  const params = useSearchParams();
  const router = useRouter();
  const docParam = Number(params.get("doc")) || null;

  const list = useFetch("documents", (signal) =>
    getJson<{ documents: DocumentItem[] }>("/documents", undefined, signal),
  );
  const docs = list.data?.documents ?? [];
  const firstNotice = docs.find((d) => d.type === "notice")?.id ?? null;
  const openId = docParam ?? firstNotice;
  const detail = useFetch(openId === null ? null : `doc:${openId}`, (signal) =>
    getJson<DocumentDetail>(`/documents/${openId}`, undefined, signal),
  );
  const open = detail.data;

  // 칩은 열린 문서의 종류를 따른다(옛 공지도 공지) — 문서가 오기 전에는 공지
  const type: DocType = open?.type ?? "notice";
  const ofType = (t: DocType) => docs.filter((d) => d.type === t);
  const shown = ofType(type);
  const latest = open && open.versions.find((v) => v.id === open.latest_id);

  if (list.error) return <Note>{list.error}</Note>;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        {DOC_TYPE_ORDER.map((t) => (
          <Button
            key={t}
            size="sm"
            variant={t === type ? "secondary" : "outline"}
            aria-pressed={t === type}
            // 목록이 오기 전이나 문서가 없는 종류는 누를 수 없다 — 눌러도 열 문서가 없다
            disabled={ofType(t).length === 0}
            onClick={() => {
              const first = ofType(t)[0];
              if (first) router.replace(docHref(first.id));
            }}
          >
            {DOC_TYPE[t]} {ofType(t).length}
          </Button>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-[20rem_minmax(0,1fr)]">
        <Card className="max-h-[45rem] gap-0 overflow-y-auto p-2 shadow-card">
          <nav aria-label="문서 목록" className="flex flex-col">
            <p className="px-3 pt-2 pb-1 text-xs font-bold text-muted-foreground">
              {DOC_TYPE[type]} {shown.length}
              {type === "notice" && " · 최신 버전만"}
            </p>
            {shown.map((d) => (
              <Link
                key={d.id}
                href={docHref(d.id)}
                replace
                aria-current={d.id === open?.latest_id ? "page" : undefined}
                className={cn(
                  "rounded-md px-3 py-2 text-sm hover:bg-muted",
                  d.id === open?.latest_id &&
                    "bg-accent text-accent-foreground",
                )}
              >
                {d.title}
                {d.type === "notice" && (
                  <span className="text-xs text-muted-foreground">
                    {" "}
                    · 시행 {d.effective_date}
                  </span>
                )}
              </Link>
            ))}
          </nav>
        </Card>
        <Card className="gap-3 p-6 shadow-card">
          <article className="flex flex-col gap-3">
            {detail.error && <Note>{detail.error}</Note>}
            {!open && !detail.error && <Note>불러오는 중…</Note>}
            {open && (
              <>
                {open.id !== open.latest_id && latest && (
                  <Link
                    href={docHref(open.latest_id)}
                    replace
                    className="self-start text-sm font-medium text-link hover:underline"
                  >
                    옛 버전 — 최신 v{latest.version} 보기
                  </Link>
                )}
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="kind">{DOC_TYPE[open.type]}</Badge>
                  <Badge variant="muted">v{open.version}</Badge>
                  {open.effective_date && (
                    <Badge variant="outline">시행 {open.effective_date}</Badge>
                  )}
                  <span className="ml-auto">읽기 전용 · doc:{open.id}</span>
                </div>
                <DocBody markdown={open.body} />
                {open.versions.length > 1 && (
                  <p className="border-t border-dashed border-border pt-2 text-xs text-muted-foreground">
                    버전{" "}
                    {open.versions.map((v, i) => (
                      <span key={v.id}>
                        {i > 0 && " · "}
                        {v.id === open.id ? (
                          <b>v{v.version}</b>
                        ) : (
                          <Link
                            href={docHref(v.id)}
                            replace
                            className="font-medium text-link hover:underline"
                          >
                            v{v.version}
                          </Link>
                        )}{" "}
                        (시행 {v.effective_date})
                      </span>
                    ))}{" "}
                    — 검색·답변은 최신 버전을 씁니다
                  </p>
                )}
              </>
            )}
          </article>
        </Card>
      </div>
    </div>
  );
}
