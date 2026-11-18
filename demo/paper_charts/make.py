"""시연용 옛 종이 차트 PNG — charts.json 한 건마다 template.html을 채워 Playwright CLI(frontend 설치본, 설치된 Chrome)로
찍는다(차트 OCR 설계 §설계 1). 동네 치과에서 파는 두 쪽 펼침 차트 구성(왼쪽 구강 검사·C.C·P.I·치료 계획·수납, 오른쪽
진료 경과 줄 공책 — 위치 칸에 점선 십자)을 따르고 인쇄 양식 위에 손글씨 글꼴로 기입한다. 흔들림·얼룩은 차트 번호로
시드를 고정해 실행마다 같다. 생성기·data/·정답과 무관한 고정 샘플이다."""

import html
import json
import random
import subprocess
import tempfile
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parents[1] / "frontend"
WIDTH, HEIGHT = 2400, 1700  # 두 쪽 펼침(가로)
PROGRESS_ROWS = 34  # 진료 경과 줄 수
LEDGER_ROWS = 9  # 수납 장부 한 칸의 줄 수(장부 2칸)
SIGN, DESK_SIGN = "강", "오"  # 원장 서명 — 스케일링 줄은 visit.sign(위생사), 수납은 데스크
QUADRANT = {"┘": 1, "└": 2, "┌": 3, "┐": 4}  # 십자 기호 → 사분면(FDI 첫 자리)
TOOTH_W = {8: 40, 7: 42, 6: 44, 5: 34, 4: 34, 3: 32, 2: 28, 1: 34}  # 치아 그림 폭(px)
MID = 340  # 치아 그림 가운데 선


def digits(rng: random.Random, text: str) -> str:
    """숫자는 한 자씩 크기·높이·기울기를 살짝 흔든다 — 같은 숫자가 똑같은 모양이면 인쇄체처럼 보인다"""
    return "".join(
        f'<span class="d" style="font-size:{rng.uniform(0.94, 1.06):.2f}em;'
        f"transform:translateY({rng.uniform(-1.2, 1.2):.1f}px)"
        f' rotate({rng.uniform(-3.5, 3.5):.1f}deg)">{ch}</span>'
        if ch.isdigit()
        else html.escape(ch)
        for ch in text
    )


def hw(rng: random.Random, text: str, size: int = 34, cls: str = "") -> str:
    """손글씨 한 덩어리 — 크기·기울기·위치를 조금씩 흔든다"""
    return (
        f'<span class="hw {cls}" style="font-size:{size + rng.randint(-3, 3)}px;'
        f"transform:translate({rng.randint(-3, 8)}px,{rng.randint(-3, 2)}px)"
        f' rotate({rng.uniform(-2.0, 2.0):.1f}deg)">{digits(rng, text)}</span>'
    )


def lines(rng: random.Random, rows: list[str], size: int = 34) -> str:
    return "".join(hw(rng, r, size) for r in rows)


def written_date(iso: str, fmt: str | None = None) -> str:
    """기본 12.3.14, short 3/21(같은 해), ditto 〃(윗줄과 같은 날)"""
    y, m, d = iso.split("-")
    if fmt == "ditto":
        return "〃"
    if fmt == "short":
        return f"{int(m)}/{int(d)}"
    return f"{y[2:]}.{int(m)}.{int(d)}"


def tooth_x(q: int, n: int) -> float:
    """치아 그림에서 FDI 치아의 가운데 x — 1·4 사분면은 왼쪽(8이 바깥), 2·3은 오른쪽"""
    inner = sum(TOOTH_W[k] + 2 for k in range(1, n))
    half = TOOTH_W[n] / 2
    return MID - inner - half - 1 if q in (1, 4) else MID + inner + half + 1


def tooth_svg(x: float, w: float, upper: bool, roots: int) -> str:
    """치아 하나 — 머리(둥근 사각형) + 뿌리(가늘어지는 다각형). 위턱은 뿌리가 위로"""
    crown_y, crown_h, root_len = (112, 46, 78) if upper else (300, 46, 78)
    out = [
        f'<rect x="{x - w / 2 + 2:.1f}" y="{crown_y}" width="{w - 4:.1f}" height="{crown_h}"'
        ' rx="9" fill="none" stroke="#555" stroke-width="1.6"/>'
    ]
    for i in range(roots):
        cx = x + (i - (roots - 1) / 2) * w * 0.32
        base = crown_y if upper else crown_y + crown_h
        tip = base - root_len if upper else base + root_len
        rw = w * (0.17 if roots > 1 else 0.24)
        out.append(
            f'<path d="M{cx - rw:.1f},{base} Q{cx - rw * 0.6:.1f},{(base + tip) / 2:.1f}'
            f' {cx:.1f},{tip} Q{cx + rw * 0.6:.1f},{(base + tip) / 2:.1f} {cx + rw:.1f},{base}"'
            ' fill="none" stroke="#555" stroke-width="1.6"/>'
        )
    return "".join(out)


