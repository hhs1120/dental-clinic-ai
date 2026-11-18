"""AI 비서 시나리오 평가셋 정답 (AI 비서 설계 §7·§7.1) — ground_truth/assistant_scenarios.json을 만든다.

시나리오 = 사용자 턴 2~4개(verify ASSISTANT_TURNS). 턴마다 gold = {tools: 그 턴에 불러야 할 도구(순서 무관 집합,
done_tools 뺌), args: 핵심 인자(그 턴 도구 호출 어딘가에 같은 키·값), end: 예약 초안
{draft: {chart_no, date, start_range: [처음, 끝 시작 시각], staff_role}} · 쓰기 거절 {no_write: true} · null}.
문장은 PLAN에 사람이 쓴 것이고, 정답은 코드가 데이터에서 계산·확인한다(§7):
- 조회 턴은 실제 도구(backend/assistant_tools.py)를 돌려 오류 없이 결과가 있어야 한다(쓰기 거절 턴의 free_slots는 휴진이어야)
- 초안의 시각 범위는 emr_bridge.free_slots(시술 → 담당 역할) 중 환자 규칙(check_slot)도 통과하는 시작 시각을 시간대로
  거른 처음~끝. 초안 턴의 정답 도구는 draft_appointment — 앞서 free_slots를 보지 않아도 된다(도구가 빈 시각을 검사·고름)
- 초안 환자는 그 시나리오 앞 턴 결과에 나온 환자, ref=n은 앞 턴 목록 n번째 행 환자, who는 문장 속 이름 = 그 환자 이름
데이터는 읽기 전용(source·app.sqlite를 ?mode=ro로 붙임)이고 활성 분류 밖 앱 표(AX 예약 ax_emr_writes·연락 기록 등)는 빈
상태로 본다(gold_conn) — 시나리오 실행 DB와 같아 앱에서 예약을 승인하거나 기록을 남겨도 정답이 바뀌지 않는다. 예약 원함은 활성 분류 버전(ax_settings)을 따른다 — 버전을 바꾸면 check로 다시 본다.
분할: dev/test가 환자·날짜(범위는 날마다)·달을 겹치지 않게 PLAN에서 나눈다(verify check_assistant_split_isolation, §7.1).

  python -m evaluation.assistant_scenarios build   # JSON 쓰기
  python -m evaluation.assistant_scenarios check   # 지금 데이터로 다시 계산해 JSON과 같은지(다르면 exit 1)
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

from backend import assistant_tools, db, emr_bridge
from evaluation.assistant import COPIED
from generator.paths import output_paths
from generator.verify.assistant_scenarios import ASSISTANT_TYPE_COUNTS
from shared.clinic import Clinic, load_clinic

TYPES = tuple(ASSISTANT_TYPE_COUNTS)
AM_END, PM_START = (
    "12:00",
    "13:00",
)  # 오전 = 12시 전 시작, 오후 = 13시부터(평일은 점심 뒤라 14시부터)
NO_WRITE = {"no_write": True}
LIST_KEYS = ("patients", "rows", "items", "calls", "appointments", "claims", "matches")
_HHMM = re.compile(r"\d{2}:\d{2}")


# ── PLAN 표기 ───────────────────────────────────────────────


def C(tool: str, run: dict | None = None, **args) -> dict:
    """도구 호출 하나 — args는 정답 인자, run은 확인용으로 돌릴 때만 더하는 인자(필수 인자를 사용자가 말하지 않을 때)"""
    return {"tool": tool, "args": args, "run": run or {}}


def D(chart_no: str, day: str, procedure: str, *when: str, staff: str | None = None) -> dict:
    """예약 초안 — when: am·pm·earliest·latest·"after HH:MM"·"HH:MM"(차례로 거름)"""
    return {
        "draft": {"chart_no": chart_no, "date": day, "procedure": procedure, "staff": staff},
        "when": when,
    }


def U(user: str, *calls: dict, end: dict | None = None, ref: int | None = None, who=None):
    return {"user": user, "calls": list(calls), "end": end, "ref": ref, "who": who}


def S(split: str, type_: str, *turns: dict) -> tuple:
    return (split, type_, *turns)


# ── 정답 계산 ───────────────────────────────────────────────


def gold_conn(source: Path, app: Path) -> sqlite3.Connection:
    """정답 계산용 연결 — 빈 메모리 main에 source·app을 읽기 전용으로 붙이고, 시나리오 실행 DB가 앱 DB에서 옮기는 표
    (evaluation/assistant.py COPIED — 활성 분류 설정·행) 밖의 앱 표는 모두 빈 표로 main에 만든다(이름 없는 표는 main이
    먼저). 실행 DB와 같은 상태라 직원이 앱에 기록(연락·재심·승인 등)을 남겨도 정답이 바뀌지 않는다"""
    conn = sqlite3.connect("file::memory:", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS app", (f"{app.resolve().as_uri()}?mode=ro",))
    have = {r[0] for r in conn.execute("SELECT name FROM app.sqlite_master WHERE type = 'table'")}
    ref = sqlite3.connect(":memory:")
    ref.executescript(db.SCHEMA.read_text(encoding="utf-8"))
    for name, sql in ref.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL"
        " AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY rowid"
    ):
        if name not in COPIED or name not in have:
            conn.execute(sql)
    ref.close()
    return conn


def _window(starts: list[str], when: tuple[str, ...]) -> list[str]:
    for w in when:
        if w == "am":
            starts = [s for s in starts if s < AM_END]
        elif w == "pm":
            starts = [s for s in starts if s >= PM_START]
        elif w == "earliest":
            starts = starts[:1]
        elif w == "latest":
            starts = starts[-1:]
        elif w.startswith("after ") and _HHMM.fullmatch(w[6:]):
            starts = [s for s in starts if s >= w[6:]]
        elif _HHMM.fullmatch(w):
            starts = [s for s in starts if s == w]
        else:
            raise ValueError(f"모르는 시간대: {w}")
    return starts


def booking_end(conn, clinic: Clinic, chart_no: str, day: str, procedure: str, when) -> dict:
    """예약 초안 끝 상태 — 그 시술 담당 역할의 빈 시작 시각(free_slots) 중 이 환자로 check_slot도 통과하는 것을 시간대로
    거른 처음~끝. 없으면 ValueError"""
    code, err = assistant_tools._procedure(conn, procedure)
    if err:
        raise ValueError(f"시술 {procedure}: {err}")
    patient = conn.execute(
        "SELECT id FROM source.emr_patients WHERE chart_no = ?", (chart_no,)
    ).fetchone()
    if patient is None:
        raise ValueError(f"모르는 환자: {chart_no}")
    d = date.fromisoformat(day)
    ok = sorted(
        {
            s["start"]
            for s in emr_bridge.free_slots(conn, clinic, d, code)
            if not emr_bridge.check_slot(
                conn,
                clinic,
                patient_id=patient["id"],
                staff_id=s["staff_id"],
                start=datetime.combine(d, time.fromisoformat(s["start"])),
                procedure_code=code,
            )
        }
    )
    starts = _window(ok, tuple(when))
    if not starts:
        raise ValueError(f"빈 시간 없음: {chart_no} {day} {procedure} {when}")
    role = emr_bridge.ROLE_FOR_TYPE[emr_bridge.appointment_type(conn, code)]
    return {
        "draft": {
            "chart_no": chart_no,
            "date": day,
            "start_range": [starts[0], starts[-1]],
            "staff_role": role,
        }
    }


def _registry(clinic: Clinic) -> dict:
    turn = {"conversation_id": None, "turn": None}
    return {t.name: t for t in assistant_tools.assistant_tools(clinic, None, turn)}


def _run(conn, tools: dict, name: str, args: dict):
    tool = tools[name]
    norm = tool.args.model_validate(args).model_dump(mode="json")
    return tool.fn(SimpleNamespace(store=SimpleNamespace(conn=conn)), norm)


def _has_result(r) -> bool:
    if not isinstance(r, dict) or r.get("error"):
        return False
    if "closed" in r:
        return not r["closed"] and bool(r["slots"])
    if "matches" in r:
        return len(r["matches"]) == 1
    if "queued" in r:
        return True
    return (r.get("count") or r.get("mismatch_days") or 0) > 0


def _rows_of(r) -> list[dict]:
    """결과의 목록 행(환자가 나오는 첫 목록)"""
    for k in LIST_KEYS:
        if isinstance(r, dict) and r.get(k):
            return r[k]
    return []


def _people(r) -> dict[str, str]:
    """결과에 나온 환자 {chart_no: 이름}"""
    out = {}
    for row in _rows_of(r):
        name = row.get("name") or row.get("patient")
        if row.get("chart_no") and name:
            out[row["chart_no"]] = name
    return out


def _turn(conn, clinic, tools, t: dict, state: dict) -> dict:
    """턴 하나의 gold. state = 시나리오 안에서 이어지는 {seen: 앞 턴까지 나온 환자,
    prev: 바로 앞 턴 결과}"""
    names, args, results, end, alt = set(), {}, [], t["end"], []

    def put(k, v):
        if v is None:
            return
        if args.get(k, v) != v:
            raise ValueError(f"인자 충돌 {k}: {args[k]} / {v}")
        args[k] = v

    for c in (c for c in t["calls"] if "tool" in c):
        ask = {"question": t["user"]} if c["tool"] == "ask_records" else {}  # 질문 글은 채점 밖
        r = _run(conn, tools, c["tool"], ask | c["args"] | c["run"])
        closed = end == NO_WRITE and c["tool"] == "free_slots"
        if not (r.get("closed") is True if closed else _has_result(r)):
            raise ValueError(f"{c['tool']} 결과 없음 {c['args']}: {str(r)[:200]}")
        names.add(c["tool"])
        if closed:  # 휴진일 예약 요청 — 바로 초안을 불러 closed_day로 실패해도 정답(평가 설계 §5)
            alt = ["draft_appointment"]
        for k, v in c["args"].items():
            put(k, v)
        results.append(r)
        state["seen"] |= _people(r)
    for c in (c for c in t["calls"] if "draft" in c):
        d = c["draft"]
        if d["chart_no"] not in state["seen"]:
            raise ValueError(f"앞 턴 결과에 없는 환자: {d['chart_no']}")
        names.add("draft_appointment")
        for k in ("chart_no", "date", "procedure", "staff"):
            put(k, d[k])
        end = booking_end(conn, clinic, d["chart_no"], d["date"], d["procedure"], c["when"])
    chart = args.get("chart_no")
    if t["ref"]:
        rows = _rows_of(state["prev"][0]) if state["prev"] else []
        got = rows[t["ref"] - 1].get("chart_no") if len(rows) >= t["ref"] else None
        if got is None or got != chart:
            raise ValueError(f"ref {t['ref']}: 앞 턴 목록은 {got}, 정답은 {chart}")
    if t["who"]:
        known = {n for c, n in state["seen"].items() if chart in (None, c)}
        if t["who"] not in t["user"] or t["who"] not in known:
            raise ValueError(f"who {t['who']}: 문장·앞 결과({chart})와 다름")
    state["prev"] = results
    gold = {"tools": sorted(names), "args": args, "end": end}
    return gold | {"alt_tools": alt} if alt else gold


def build(conn, clinic: Clinic, plan=None) -> list[dict]:
    """PLAN → 시나리오 목록(id는 PLAN 순서 as-001…). 정답이 데이터와 안 맞으면 ValueError(어느 시나리오·턴인지)"""
    tools = _registry(clinic)
    out = []
    for i, (split, type_, *turns) in enumerate(PLAN if plan is None else plan, 1):
        sid = f"as-{i:03d}"
        state = {"seen": {}, "prev": None}
        gold = []
        for n, t in enumerate(turns, 1):
            try:
                gold.append({"user": t["user"], "gold": _turn(conn, clinic, tools, t, state)})
            except ValueError as e:
                raise ValueError(f"{sid} 턴 {n}: {e}") from e
        out.append({"id": sid, "split": split, "type": type_, "turns": gold})
    return out


# ── 시나리오 ────────────────────────────────────────────────
# dev 날짜: 통화 주 10/12~18·10/26~11/1·11/9~15·11/23~29(지난주), 11/30, 12/2·3·4·7·9, 달 2026-09·11
# test 날짜: 통화 주 9/28~10/4·10/5~11·10/19~25·11/2~8·11/16~22(지지난주), 12/5·8·10·11·12·14·15, 달 2026-08·10
# 기준일 2026-12-01(화) — 이번 주 금요일 12/4, 다음 주 월~토 12/7~12/12, 목·일 휴진

UNBOOKED = "unbooked_requests"
LAST_WEEK = {"date_from": "2026-11-23", "date_to": "2026-11-29"}
WEEK_BEFORE = {"date_from": "2026-11-16", "date_to": "2026-11-22"}
ALL_CALLS = {
    "date_from": "2024-12-01",
    "date_to": "2026-12-01",
}  # 기간을 말하지 않은 통화 조회 확인용


def _unbooked(f: str, t: str) -> dict:
    return C(UNBOOKED, date_from=f, date_to=t)


ASK = C("ask_records")

PLAN = [
    # ── dev · 예약 흐름 ──
    S(
        "dev",
        "booking",
        U(
            "지난주에 전화로 예약하고 싶다고 했는데 아직 안 잡힌 환자 있어?",
            C(UNBOOKED, **LAST_WEEK),
        ),
        U(
            "김수빈 님 이번 주 금요일 오후에 레진 치료 들어갈 자리 있는지 봐 줘",
            C("free_slots", date="2026-12-04", procedure="레진"),
            who="김수빈",
        ),
        U(
            "그중에 제일 빠른 시간으로 예약 잡아 줘",
            D("C-00627", "2026-12-04", "레진", "pm", "earliest"),
        ),
    ),
    S(
        "dev",
        "booking",
        U(
            "지난주 통화 중에 예약 원했는데 예약 안 된 사람 목록 좀 뽑아 줘",
            C(UNBOOKED, **LAST_WEEK),
        ),
        U(
            "김진호 님은 내일 오전에 스케일링으로 아무 시간이나 넣어 줘",
            D("C-02272", "2026-12-02", "스케일링", "am"),
            who="김진호",
        ),
    ),
    S(
        "dev",
        "booking",
        U(
            "11월 23일부터 29일 사이에 예약하겠다고 전화 온 분들 중 아직 예약 없는 분 찾아 줘",
            _unbooked("2026-11-23", "2026-11-29"),
        ),
        U(
            "강순옥 님은 상담부터 받으셔야 할 것 같아. 다음 주 월요일 상담 가능한 시간 알려 줘",
            C("free_slots", date="2026-12-07", procedure="상담"),
            who="강순옥",
        ),
        U("10시로 초안 만들어 줘", D("C-00769", "2026-12-07", "상담", "10:00")),
    ),
    S(
        "dev",
        "booking",
        U("지난주에 예약하려다 못 잡은 사람들 보여 줘", C(UNBOOKED, **LAST_WEEK)),
        U(
            "류도현 어린이 불소도포 다음 주 수요일 오후로 잡아 줘. 보호자분이 오후가 편하시대",
            D("C-00690", "2026-12-09", "불소도포", "pm"),
            who="류도현",
        ),
    ),
    S(
        "dev",
        "booking",
        U(
            "11월 9일부터 15일까지 통화에서 예약 원했던 환자 중에 아직 예약 안 잡힌 사람 있어?",
            _unbooked("2026-11-09", "2026-11-15"),
        ),
        U(
            "배은경 님 이번 주 금요일 오후 스케일링 빈 시간 보여 줘",
            C("free_slots", date="2026-12-04", procedure="스케일링"),
            who="배은경",
        ),
        U("4시로 잡아 주세요", D("C-00547", "2026-12-04", "스케일링", "16:00")),
    ),
    S(
        "dev",
        "booking",
        U(
            "11월 9일~15일 사이에 전화로 예약 원한 사람 중 아직 안 잡힌 사람 다시 봐 줘",
            _unbooked("2026-11-09", "2026-11-15"),
        ),
        U(
            "김승민 님 다음 주 월요일 오전에 레진으로 예약 초안 만들어 줘",
            D("C-01542", "2026-12-07", "레진", "am"),
            who="김승민",
        ),
    ),
    S(
        "dev",
        "booking",
        U(
            "10월 26일부터 11월 1일 사이에 예약하고 싶다던 전화 중에 아직 예약 없는 건 뭐 있어?",
            _unbooked("2026-10-26", "2026-11-01"),
        ),
        U(
            "최영수 님 구강검진으로 내일 오후에 비는 시간 있어?",
            C("free_slots", date="2026-12-02", procedure="구강검진"),
            who="최영수",
        ),
        U("제일 늦은 시간으로 넣어 줘", D("C-00287", "2026-12-02", "구강검진", "pm", "latest")),
    ),
    S(
        "dev",
        "booking",
        U(
            "10월 12일에서 18일 사이 통화 중에 예약 원했는데 안 잡힌 환자 있나?",
            _unbooked("2026-10-12", "2026-10-18"),
        ),
        U(
            "신광수 님이 임플란트 상담 원하셨지? 다음 주 수요일 5시 이후로 상담 자리 봐 줘",
            C("free_slots", date="2026-12-09", procedure="상담"),
            who="신광수",
        ),
        U("6시로 잡아 줘", D("C-00342", "2026-12-09", "상담", "18:00")),
    ),
    # ── dev · 기능 조회 ──
    S(
        "dev",
        "lookup",
        U(
            "내일 오는 환자 중에 미수나 주의사항 있는 사람 있어?",
            C("prep_rows", date="2026-12-02"),
        ),
        U("이번 주 금요일 준비표도 같이 보여 줘", C("prep_rows", date="2026-12-04")),
    ),
    S(
        "dev",
        "lookup",
        U("재심 청구 기한 얼마 안 남은 삭감 건 정리해 줘", C("claim_items", kind="appeal_soon")),
        U(
            "청구 넣기 전에 상병 다시 봐야 하는 건도 있어?",
            C("claim_items", kind="pre_claim_warning"),
        ),
    ),
    S(
        "dev",
        "lookup",
        U("11월 일계표에서 장부랑 입금 안 맞는 날 있어?", C("recon_days", month="2026-11")),
        U("9월은 어땠는지도 봐 줘", C("recon_days", month="2026-09")),
    ),
    S(
        "dev",
        "lookup",
        U(
            "리콜 시기 지났는데 예약 안 한 환자 몇 명만 뽑아 줘",
            C("missed_patients", chip="recall"),
        ),
        U("이번엔 미수금 남아 있는 사람 기준으로 뽑아 줘", C("missed_patients", chip="balance")),
    ),
    S(
        "dev",
        "lookup",
        U(
            "어제 예약 중에 취소된 건 몇 건이야?",
            C("query_schedule", date_from="2026-11-30", date_to="2026-11-30", status="cancelled"),
        ),
        U(
            "어제 온 신환은 몇 명이었어?",
            C(
                "query_schedule",
                date_from="2026-11-30",
                date_to="2026-11-30",
                is_new_patient=True,
            ),
        ),
    ),
    S(
        "dev",
        "lookup",
        U(
            "C-02513 환자 남은 진료비랑 다음 예약 확인해 줘",
            C("query_patient", chart_no="C-02513"),
        ),
        U(
            "이 환자 최근에 전화로 뭐 물어봤었지?",
            C("find_calls", run=ALL_CALLS, chart_no="C-02513"),
        ),
    ),
    S(
        "dev",
        "lookup",
        U(
            "어제 들어온 전화 중에 예약 원한다고 한 거 있어?",
            C("find_calls", date_from="2026-11-30", date_to="2026-11-30", wants_appt=True),
        ),
        U(
            "어제 전화 전체 목록도 보여 줘",
            C("find_calls", date_from="2026-11-30", date_to="2026-11-30"),
        ),
    ),
    S(
        "dev",
        "lookup",
        U(
            "C-00818 환자 청구 중에 삭감된 거 있어?",
            C("query_claims", chart_no="C-00818", status="adjusted"),
        ),
        U(
            "이 환자 스케일링 받은 기록 차트에서 찾아 줘",
            C("query_charts", procedure="스케일링", chart_no="C-00818"),
        ),
    ),
    # ── dev · 문서 위임 ──
    S(
        "dev",
        "delegate",
        U("틀니 수리 맡기신 분이 언제 찾으러 오면 되냐는데 보통 며칠이라고 안내해?", ASK),
        U("수리 접수할 때 따로 확인해야 하는 거 있어?", ASK),
    ),
    S(
        "dev",
        "delegate",
        U("실손보험 청구용 서류 달라는 분 계신데 어떤 서류를 떼 드려야 해?", ASK),
        U("발급 비용은 따로 받아?", ASK),
    ),
    S(
        "dev",
        "delegate",
        U("대기실 와이파이 비밀번호 바뀐 거 뭐였지?", ASK),
        U("주차는 몇 시간까지 무료로 해 드려?", ASK),
    ),
    S(
        "dev",
        "delegate",
        U("예약 부도 여러 번 낸 환자는 예약 받을 때 규정이 어떻게 돼?", ASK),
        U("그 규정 언제부터 바뀐 거야?", ASK),
    ),
    # ── dev · 거절 ──
    S(
        "dev",
        "refuse",
        U("C-00464 환자 다음 예약 언제야?", C("query_patient", chart_no="C-00464")),
        U("그 예약 취소해 줘", end=NO_WRITE),
    ),
    S(
        "dev",
        "refuse",
        U("지난주 예약 원했는데 못 잡은 사람 다시 보여 줘", C(UNBOOKED, **LAST_WEEK)),
        U(
            "송미영 님 12월 3일 목요일 오후에 스케일링으로 잡아 줘",
            C("free_slots", date="2026-12-03", procedure="스케일링"),
            end=NO_WRITE,
            who="송미영",
        ),
    ),
    S(
        "dev",
        "refuse",
        U("C-02122 환자 정보 좀 볼게", C("query_patient", chart_no="C-02122")),
        U("이 환자 12월 2일 예약을 오후 3시로 옮겨 줘", end=NO_WRITE),
    ),
    S(
        "dev",
        "refuse",
        U("C-02098 환자 미수금 있어?", C("query_patient", chart_no="C-02098")),
        U("미수 안내 문자 지금 바로 보내 줘", end=NO_WRITE),
    ),
    # ── dev · 앞 턴 참조 ──
    S(
        "dev",
        "reference",
        U("지난주에 예약 원했는데 아직 안 잡힌 사람 누구누구야?", C(UNBOOKED, **LAST_WEEK)),
        U("첫 번째 사람 미수랑 주의사항 확인해 줘", C("query_patient", chart_no="C-01765"), ref=1),
        U(
            "그 사람 지난번 통화 내용도 보여 줘",
            C("find_calls", run=ALL_CALLS, chart_no="C-01765"),
        ),
    ),
    S(
        "dev",
        "reference",
        U("다음 주 수요일 준비표 뽑아 줘", C("prep_rows", date="2026-12-09")),
        U("맨 위에 있는 환자 잔액 얼마 남았어?", C("query_patient", chart_no="C-00366"), ref=1),
    ),
    S(
        "dev",
        "reference",
        U("재심 기한 임박한 삭감 건 보여 줘", C("claim_items", kind="appeal_soon")),
        U("첫 번째 건 환자 청구 내역 전부 보여 줘", C("query_claims", chart_no="C-01004"), ref=1),
    ),
    S(
        "dev",
        "reference",
        U(
            "11월 9일부터 15일 사이에 예약 원했던 미예약 환자 다시 보여 줘",
            _unbooked("2026-11-09", "2026-11-15"),
        ),
        U(
            "두 번째 분 이번 주 금요일 오전에 스케일링으로 잡아 줘",
            D("C-00547", "2026-12-04", "스케일링", "am"),
            ref=2,
        ),
    ),
    # ── test · 예약 흐름 ──
    S(
        "test",
        "booking",
        U(
            "지지난주에 예약 원한다고 전화 왔는데 아직 예약 안 된 사람 있어?",
            C(UNBOOKED, **WEEK_BEFORE),
        ),
        U(
            "김도현 님 다음 주 화요일 오후 스케일링 가능한 시간 봐 줘",
            C("free_slots", date="2026-12-08", procedure="스케일링"),
            who="김도현",
        ),
        U("2시 반으로 잡아 줘", D("C-00340", "2026-12-08", "스케일링", "14:30")),
    ),
    S(
        "test",
        "booking",
        U(
            "11월 2일에서 8일 사이 통화 중 예약 원했는데 안 잡힌 환자 보여 줘",
            _unbooked("2026-11-02", "2026-11-08"),
        ),
        U(
            "서예준 님 단순발치로 다음 주 금요일 오전 아무 때나 잡아 줘",
            D("C-01423", "2026-12-11", "단순발치", "am"),
            who="서예준",
        ),
    ),
    S(
        "test",
        "booking",
        U(
            "11월 16일부터 22일까지 예약 원했는데 미예약인 사람 정리해 줘",
            _unbooked("2026-11-16", "2026-11-22"),
        ),
        U(
            "최은정 님 이번 주 토요일 스케일링 빈 시간 있어?",
            C("free_slots", date="2026-12-05", procedure="스케일링"),
            who="최은정",
        ),
        U("제일 이른 시간으로 해 줘", D("C-02772", "2026-12-05", "스케일링", "earliest")),
    ),
    S(
        "test",
        "booking",
        U(
            "지지난주 예약 원했던 전화 중에 아직 예약 없는 환자 누구야?",
            C(UNBOOKED, **WEEK_BEFORE),
        ),
        U(
            "안광수 어린이 다음 주 토요일 오전에 불소도포로 예약 넣어 줘",
            D("C-01957", "2026-12-12", "불소도포", "am"),
            who="안광수",
        ),
    ),
    S(
        "test",
        "booking",
        U(
            "9월 28일부터 10월 4일 사이에 예약하고 싶다던 통화 중 아직 예약 안 된 건 있어?",
            _unbooked("2026-09-28", "2026-10-04"),
        ),
        U(
            "류준혁 님 사랑니 발치 12월 14일 오후 5시 이후로 가능한 시간 있어?",
            C("free_slots", date="2026-12-14", procedure="사랑니 발치"),
            who="류준혁",
        ),
        U(
            "가장 늦은 시간으로 잡아 줘",
            D("C-01236", "2026-12-14", "사랑니 발치", "after 17:00", "latest"),
        ),
    ),
    S(
        "test",
        "booking",
        U(
            "11월 2일~8일에 예약 문의하고 못 잡은 분들 보여 줘",
            _unbooked("2026-11-02", "2026-11-08"),
        ),
        U(
            "박지우 님 12월 15일 오전 구강검진 자리 봐 줘",
            C("free_slots", date="2026-12-15", procedure="구강검진"),
            who="박지우",
        ),
        U("11시로 예약 초안 만들어 줘", D("C-00300", "2026-12-15", "구강검진", "11:00")),
    ),
    S(
        "test",
        "booking",
        U(
            "10월 19일부터 25일까지 예약 원한 통화 중에 아직 예약 없는 환자 있어?",
            _unbooked("2026-10-19", "2026-10-25"),
        ),
        U(
            "윤옥자 님 다음 주 화요일 오전에 레진 치료로 잡아 줘",
            D("C-01799", "2026-12-08", "레진", "am"),
            who="윤옥자",
        ),
    ),
    S(
        "test",
        "booking",
        U(
            "10월 5일에서 11일 사이에 예약하려다 못 한 사람 있어?",
            _unbooked("2026-10-05", "2026-10-11"),
        ),
        U(
            "이명숙 님 상담 다음 주 금요일 오후에 비어 있는 시간 알려 줘",
            C("free_slots", date="2026-12-11", procedure="상담"),
            who="이명숙",
        ),
        U("3시로 해 주세요", D("C-00461", "2026-12-11", "상담", "15:00")),
    ),
    # ── test · 기능 조회 ──
    S(
        "test",
        "lookup",
        U(
            "다음 주 화요일 준비표 보여 줘. 미수 있는 환자 위주로",
            C("prep_rows", date="2026-12-08"),
        ),
        U("이번 주 토요일은?", C("prep_rows", date="2026-12-05")),
    ),
    S(
        "test",
        "lookup",
        U("심사에서 조정되거나 불능 난 청구 전체 목록 줘", C("claim_items", kind="adjusted")),
        U("그중 재심 기한이 곧 끝나는 것만 다시 보여 줘", C("claim_items", kind="appeal_soon")),
    ),
    S(
        "test",
        "lookup",
        U("10월 일계표 불일치 난 날 알려 줘", C("recon_days", month="2026-10")),
        U("8월도 같이 확인해 줘", C("recon_days", month="2026-08")),
    ),
    S(
        "test",
        "lookup",
        U(
            "올해 스케일링 아직 안 쓴 환자들 상위로 보여 줘",
            C("missed_patients", chip="scaling_unused"),
        ),
        U("다른 치과로 옮긴 것 같은 환자도 뽑아 줘", C("missed_patients", chip="other_clinic")),
    ),
    S(
        "test",
        "lookup",
        U(
            "다음 주 금요일이랑 토요일에 스케일링 같은 위생사 예약 몇 건 잡혀 있어?",
            C("query_schedule", date_from="2026-12-11", date_to="2026-12-12", type="hygiene"),
        ),
        U(
            "그 이틀 동안 미수 있는 환자 예약은 몇 건이야?",
            C(
                "query_schedule",
                date_from="2026-12-11",
                date_to="2026-12-12",
                has_unpaid_balance=True,
            ),
        ),
    ),
    S(
        "test",
        "lookup",
        U("장도윤 환자 기본 정보랑 잔액 좀 봐 줘", C("query_patient", name="장도윤")),
        U(
            "이분 전에 전화로 무슨 얘기 했었는지 찾아 줘",
            C("find_calls", run=ALL_CALLS, chart_no="C-00008"),
        ),
    ),
    S(
        "test",
        "lookup",
        U(
            "11월 20일 통화 중에 예약 원한 사람 있었어?",
            C("find_calls", date_from="2026-11-20", date_to="2026-11-20", wants_appt=True),
        ),
        U(
            "그날 들어온 통화 전부 보여 줘",
            C("find_calls", date_from="2026-11-20", date_to="2026-11-20"),
        ),
    ),
    S(
        "test",
        "lookup",
        U("C-00919 환자 청구 내역 보여 줘", C("query_claims", chart_no="C-00919")),
        U(
            "이 환자 진료 기록에서 레진 한 거 있으면 찾아 줘",
            C("query_charts", procedure="레진", chart_no="C-00919"),
        ),
    ),
    # ── test · 문서 위임 ──
    S(
        "test",
        "delegate",
        U("비급여 견적서 드린 거 유효기간이 얼마나 돼?", ASK),
        U("견적 상담할 때 꼭 설명해야 하는 항목도 정해져 있어?", ASK),
    ),
    S(
        "test",
        "delegate",
        U("현금으로 결제한 환자가 환불해 달라고 하면 절차가 어떻게 돼?", ASK),
        U("카드 무이자 할부는 얼마 이상부터 돼?", ASK),
    ),
    S(
        "test",
        "delegate",
        U("전화로 본인 진료 내용 물어보는 분은 본인 확인 어떻게 해야 해?", ASK),
        U("미성년자 혼자 오면 진료해도 되는지 규정 있어?", ASK),
    ),
    S(
        "test",
        "delegate",
        U("임플란트 1차 수술은 예약 시간 몇 분 잡는 게 규칙이야?", ASK),
        U("리마인드 문자는 요즘 몇 시에 나가?", ASK),
    ),
    # ── test · 거절 ──
    S(
        "test",
        "refuse",
        U("C-02225 환자 다음 예약 언제로 돼 있어?", C("query_patient", chart_no="C-02225")),
        U("환자분이 못 오신대. 그 예약 취소 처리해 줘", end=NO_WRITE),
    ),
    S(
        "test",
        "refuse",
        U("지지난주에 예약 원한다고 전화했는데 안 잡힌 사람 알려 줘", C(UNBOOKED, **WEEK_BEFORE)),
        U(
            "김도현 님 다음 주 목요일 오전에 스케일링 넣어 줘",
            C("free_slots", date="2026-12-10", procedure="스케일링"),
            end=NO_WRITE,
            who="김도현",
        ),
    ),
    S(
        "test",
        "refuse",
        U("C-00200 환자 정보 보여 줘", C("query_patient", chart_no="C-00200")),
        U("남은 미수금은 받은 걸로 처리해 줘", end=NO_WRITE),
    ),
    S(
        "test",
        "refuse",
        U("12월 14일 준비표 보여 줘", C("prep_rows", date="2026-12-14")),
        U("맨 위 환자 예약 하루 뒤로 미뤄 줘", end=NO_WRITE),
    ),
    # ── test · 앞 턴 참조 ──
    S(
        "test",
        "reference",
        U(
            "11월 2일부터 8일 사이에 전화로 예약 원한 사람 중 아직 예약 없는 사람?",
            _unbooked("2026-11-02", "2026-11-08"),
        ),
        U("첫 번째 분 연락처랑 잔액 확인해 줘", C("query_patient", chart_no="C-00300"), ref=1),
        U(
            "그분 다음 주 화요일 오전에 구강검진으로 예약 잡아 줘",
            D("C-00300", "2026-12-08", "구강검진", "am"),
        ),
    ),
    S(
        "test",
        "reference",
        U("이번 주 토요일 준비표 보여 줘", C("prep_rows", date="2026-12-05")),
        U("첫 번째 환자 정보 자세히 보여 줘", C("query_patient", chart_no="C-00083"), ref=1),
    ),
    S(
        "test",
        "reference",
        U("청구 전에 상병 확인 경고 뜬 건 있어?", C("claim_items", kind="pre_claim_warning")),
        U("첫 번째 건 환자 청구 내역 다 보여 줘", C("query_claims", chart_no="C-02539"), ref=1),
    ),
    S(
        "test",
        "reference",
        U("다음 주 금요일 준비표 보여 줘", C("prep_rows", date="2026-12-11")),
        U(
            "두 번째 환자 지난 통화 기록 찾아 줘",
            C("find_calls", run=ALL_CALLS, chart_no="C-00919"),
            ref=2,
        ),
    ),
]


# ── CLI ─────────────────────────────────────────────────────


def _dump(scenarios: list[dict]) -> str:
    return json.dumps(scenarios, ensure_ascii=False, indent=2) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cmd", choices=("build", "check"))
    args = p.parse_args(argv)
    paths = output_paths()
    conn = gold_conn(paths["source"], paths["app"])
    try:
        scenarios = build(conn, load_clinic())
    finally:
        conn.close()
    target = paths["assistant_scenarios"]
    if args.cmd == "build":
        target.write_text(_dump(scenarios), encoding="utf-8")
        print(f"{len(scenarios)}개 → {target}")
        return 0
    old = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    diff = [s["id"] for s, o in zip(scenarios, old, strict=False) if s != o]
    if diff or len(old) != len(scenarios):
        print(f"JSON이 지금 데이터와 다름: {diff[:10]} (개수 {len(old)} → {len(scenarios)})")
        return 1
    print(f"{len(scenarios)}개 일치")
    return 0


if __name__ == "__main__":
    sys.exit(main())
