"""⑦ 아침 브리핑 1단계 — 코드 템플릿 (기능 정의 §5 ⑦, 설계 docs/superpowers/specs/2026-11-16-nightly-briefing-design.md §4).
요약 카드·"사람이 볼 일"에 없는 하루의 모양(몰리는 시간대·큰 시술·챙길 환자)과 밤사이(마지막 야간 실행)만 줄로 낸다.
준비표 사실(prep.prep_facts)과 ax_jobs nightly 행만 읽고 LLM을 부르지 않는다."""

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta

from backend import nightly, prep

BUSY_TOP = 3  # 몰리는 시간대 — 시작 예약 수가 그날 최댓값(2건 이상)인 시. 동률이 이보다 많으면 고르게 찬 날이라 쓰지 않는다
BIG_CATEGORIES = ("surgery", "implant")  # 큰 시술 — 발치·임플란트(60분 이상인 보철은 넣지 않는다)
BIG_MAX = 3  # 큰 시술 줄에 보일 수, 넘으면 "외 n건"
CARE_MAX = 3  # 챙길 환자 — 큰 시술이면서 그 시술과 관련된 주의사항이 있는 예약
NIGHT_HOURS = 18  # 밤사이 — 마지막 야간 실행이 끝난 지 이 시간(실제 시계) 안일 때만


def _hm(time: str) -> str:
    """'09:30' → '9:30'"""
    h, m = time.split(":")
    return f"{int(h)}:{m}"


def day_lines(rows: list[dict], categories: dict[str, str]) -> list[dict]:
    """준비표 행(시각순) → 오늘·큰 시술·챙길 환자 줄. 해당 없는 줄은 내지 않는다"""
    if not rows:
        return []
    out = []
    hours = Counter(int(r["time"][:2]) for r in rows)
    top = max(hours.values())
    busy = sorted(h for h, n in hours.items() if n == top)
    new = sum(r["patient"]["new_patient"] for r in rows)
    text = f"예약 {len(rows)}건(신환 {new})"
    if top >= 2 and len(busy) <= BUSY_TOP:
        text += " · " + "·".join(f"{h}시" for h in busy) + "대에 몰림"
    out.append({"head": "오늘", "text": text})
    big = [r for r in rows if categories.get(r["procedure_code"]) in BIG_CATEGORIES]
    if big:
        text = " · ".join(f"{r['procedure']} {_hm(r['time'])}" for r in big[:BIG_MAX])
        if len(big) > BIG_MAX:
            text += f" 외 {len(big) - BIG_MAX}건"
        out.append({"head": "큰 시술", "text": text})
    care = [r for r in big if any(a["related"] for a in r["alerts"])][:CARE_MAX]
    for r in care:
        labels = ", ".join(a["label"] for a in r["alerts"] if a["related"])
        out.append(
            {
                "head": "챙길 환자",
                "text": f"{_hm(r['time'])} {r['patient']['name']} {r['procedure']} — {labels}",
            }
        )
    return out


def night_line(job: dict | None, now: datetime) -> dict | None:
    """마지막 야간 실행(nightly.last_run) → 밤사이 줄. 끝나지 않았거나 NIGHT_HOURS가 지났으면 None.
    급한 통화는 "사람이 볼 일"에 있어 넣지 않는다"""
    if not job or not job["finished_at"]:
        return None
    end = datetime.fromisoformat(job["finished_at"])
    if now - end > timedelta(hours=NIGHT_HOURS):
        return None
    steps = {d["step"]: d for d in job["detail"]}
    classified = steps.get("classify", {}).get("new", 0)
    closed = steps.get("autoclose", {}).get("new", 0)
    failed = sum(d["failed"] for d in job["detail"])
    tail = f"{end:%H:%M} 끝남"
    if any(d["note"] == nightly.BUDGET_NOTE for d in job["detail"]):
        tail += f" · {nightly.BUDGET_NOTE}"
    return {
        "head": "밤사이",
        "text": f"통화 {classified}건 분류 · 자동 종결 {closed} · 실패 {failed} ({tail})",
    }


def lines(conn: sqlite3.Connection, today: date, now: datetime) -> list[dict]:
    """오늘 화면 브리핑 칸(backend/today.py) — today = 앱 기준일, now = 실제 시계"""
    cats = dict(conn.execute("SELECT code, category FROM source.emr_procedures").fetchall())
    night = night_line(nightly.last_run(conn), now)
    return day_lines(prep.prep_facts(conn, today, today), cats) + ([night] if night else [])
