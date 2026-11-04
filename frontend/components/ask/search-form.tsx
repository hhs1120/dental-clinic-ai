"use client";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ALL_TYPES, SOURCE_LABEL, type SearchState } from "@/lib/search-query";

export function SearchForm({
  draft,
  onChange,
  onSubmit,
}: {
  draft: SearchState;
  onChange: (next: SearchState) => void;
  onSubmit: () => void;
}) {
  const set = (patch: Partial<SearchState>) => onChange({ ...draft, ...patch });
  const blocked = !draft.q.trim() || draft.types.length === 0;

  return (
    <Card className="gap-4 p-5 shadow-card">
      <form
        aria-label="검색 조건"
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!blocked) onSubmit();
        }}
      >
        <div className="flex flex-wrap gap-2">
          <Input
            type="search"
            aria-label="검색어"
            placeholder="예: 크라운 가격"
            value={draft.q}
            onChange={(e) => set({ q: e.target.value })}
            className="h-11 min-w-0 flex-1 bg-card text-base shadow-control"
          />
          <Button
            type="submit"
            disabled={blocked}
            className="h-11 px-6 font-bold shadow-control"
          >
            찾기
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-x-8 gap-y-4 text-sm text-muted-foreground">
          <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <legend className="float-left mr-2 font-medium text-foreground">
              출처
            </legend>
            {ALL_TYPES.map((t) => (
              <div key={t} className="flex items-center gap-1.5">
                <Checkbox
                  id={`type-${t}`}
                  checked={draft.types.includes(t)}
                  onCheckedChange={(on) =>
                    set({
                      types: on
                        ? ALL_TYPES.filter(
                            (x) => x === t || draft.types.includes(x),
                          )
                        : draft.types.filter((x) => x !== t),
                    })
                  }
                />
                <Label htmlFor={`type-${t}`} className="font-normal">
                  {SOURCE_LABEL[t]}
                </Label>
              </div>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-foreground">기간</span>
            <Input
              type="date"
              aria-label="시작일"
              value={draft.from}
              onChange={(e) => set({ from: e.target.value })}
              className="w-auto bg-card shadow-control"
            />
            <span>~</span>
            <Input
              type="date"
              aria-label="종료일"
              value={draft.to}
              onChange={(e) => set({ to: e.target.value })}
              className="w-auto bg-card shadow-control"
            />
          </div>
          <div className="flex items-center gap-2">
            <Label htmlFor="chart-no" className="font-medium text-foreground">
              환자번호
            </Label>
            <Input
              id="chart-no"
              placeholder="C-00000"
              value={draft.chart}
              onChange={(e) => set({ chart: e.target.value })}
              className="w-32 bg-card shadow-control"
            />
          </div>
          {draft.types.length === 0 && (
            <span role="status">출처를 하나 이상 고르세요</span>
          )}
        </div>
      </form>
    </Card>
  );
}
