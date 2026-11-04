// 치식도 — 와이어프레임 teethSvg를 옮긴 것. 치아 종류별 선화(위턱 기준 경로, 아래턱은 뒤집음).
// 진료 기록 있는 치아는 칠하고, 남은 치료계획 치아는 점선
import type { PatientRecord } from "@/lib/emr-api";
import { cn } from "@/lib/utils";

type Kind = "incisor" | "canine" | "premolar" | "molar";
const PATH: Record<Kind, string> = {
  incisor:
    "M.15 .55C.07 .7 .08 .9 .18 .99L.82 .99C.92 .9 .93 .7 .85 .55C.75 .3 .6 .05 .5 .02C.4 .05 .25 .3 .15 .55Z",
  canine:
    "M.15 .55C.05 .72 .12 .88 .5 1C.88 .88 .95 .72 .85 .55C.75 .25 .6 0 .5 0C.4 0 .25 .25 .15 .55Z",
  premolar:
    "M.12 .55C.02 .7 .05 .9 .25 .98Q.38 1 .5 .94Q.62 1 .75 .98C.95 .9 .98 .7 .88 .55C.78 .3 .6 .06 .5 .04C.4 .06 .22 .3 .12 .55Z",
  molar:
    "M.08 .52C0 .68 .02 .9 .18 .97Q.3 1 .4 .94Q.5 1 .6 .94Q.7 1 .82 .97C.98 .9 1 .68 .92 .52C.88 .3 .8 .1 .74 .04C.68 .12 .62 .3 .57 .45Q.5 .4 .43 .45C.38 .3 .32 .12 .26 .04C.2 .1 .12 .3 .08 .52Z",
};
const WIDTH: Record<Kind, number> = {
  incisor: 15,
  canine: 16,
  premolar: 17,
  molar: 23,
};
const ROWS = [
  [18, 17, 16, 15, 14, 13, 12, 11, 21, 22, 23, 24, 25, 26, 27, 28],
  [48, 47, 46, 45, 44, 43, 42, 41, 31, 32, 33, 34, 35, 36, 37, 38],
];
const GAP = 3;
const MID = 14;
const H = 46;

function kind(n: number): Kind {
  const d = n % 10;
  return d <= 2
    ? "incisor"
    : d === 3
      ? "canine"
      : d <= 5
        ? "premolar"
        : "molar";
}

export function Odontogram({ record }: { record: PatientRecord }) {
  const done = new Set(record.treatments.map((t) => t.tooth));
  const plan = new Set(
    record.plans.filter((x) => x.status === "open").map((x) => x.tooth),
  );
  const state = (n: number) =>
    done.has(String(n)) ? "on" : plan.has(String(n)) ? "plan" : "";
  const total = ROWS[0].reduce((t, n) => t + WIDTH[kind(n)] + GAP, 0) + MID;

  const row = (list: number[], upper: boolean) => {
    let x = 2;
    return list.map((n, i) => {
      const w = WIDTH[kind(n)];
      const y = upper ? 2 : 70;
      const transform = upper
        ? `translate(${x} ${y}) scale(${w} ${H})`
        : `translate(${x} ${y + H}) scale(${w} ${-H})`;
      const st = state(n);
      const g = (
        <g key={n}>
          <title>{`${n}번${st === "on" ? " · 진료 기록 있음" : st === "plan" ? " · 남은 치료계획" : ""}`}</title>
          <g transform={transform}>
            <path
              d={PATH[kind(n)]}
              vectorEffect="non-scaling-stroke"
              className={cn(
                "stroke-emr-fg-muted",
                st === "on" ? "fill-emr-tab-off" : "fill-emr-surface",
                st === "plan" && "stroke-emr-title [stroke-dasharray:3_2]",
              )}
            />
          </g>
          <text
            x={x + w / 2}
            y={upper ? y + H + 9 : y - 3}
            textAnchor="middle"
            className="fill-emr-fg-muted text-[7px]"
          >
            {n}
          </text>
        </g>
      );
      x += w + GAP + (i === 7 ? MID : 0);
      return g;
    });
  };

  return (
    <div>
      <svg
        viewBox={`0 0 ${total + 4} 120`}
        role="img"
        aria-label="치식도"
        className="w-full"
      >
        {row(ROWS[0], true)}
        {row(ROWS[1], false)}
        <line
          x1={total / 2 + 2}
          y1={2}
          x2={total / 2 + 2}
          y2={118}
          className="stroke-emr-line"
        />
      </svg>
      <div className="mt-1 flex gap-3 text-emr-fg-muted">
        <span className="flex items-center gap-1">
          <i className="size-2.5 border border-emr-fg-muted bg-emr-tab-off" />
          진료 기록 있음
        </span>
        <span className="flex items-center gap-1">
          <i className="size-2.5 border border-dashed border-emr-title" />
          남은 치료계획
        </span>
      </div>
    </div>
  );
}