def teeth(rng: random.Random, marks: list[dict]) -> str:
    """구강 검사 치아 그림 — 영구치 위아래 + 번호 줄 8~1|1~8, 가운데 유치 E~A 동그라미, 손 표시"""
    parts = []
    for q in (1, 2, 4, 3):
        upper = q in (1, 2)
        for n in range(1, 9):
            x = tooth_x(q, n)
            parts.append(tooth_svg(x, TOOTH_W[n], upper, 2 if n >= 6 else 1))
            y = 186 if upper else 286
            parts.append(f'<text x="{x:.1f}" y="{y}" class="num">{n}</text>')
        for i, letter in enumerate("ABCDE"):
            x = tooth_x(q, i + 1)
            y = 216 if upper else 256
            parts.append(f'<circle cx="{x:.1f}" cy="{y}" r="9" fill="none" stroke="#777"/>')
            if upper:
                parts.append(f'<text x="{x:.1f}" y="241" class="pri">{letter}</text>')
    for m in marks:
        q, n = int(m["tooth"][0]), int(m["tooth"][1])
        y = 140 if q in (1, 2) else 328
        parts.append(
            f'<text x="{tooth_x(q, n) + rng.randint(-3, 3):.1f}" y="{y + 14}" class="mark"'
            f' transform="rotate({rng.randint(-8, 8)} {tooth_x(q, n):.1f} {y})">'
            f"{html.escape(m['mark'])}</text>"
        )
    return (
        '<svg viewBox="0 0 680 470" width="100%" style="margin-top:4px">'
        "<style>.num{font:15px 'Noto Sans KR';fill:#444;text-anchor:middle}"
        ".pri{font:12px 'Noto Sans KR';fill:#777;text-anchor:middle}"
        ".mark{font:46px 'Caveat','Nanum Pen Script';fill:#1d2f6b;text-anchor:middle}</style>"
        f'<line x1="{MID}" y1="20" x2="{MID}" y2="450" stroke="#555" stroke-width="1.4"'
        ' stroke-dasharray="4 4"/>'
        '<line x1="20" y1="236" x2="660" y2="236" stroke="#777" stroke-dasharray="3 5"/>'
        f"{''.join(parts)}</svg>"
    )


def location(rng: random.Random, tooth: str, old: str | None) -> str:
    """위치 칸 — 인쇄된 점선 십자의 사분면에 치아 번호, 전악은 가운데. 고친 칸은 지운 번호를 먼저"""

    def place(t: str) -> tuple[str, str]:
        sym = next((c for c in t if c in QUADRANT), None)
        return (f"q{QUADRANT[sym]}", t.replace(sym, "").strip()) if sym else ("q0", t)

    def small(t: str, cls: str = "") -> str:  # 칸 반쪽에 들어가게 — 흔들림 없이 크기만
        return f'<span class="hw {cls}" style="font-size:{rng.randint(29, 32)}px">{html.escape(t)}</span>'

    by_q: dict[str, list[str]] = {}
    if old:
        q, t = place(old)
        by_q.setdefault(q, []).append(
            small(t, "struck") if q != "q0" else hw(rng, t, 30, "struck")
        )
    q, t = place(tooth)
    by_q.setdefault(q, []).append(small(t) if q != "q0" else hw(rng, t, 30))
    spans = "".join(f'<span class="q {q}">{"".join(v)}</span>' for q, v in by_q.items())
    return f'<div class="cross"></div>{spans}'


def progress(rng: random.Random, visits: list[dict]) -> str:
    """진료 경과 — 진료 한 번이 lines 수만큼 줄을 차지, 날짜·위치는 첫 줄, 서명은 마지막 줄"""
    rows = []
    for v in visits:
        old = v.get("struck", {})
        for i, text in enumerate(v["lines"]):
            first, last = i == 0, i == len(v["lines"]) - 1
            body = hw(rng, text, 32)
            if first and old.get("text"):
                body = hw(rng, old["text"], 32, "struck") + body
            date = hw(rng, written_date(v["date"], v.get("fmt")), 32) if first else ""
            loc = (
                location(rng, v["tooth"], old.get("tooth"))
                if first
                else '<div class="cross"></div>'
            )
            sign = hw(rng, v.get("sign", SIGN), 40, "brush") if last else ""
            rows.append(
                f'<tr><td>{date}</td><td class="loc">{loc}</td><td>{body}</td><td>{sign}</td></tr>'
            )
    empty = '<tr><td></td><td class="loc"><div class="cross"></div></td><td></td><td></td></tr>'
    rows += [empty] * (PROGRESS_ROWS - len(rows))
    return "".join(rows)


