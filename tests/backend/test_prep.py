"""④ 진료 준비표 — 사실·칩(prep_facts). 설계 docs/superpowers/specs/2026-11-10-prep-sheet-design.md §2.
손으로 만든 source(실제 스키마 + master 값)로 칩마다 켜짐·꺼짐을 본다."""

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from backend import prep
from generator.clinic import master_values_rows
from generator.master import ALERT_TYPES, PROCEDURES

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = Path(__file__).parent / "snapshots" / "prep_facts.json"
TODAY = date(2026, 12, 1)

APPT = (
    "INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,"
    " planned_procedure_code, status, booked_via, confirmed, created_at, chair_no, plan_item_id)"
    " VALUES (?, ?, ?, ?, 30, ?, ?, ?, 'phone', ?, '2026-10-01 09:00:00', ?, ?)"
)


def make_source(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / "shared" / "source_schema.sql").read_text(encoding="utf-8"))
    cols = list(PROCEDURES[0])
    conn.executemany(
        f"INSERT INTO emr_procedures ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [tuple(p[c] for c in cols) for p in PROCEDURES],
    )
    conn.executemany(
        "INSERT INTO emr_alert_types VALUES (?, ?, ?)",
        [(c, a["label"], a["category"]) for c, a in ALERT_TYPES.items()],
    )
    conn.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
    conn.executemany(
        "INSERT INTO emr_staff (id, name, role) VALUES (?, ?, ?)",
        [(1, "강태현", "doctor"), (2, "임재윤", "hygienist")],
    )
    conn.execute("INSERT INTO emr_families VALUES (1, '김')")
    conn.executemany(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id,"
        " registered_at) VALUES (?, ?, ?, ?, ?, '010-0000-0000', ?, '2020-01-01')",
        [
            (1, "C-00001", "김철수", "1970-03-01", "M", 1),
            (2, "C-00002", "박영희", "1972-05-01", "F", 1),
            (3, "C-00003", "김민수", "2000-07-01", "M", 1),
            (4, "C-00004", "정다은", "2010-02-01", "F", None),
        ],
    )
    conn.executemany(
        APPT,
        [
            # 지난 내원 — 김철수 11/10 레진(계획 1을 만듦), 11/15 검진, 11/20 계획 밖 레진(17번 → 대체)
            (90, 1, 1, "2026-11-10 10:00:00", "treatment", "U0005", "completed", 1, 1, None),
            (80, 1, 2, "2026-11-15 10:00:00", "hygiene", "U0002", "completed", 1, 2, None),
            (95, 1, 1, "2026-11-20 10:00:00", "treatment", "U0005", "completed", 1, 1, None),
            # 위생 이력 — 박영희·김민수는 리콜(12개월)이 지났다
            (70, 2, 2, "2025-11-01 10:00:00", "hygiene", "U0001", "completed", 1, 2, None),
            (71, 3, 2, "2025-10-01 10:00:00", "hygiene", "U0001", "completed", 1, 2, None),
            # 기준일 예약
            (101, 1, 1, "2026-12-01 10:00:00", "treatment", "U0011", "scheduled", 0, 1, 11),
            (102, 2, 2, "2026-12-01 11:00:00", "hygiene", "U0001", "scheduled", 1, 2, None),
            (103, 4, 1, "2026-12-01 14:00:00", "consult", "U0019", "scheduled", 0, None, None),
            (104, 3, 2, "2026-12-01 15:00:00", "hygiene", "U0001", "cancelled", 0, 2, None),
        ],
    )
    conn.executemany(
        "INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,"
        " performed_at, fee_total, insured_amount, patient_amount) VALUES (?, ?, 1, ?, ?, ?, 0, 0, 0)",
        [
            (1, 90, "U0005", "16", "2026-11-10 10:00:00"),
            (2, 95, "U0005", "17", "2026-11-20 10:00:00"),
        ],
    )
    conn.execute("INSERT INTO emr_treatment_plans VALUES (1, 1, '2026-11-10 10:30:00', 90)")
    conn.execute("INSERT INTO emr_treatment_plans VALUES (2, 1, '2026-11-15 10:30:00', 80)")
    conn.executemany(
        "INSERT INTO emr_treatment_plan_items VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (11, 1, 1, "U0011", "36", 30000, "open"),  # 기준일 예정 — 남은 계획에서 빠짐
            (12, 1, 2, "U0009", "46", 450000, "open"),
            (13, 1, 3, "U0005", "17", 80000, "replaced"),  # 11/20 계획 밖 진료로 대체
            (21, 2, 1, "U0018", "00", 50000, "open"),  # 11/15 내원이 만든 계획
        ],
    )
    conn.execute(
        "INSERT INTO emr_payments (patient_id, appointment_id, kind, amount_due, amount_paid,"
        " paid_at, method) VALUES (1, 90, 'visit', 50000, 20000, '2026-11-10 10:40:00', 'card')"
    )
    conn.executemany(
        "INSERT INTO emr_patient_alerts (patient_id, code, source, recorded_at, ends_at)"
        " VALUES (?, ?, 'intake', ?, ?)",
        [
            (1, "ANTICOAG", "2026-01-01", None),  # 발치와 관련
            (1, "GAG", "2026-01-01", None),  # 발치와 무관
            (2, "PREGNANT", "2026-06-01", "2026-12-01"),  # 기준일부터 안 보임
            (2, "LATEX", "2026-12-01", None),  # 기록일 당일부터 보임
        ],
    )
    conn.commit()
    return conn


