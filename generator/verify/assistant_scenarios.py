"""AI 비서 시나리오 검사 (ground_truth/assistant_scenarios.json — AI 비서 설계 §7·§7.1). DB가 아니라 JSON을 본다
(정답은 data/에 넣지 않고 truth_ 테이블도 두지 않는다). 짝 JSON이 없으면(verify 테스트 DB) 검사 없음.
문항 구성(유형별 수·분할·id·턴 모양)과 분할 격리(dev/test가 환자·날짜를 겹치지 않음 — 다음 묶음 학습 누수 방지 §7.1)."""

import json
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

# 유형별 시나리오 수 — 분할(dev·test)마다 이 수(AI 비서 설계 §7, 분할마다 28)
ASSISTANT_TYPE_COUNTS = {"booking": 8, "lookup": 8, "delegate": 4, "refuse": 4, "reference": 4}

ASSISTANT_TURNS = (2, 4)  # 시나리오 하나의 사용자 턴 수(양 끝 포함, §7)

ASSISTANT_ID = re.compile(r"as-\d{3}")

DRAFT_KEYS = {"chart_no", "date", "start_range", "staff_role"}
STAFF_ROLES = {"doctor", "hygienist", "consultant"}  # source.emr_staff.role
_HHMM = re.compile(r"\d{2}:\d{2}")


def _load(ctx) -> list[dict] | None:
    path = getattr(ctx, "assistant_scenarios", None)
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _end_ok(end) -> bool:
    """end = None · {no_write: true} · {draft: {chart_no, date, start_range: [시작, 끝], staff_role}}"""
    if end is None or end == {"no_write": True}:
        return True
    d = end.get("draft") if isinstance(end, dict) and set(end) == {"draft"} else None
    if not isinstance(d, dict) or set(d) != DRAFT_KEYS or d["staff_role"] not in STAFF_ROLES:
        return False
    r = d["start_range"]
    return (
        isinstance(r, list)
        and len(r) == 2
        and all(isinstance(t, str) and _HHMM.fullmatch(t) for t in r)
        and r[0] <= r[1]
    )


def _shape(s: dict) -> str | None:
    """시나리오 하나의 모양 문제(첫 번째 하나) — 없으면 None"""
    turns = s.get("turns") or []
    golds = [t.get("gold") or {} for t in turns]
    lo, hi = ASSISTANT_TURNS
    if s.get("split") not in ("dev", "test"):
        return "split"
    if not lo <= len(turns) <= hi:
        return "턴 수"
    if any(not str(t.get("user", "")).strip() for t in turns):
        return "빈 문장"
    if any(set(g) != {"tools", "args", "end"} for g in golds):
        return "gold 칸"
    if any(not isinstance(g["tools"], list) or not isinstance(g["args"], dict) for g in golds):
        return "gold 칸"
    if not all(_end_ok(g["end"]) for g in golds):
        return "end 모양"
    if s["type"] == "booking" and not (golds[-1]["end"] or {}).get("draft"):
        return "예약 초안 없음"
    if s["type"] == "refuse" and not any(g["end"] == {"no_write": True} for g in golds):
        return "쓰기 거절 없음"
    return None


def check_assistant_scenarios(conn, ctx):
    """AI 비서 시나리오 구성 — 유형별 수가 분할마다 ASSISTANT_TYPE_COUNTS, id(as-NNN) 유일, split은 dev·test,
    턴 2~4개(문장 있음), gold = {tools, args, end}, end는 없음·{no_write}·{draft} 모양, 예약 흐름은 마지막 턴이 초안,
    거절은 쓰기 거절 턴이 있음"""
    scenarios = _load(ctx)
    if scenarios is None:
        return []
    bad = []
    n = Counter((s.get("type"), s.get("split")) for s in scenarios)
    for t, k in ASSISTANT_TYPE_COUNTS.items():
        if not n[t, "dev"] == n[t, "test"] == k:
            bad.append({"type": t, "dev": n[t, "dev"], "test": n[t, "test"], "expected": k})
    other = sorted({s.get("type") for s in scenarios} - ASSISTANT_TYPE_COUNTS.keys(), key=str)
    if other:
        bad.append({"problem": "모르는 유형", "types": other})
    ids = Counter(s.get("id") for s in scenarios)
    for s in scenarios:
        sid = s.get("id")
        problem = (
            "id 형식"
            if not (isinstance(sid, str) and ASSISTANT_ID.fullmatch(sid))
            else "id 중복"
            if ids[sid] > 1
            else _shape(s)
        )
        if problem:
            bad.append({"id": sid, "problem": problem})
    return bad


def _days(a: str, b: str) -> set[str]:
    d, end = date.fromisoformat(a), date.fromisoformat(b)
    out = set()
    while d <= end:
        out.add(d.isoformat())
        d += timedelta(days=1)
    return out


def scenario_keys(s: dict) -> dict[str, set[str]]:
    """시나리오가 정답에 쓰는 환자(chart_no)·날짜(범위는 날마다 펼침)·달 — 분할 격리 비교 단위"""
    out = {"patient": set(), "day": set(), "month": set()}
    for t in s["turns"]:
        g = t["gold"]
        args = dict(g["args"])
        draft = (g["end"] or {}).get("draft")
        if draft:
            out["patient"].add(draft["chart_no"])
            out["day"].add(draft["date"])
        if args.get("chart_no"):
            out["patient"].add(args["chart_no"])
        if args.get("date"):
            out["day"].add(args["date"])
        if args.get("date_from") and args.get("date_to"):
            out["day"] |= _days(args["date_from"], args["date_to"])
        if args.get("month"):
            out["month"].add(args["month"])
    return out


def split_overlap(scenarios: list[dict]) -> list[dict]:
    """dev와 test가 함께 쓰는 환자·날짜·달 — {kind, value, ids}"""
    seen = defaultdict(lambda: defaultdict(set))
    for s in scenarios:
        for kind, values in scenario_keys(s).items():
            for v in values:
                seen[kind, v][s["split"]].add(s["id"])
    return [
        {"kind": kind, "value": v, "ids": sorted(by["dev"] | by["test"])}
        for (kind, v), by in sorted(seen.items())
        if by["dev"] and by["test"]
    ]


def check_assistant_split_isolation(conn, ctx):
    """AI 비서 시나리오 dev/test가 환자(chart_no)·날짜(범위는 날마다)·달을 함께 쓰지 않음 — 겹치면 dev로 학습한
    모델이 test 답을 미리 본다(AI 비서 설계 §7.1)"""
    scenarios = _load(ctx)
    return split_overlap(scenarios) if scenarios else []