def ledgers(rng: random.Random, payments: list[dict]) -> str:
    """수납 장부 두 칸 — 날짜·내용·금액·잔액·서명, 앞 칸부터 채운다"""
    head = (
        '<tr><th style="width:21%">날 짜</th><th style="width:25%">내 용</th>'
        '<th style="width:20%">금 액</th><th style="width:20%">잔 액</th><th>서 명</th></tr>'
    )
    out = []
    for k in range(2):
        rows = []
        for p in payments[k * LEDGER_ROWS : (k + 1) * LEDGER_ROWS]:
            rows.append(
                f"<tr><td>{hw(rng, written_date(p['date']), 28)}</td><td>{hw(rng, p['item'], 28)}</td>"
                f"<td>{hw(rng, p['amount'], 28)}</td><td>{hw(rng, p['balance'], 28)}</td>"
                f"<td>{hw(rng, DESK_SIGN, 32, 'brush')}</td></tr>"
            )
        rows += ["<tr><td></td><td></td><td></td><td></td><td></td></tr>"] * (
            LEDGER_ROWS - len(rows)
        )
        out.append(f'<div class="panel" style="flex:1"><table>{head}{"".join(rows)}</table></div>')
    return "".join(out)


def notes(rows: list[dict]) -> str:
    """빈 칸에 쓴 빨간 펜 경고·메모"""
    return "".join(
        f'<span class="hw note {n["color"]}" style="left:{n["left"]}px;top:{n["top"]}px;'
        f'font-size:{n["size"]}px;transform:rotate({n["rot"]}deg)">{html.escape(n["text"])}</span>'
        for n in rows
    )


def stains(rng: random.Random) -> str:
    out = []
    for _ in range(2):
        r = rng.randint(60, 180)
        out.append(
            f'<div class="stain" style="left:{rng.randint(0, 1000)}px;top:{rng.randint(0, 1450)}px;'
            f"width:{r}px;height:{int(r * rng.uniform(0.6, 1))}px;"
            f'background:radial-gradient(rgba(150,110,40,{rng.uniform(0.06, 0.14):.2f}),transparent 70%)"></div>'
        )
    return "".join(out)


def page(chart: dict, template: Template) -> str:
    rng = random.Random(chart["chart_no"])
    y, m, d = chart["birth_date"].split("-")
    fy, fm, fd = chart["first_visit"].split("-")
    return template.substitute(
        tilt=f"{rng.uniform(-1.0, 1.0):.2f}",
        name=hw(rng, chart["name"], 40),
        old_chart_no=hw(rng, chart["old_chart_no"], 36),
        birth=hw(rng, f"{y}. {int(m)}. {int(d)}", 36),
        teeth=teeth(rng, chart["marks"]),
        staff=hw(rng, chart["staff"], 40, "brush"),
        first_visit=hw(rng, f"{fy}. {int(fm)}. {int(fd)}", 34),
        cc=lines(rng, chart["cc"]),
        pi=lines(rng, chart["pi"]),
        tx_plan=lines(rng, chart["tx_plan"], 36),
        notes=notes(chart["notes"]),
        ledgers=ledgers(rng, chart["payments"]),
        progress=progress(rng, chart["visits"]),
        stains_left=stains(rng),
        stains_right=stains(rng),
    )


def main() -> None:
    template = Template((HERE / "template.html").read_text(encoding="utf-8"))
    charts = json.loads((HERE / "charts.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        for chart in charts:
            src = Path(tmp) / f"{chart['chart_no']}.html"
            src.write_text(page(chart, template), encoding="utf-8")
            out = HERE / f"chart-{chart['chart_no']}.png"
            subprocess.run(
                [
                    "npx",
                    "playwright",
                    "screenshot",
                    "--channel=chrome",
                    f"--viewport-size={WIDTH},{HEIGHT}",
                    "--wait-for-timeout=2000",
                    src.as_uri(),
                    str(out),
                ],
                cwd=FRONTEND,
                check=True,
                capture_output=True,
            )
            print(out.relative_to(HERE.parents[1]))


if __name__ == "__main__":
    main()