@pytest.fixture
def src(tmp_path):
    """앱 연결처럼 source를 `source`로 붙인 연결 — 테스트의 INSERT는 이름만으로 source에 들어간다"""
    make_source(tmp_path / "source.sqlite").close()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (str(tmp_path / "source.sqlite"),))
    yield conn
    conn.close()


def rows(conn, day=TODAY):
    return {r["appointment_id"]: r for r in prep.prep_facts(conn, day, TODAY)}


def test_alert_related_only_for_planned_procedure(src):
    r = rows(src)[101]
    assert [(a["code"], a["related"]) for a in r["alerts"]] == [
        ("ANTICOAG", True),
        ("GAG", False),
    ]
    assert r["alerts"][0]["action"] == ALERT_TYPES["ANTICOAG"]["action"]
    assert "alert" in r["chips"]


def test_alert_window(src):
    r = rows(src)[102]
    assert [a["code"] for a in r["alerts"]] == ["LATEX"]  # 임신은 끝나는 날부터 안 보임
    assert "alert" in r["chips"]  # 라텍스는 스케일링과 관련


def test_scaling_chip_off_when_scaling_today(src):
    assert rows(src)[102]["scaling_unused"] is False
    assert "insurance" not in rows(src)[102]["chips"]


def test_scaling_chip_adult_only(src):
    r = rows(src)
    assert r[101]["scaling_unused"] is True and "insurance" in r[101]["chips"]
    assert r[103]["scaling_unused"] is False  # 16세


def test_scaling_chip_off_after_claim_this_year(src):
    src.execute(
        "INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,"
        " performed_at, fee_total, insured_amount, patient_amount)"
        " VALUES (3, 80, 1, 'U0001', '00', '2026-11-15 10:00:00', 0, 0, 0)"
    )
    src.execute(
        "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month,"
        " service_date, procedure_code, claim_amount, status)"
        " VALUES (3, 1, 'C-00001', '00', '2026-11', '2026-11-15', 'U0001', 0, 'paid')"
    )
    assert rows(src)[101]["scaling_unused"] is False


def test_family_recall_excludes_booked_member(src):
    r = rows(src)
    # 김철수의 가족: 박영희는 오늘 위생 예약이 있어 빠지고 김민수만
    assert [m["name"] for m in r[101]["family_recall"]] == ["김민수"]
    assert r[101]["family_recall"][0]["last_hygiene"] == "2025-10-01"
    assert "family_recall" in r[101]["chips"]
    src.execute(
        APPT, (105, 3, 2, "2026-12-08 10:00:00", "hygiene", "U0001", "scheduled", 0, 2, None)
    )
    assert rows(src)[101]["family_recall"] == []


