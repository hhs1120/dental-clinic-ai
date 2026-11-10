"use client";

import { useRouter, useSearchParams } from "next/navigation";

import { AdjustTable } from "@/components/claims/adjust-table";
import { PreClaimTable } from "@/components/claims/pre-claim-table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const TABS = [
  { value: "adjust", label: "삭감 대응" },
  { value: "pre", label: "청구 전 점검" },
];

/** ⑤ 청구 점검 — 탭·칩은 URL에 둔다(/claims?tab=adjust|pre&chip=…). 기본은 삭감 대응(기한이 걸린 일) */
export function ClaimsView() {
  const params = useSearchParams();
  const router = useRouter();
  const tab = params.get("tab") === "pre" ? "pre" : "adjust";
  const chip = params.get("chip");
  const go = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    router.replace(`/claims${next.size ? `?${next}` : ""}`);
  };

  return (
    <Tabs
      value={tab}
      // 탭을 바꾸면 그 탭의 기본 칩으로 — 칩 이름이 탭마다 다르다
      onValueChange={(v) => go({ tab: v === "adjust" ? null : v, chip: null })}
      className="gap-4"
    >
      <TabsList variant="line" className="w-full justify-start border-b">
        {TABS.map((t) => (
          <TabsTrigger key={t.value} value={t.value} className="flex-none px-4">
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="adjust">
        {tab === "adjust" && (
          <AdjustTable chip={chip} onChip={(c) => go({ chip: c })} />
        )}
      </TabsContent>
      <TabsContent value="pre">
        {tab === "pre" && (
          <PreClaimTable chip={chip} onChip={(c) => go({ chip: c })} />
        )}
      </TabsContent>
    </Tabs>
  );
}