def test_open_plan_as_of_day(src):
    today = rows(src)[101]
    # 11번은 오늘 예정이라 빠지고, 13번은 11/20에 대체됐고, 계획 2(11/15)는 그대로 열림
    assert [(p["procedure"], p["tooth"]) for p in today["open_plan"]] == [
        ("크라운", "46"),
        ("잇몸치료", "00"),
    ]
    past = rows(src, date(2026, 11, 15))[80]
    # 11/15 아침: 13번은 아직 열려 있고(대체는 11/20), 그날 만든 계획 2는 아직 없다
    assert [p["tooth"] for p in past["open_plan"]] == ["36", "46", "17"]


def test_new_patient_and_empty_facts(src):
    r = rows(src)[103]
    assert r["last_visit"] is None and r["patient"]["new_patient"] is True
    assert r["chair_no"] is None and r["balance"] == 0
    assert r["chips"] == ["appointment", "unconfirmed"]


def test_balance_and_last_visit_before_day(src):
    r = rows(src)[101]
    assert r["balance"] == 30000 and r["last_visit"] == "2026-11-20"
    past = rows(src, date(2026, 11, 15))[80]
    assert past["last_visit"] == "2026-11-10" and past["balance"] == 30000


def test_unconfirmed_only_from_today(src):
    assert "unconfirmed" in rows(src)[101]["chips"]
    assert "unconfirmed" not in rows(src, date(2026, 11, 15))[80]["chips"]


def test_cancelled_excluded_and_time_order(src):
    assert [r["appointment_id"] for r in prep.prep_facts(src, TODAY, TODAY)] == [101, 102, 103]


def test_no_appointments_day_is_empty(src):
    assert prep.prep_facts(src, date(2026, 12, 2), TODAY) == []


def test_snapshot(src):
    got = json.loads(json.dumps(prep.prep_facts(src, TODAY, TODAY), ensure_ascii=False))
    assert got == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


# ── 제안·후검증·저장 (Task 3) ──
from shared.llm import LLMResult  # noqa: E402

MODEL = "claude-sonnet-5-5"


def res(data, ok=True, model=MODEL):
    return LLMResult(ok, "", data if ok else None, model, False, 1, 1, 0.001, 1)


SUGGEST = {
    "action": "suggest",
    "text": "항응고제 복용 확인, 미수 3만 원 안내",
    "used_facts": [
        {"key": "alert.ANTICOAG", "value": "항응고제 복용 — 복용 여부 확인, 지혈 준비"},
        {"key": "balance", "value": "30000"},
    ],
}
NONE = {"action": "none", "text": "", "used_facts": []}
BAD = {
    "action": "suggest",
    "text": "미수 4만 원",
    "used_facts": [{"key": "balance", "value": "40000"}],
}


class FakeGw:
    """complete는 대본 순서대로, batch는 custom_id → 답(없으면 빠진 건)"""

    config = {"aliases": {"chat": [MODEL]}, "models": {MODEL: {}}}

    def __init__(self, *turns, batch=None):
        self.turns, self.batch, self.calls, self.batches = list(turns), batch, [], []

    def complete(self, alias, **kw):
        self.calls.append(kw)
        return self.turns.pop(0)

    def submit_batch(self, alias, requests, **kw):
        self.batches.append(requests)
        return "b1"

    def batch_status(self, batch_id):
        return "ended"

    def batch_results(self, batch_id, alias, **kw):
        return {r["custom_id"]: self.batch(r["custom_id"]) for r in self.batches[-1]}


@pytest.fixture
def app(tmp_path):
    from backend import db

    make_source(tmp_path / "source.sqlite").close()
    conn = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    db.init_schema(conn)
    yield conn
    conn.close()


def facts_101(conn):
    return prep.llm_facts(rows(conn)[101])


def test_llm_facts_has_no_confirmed(app):
    f = facts_101(app)
    assert not any("confirm" in k for k in f)
    assert f["balance"] == 30000 and f["alert.ANTICOAG"].startswith("항응고제 복용 — ")
    assert f["alert.GAG"] == "구역반사"  # 무관한 주의사항은 행동 없이


def test_check_suggestion_rejects_unknown_key_and_number(app):
    f = facts_101(app)
    assert prep.check_suggestion(f, SUGGEST) is True
    assert prep.check_suggestion(f, BAD) is False
    wrong_key = SUGGEST | {
        "used_facts": [{"key": "alert.HTN", "value": "고혈압"}],
        "text": "혈압 확인",
    }
    assert prep.check_suggestion(f, wrong_key) is False


def test_check_suggestion_units(app):
    f = facts_101(app)
    used = [{"key": "balance", "value": "30000"}]
    for text, ok in [("미수 3만 원", True), ("미수 30,000원", True), ("미수 4만 원", False)]:
        assert (
            prep.check_suggestion(f, {"action": "suggest", "text": text, "used_facts": used}) is ok
        )


def test_none_requires_empty(app):
    f = facts_101(app)
    assert prep.check_suggestion(f, NONE) is True
    assert prep.check_suggestion(f, NONE | {"used_facts": SUGGEST["used_facts"]}) is False


def _saved(conn):
    return {
        r["appointment_id"]: r
        for r in conn.execute("SELECT * FROM ax_prep_sheets ORDER BY appointment_id")
    }


def test_build_retries_once_then_null(app):
    gw = FakeGw(res(BAD), res(BAD), res(NONE), res(NONE))
    out = prep.build(app, gw, [TODAY], TODAY, use_batch=False)
    row = _saved(app)[101]
    assert row["suggestion"] is None and row["suggestion_valid"] == 0
    assert out["failed"] == 1 and len(gw.calls) == 4  # 101은 두 번, 102·103은 한 번씩


def test_build_skips_same_hash_valid_row(app):
    prep.build(app, FakeGw(res(SUGGEST), res(NONE), res(NONE)), [TODAY], TODAY, use_batch=False)
    gw = FakeGw()
    out = prep.build(app, gw, [TODAY], TODAY, use_batch=False)
    assert gw.calls == [] and out["skipped"] == 3


def test_build_recalls_failed_row(app):
    prep.build(
        app, FakeGw(res(BAD), res(BAD), res(NONE), res(NONE)), [TODAY], TODAY, use_batch=False
    )
    gw = FakeGw(res(SUGGEST))
    prep.build(app, gw, [TODAY], TODAY, use_batch=False)
    assert len(gw.calls) == 1 and _saved(app)[101]["suggestion_valid"] == 1


def test_build_recalls_on_prompt_version_change(app, monkeypatch):
    prep.build(app, FakeGw(res(SUGGEST), res(NONE), res(NONE)), [TODAY], TODAY, use_batch=False)
    app.execute("UPDATE ax_prep_sheets SET prompt_version = 'v0'")
    gw = FakeGw(res(SUGGEST), res(NONE), res(NONE))
    prep.build(app, gw, [TODAY], TODAY, use_batch=False)
    assert len(gw.calls) == 3


def test_build_batch_first_then_plain_retry(app):
    answers = {"101": res(BAD), "102": res(NONE), "103": res(NONE)}
    gw = FakeGw(res(SUGGEST), batch=lambda cid: answers[cid.split("_")[1]])
    out = prep.build(app, gw, [TODAY], TODAY, use_batch=True)
    assert len(gw.batches) == 1 and len(gw.batches[0]) == 3
    # Anthropic Batch custom_id 허용 글자
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", r["custom_id"]) for r in gw.batches[0])
    assert len(gw.calls) == 1  # 실패한 101만 일반 호출로 한 번 더
    assert _saved(app)[101]["suggestion_valid"] == 1 and out["called"] == 3


def test_build_batch_missing_falls_back(app):
    gw = FakeGw(res(SUGGEST), batch=lambda cid: None if cid.endswith("_101") else res(NONE))
    prep.build(app, gw, [TODAY], TODAY, use_batch=True)
    assert len(gw.calls) == 1 and _saved(app)[101]["suggestion_valid"] == 1


def test_build_empty_day_no_calls(app):
    gw = FakeGw()
    out = prep.build(app, gw, [date(2026, 12, 2)], TODAY, use_batch=True)
    assert gw.calls == [] and gw.batches == [] and out["rows"] == 0


def test_build_input_has_no_confirmed(app):
    gw = FakeGw(res(NONE), res(NONE), res(NONE))
    prep.build(app, gw, [TODAY], TODAY, use_batch=False)
    assert all("confirm" not in c["messages"][0]["content"] for c in gw.calls)
