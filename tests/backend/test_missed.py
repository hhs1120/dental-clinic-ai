"""⑨ 미예약 환자 — 그날 아침 기준 사실·칩·점수(backend/missed.py). 설계 docs/superpowers/specs/
2026-11-10-missed-patients-design.md §1~§3. 손으로 만든 source(실제 스키마 + master 값)로 칩마다 켜짐·꺼짐을 보고,
규칙이 ④ 준비표 도우미와 같은지 대조한다."""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from backend import db, missed, prep
from generator.clinic import master_values_rows
from generator.master import ADJUST_REASONS, PROCEDURES, SCALING_CODE
from tests.backend.test_prep import APPT, make_source

ROOT = Path(__file__).resolve().parents[2]
DAY = date(2026, 12, 1)
NOW = datetime(2026, 11, 10, 9, 0, 0)
EXTERNAL = next(r["note"] for r in ADJUST_REASONS.values() if r["type"] == "external")


class Source:
    """손으로 만드는 source.sqlite — 환자·예약·청구·수납·문자·치료계획을 한 줄씩 더한다"""

    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript((ROOT / "shared" / "source_schema.sql").read_text("utf-8"))
        cols = list(PROCEDURES[0])
        self.conn.executemany(
            f"INSERT INTO emr_procedures ({', '.join(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})",
            [tuple(p[c] for c in cols) for p in PROCEDURES],
        )
        self.conn.executemany("INSERT INTO ax_master_values VALUES (?, ?)", master_values_rows())
        self.conn.execute("INSERT INTO emr_staff (id, name, role) VALUES (1, '강태현', 'doctor')")

    def patient(self, pid: int, birth: str = "1980-01-01", months: int = 6) -> int:
        self.conn.execute(
            "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone,"
            " registered_at, recall_cycle_months) VALUES (?, ?, ?, ?, 'F', ?, '2020-01-01', ?)",
            (pid, f"C-{pid:05d}", f"환자{pid}", birth, f"010-0000-{pid:04d}", months),
        )
        return pid

    def appt(
        self,
        pid: int,
        day: str,
        status: str = "completed",
        type_: str = "treatment",
        created: str = "2020-01-01 09:00:00",
        cancelled: str | None = None,
        plan_item: int | None = None,
    ) -> int:
        code = SCALING_CODE if type_ == "hygiene" else "U0005"
        return self.conn.execute(
            "INSERT INTO emr_appointments (patient_id, staff_id, scheduled_at, duration_min, type,"
            " planned_procedure_code, status, booked_via, created_at, cancelled_at, plan_item_id)"
            " VALUES (?, 1, ?, 30, ?, ?, ?, 'phone', ?, ?, ?)",
            (pid, f"{day} 10:00:00", type_, code, status, created, cancelled, plan_item),
        ).lastrowid

    def treatment(self, pid: int, appt: int, day: str, code: str, tooth: str) -> int:
        return self.conn.execute(
            "INSERT INTO emr_treatments (appointment_id, patient_id, procedure_code, tooth_no,"
            " performed_at, fee_total, insured_amount, patient_amount)"
            " VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
            (appt, pid, code, tooth, f"{day} 10:00:00"),
        ).lastrowid

    def claim(
        self,
        pid: int,
        day: str,
        code: str = SCALING_CODE,
        status: str = "paid",
        note: str | None = None,
        reviewed: str | None = None,
        created: str = "2020-01-01 09:00:00",
    ) -> None:
        type_ = "hygiene" if code == SCALING_CODE else "treatment"
        a = self.appt(pid, day, type_=type_, created=created)
        t = self.treatment(pid, a, day, code, "00")
        self.conn.execute(
            "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month,"
            " service_date, procedure_code, claim_amount, status, adjust_code, adjust_note,"
            " reviewed_at) VALUES (?, ?, ?, '00', ?, ?, ?, 10000, ?, ?, ?, ?)",
            (
                t,
                pid,
                f"C-{pid:05d}",
                day[:7],
                day,
                code,
                status,
                "C" if note else None,
                note,
                reviewed,
            ),
        )

    def payment(self, pid: int, day: str, due: int, paid: int) -> None:
        self.conn.execute(
            "INSERT INTO emr_payments (patient_id, kind, amount_due, amount_paid, paid_at, method)"
            " VALUES (?, 'visit', ?, ?, ?, 'card')",
            (pid, due, paid, f"{day} 11:00:00"),
        )

    def message(self, pid: int, at: str, trigger: str) -> None:
        self.conn.execute(
            'INSERT INTO emr_messages (patient_id, direction, sent_at, body, "trigger")'
            " VALUES (?, ?, ?, '', ?)",
            (pid, "in" if trigger == "reply" else "out", at, trigger),
        )

    def plan(self, pid: int, created: str, items: list[tuple[str, str]]) -> list[int]:
        """치료계획 하나(만든 예약 포함) — items = (시술코드, 치아), 항목 id 목록"""
        a = self.appt(pid, created[:10])
        plan = self.conn.execute(
            "INSERT INTO emr_treatment_plans (patient_id, created_at, created_appointment_id)"
            " VALUES (?, ?, ?)",
            (pid, created, a),
        ).lastrowid
        return [
            self.conn.execute(
                "INSERT INTO emr_treatment_plan_items (plan_id, seq, procedure_code, tooth_no,"
                " est_fee, status) VALUES (?, ?, ?, ?, 100000, 'open')",
                (plan, i, code, tooth),
            ).lastrowid
            for i, (code, tooth) in enumerate(items, 1)
        ]

    def app(self) -> sqlite3.Connection:
        """앱 연결(source를 읽기 전용으로 붙임) — 쓰기를 끝내고 연다"""
        self.conn.commit()
        self.conn.close()
        conn = db.connect(self.path.parent / "app.sqlite", self.path)
        db.init_schema(conn)
        return conn


@pytest.fixture
def src(tmp_path):
    return Source(tmp_path / "source.sqlite")


def facts(conn, day=DAY) -> dict[int, dict]:
    return {r["patient_id"]: r for r in missed.facts(conn, day)}


# ── ④ 규칙과 같음 ───────────────────────────────────────────────


@pytest.fixture
def prep_src(tmp_path):
    """④ 준비표 테스트 source에 미수·스케일링 청구와 치료계획 경계 사례를 더한 앱 연결.
    김철수(1): 11/18 검진(전체 구강 '00', 다른 시술코드 — 계획 2의 U0018 항목을 대체하지 않음),
    11/22 같은 계획(1)의 46번 항목 완료(같은 치아의 항목 12를 대체하지 않음), 11/24 다른 계획(2)의 36번 항목
    완료(계획 1의 36번 항목 11을 대체)"""
    s = make_source(tmp_path / "source.sqlite")
    s.execute(
        "INSERT INTO emr_payments (patient_id, kind, amount_due, amount_paid, paid_at, method)"
        " VALUES (2, 'visit', 40000, 0, '2025-11-01 11:00:00', 'card'),"
        " (1, 'settlement', 0, 10000, '2026-11-20 11:00:00', 'card')"
    )
    s.execute(
        "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month,"
        " service_date, procedure_code, claim_amount, status)"
        " VALUES (1, 3, 'C-00003', '00', '2026-11', '2026-11-12', ?, 10000, 'paid')",
        (SCALING_CODE,),
    )
    s.executemany(
        "INSERT INTO emr_treatment_plan_items VALUES (?, ?, ?, ?, ?, 80000, 'open')",
        [(14, 1, 4, "U0005", "46"), (22, 2, 2, "U0005", "36")],
    )
    s.executemany(
        APPT,
        [
            (96, 1, 1, "2026-11-18 10:00:00", "consult", "U0002", "completed", 1, 1, None),
            (97, 1, 1, "2026-11-22 10:00:00", "treatment", "U0005", "completed", 1, 1, 14),
            (98, 1, 1, "2026-11-24 10:00:00", "treatment", "U0005", "completed", 1, 1, 22),
        ],
    )
    s.executemany(
        "INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,"
        " performed_at, fee_total, insured_amount, patient_amount) VALUES (?, ?, 1, ?, ?, ?, 0, 0, 0)",
        [
            (3, 96, "U0002", "00", "2026-11-18 10:00:00"),
            (4, 97, "U0005", "46", "2026-11-22 10:00:00"),
            (5, 98, "U0005", "36", "2026-11-24 10:00:00"),
        ],
    )
    s.commit()
    s.close()
    conn = db.connect(tmp_path / "app.sqlite", tmp_path / "source.sqlite")
    yield conn
    conn.close()


def test_same_as_prep_rules(prep_src):
    """스케일링 급여 미사용·남은 치료계획·잔액·리콜 연체가 ④ 도우미와 같다(테스트에서만 ④ 도우미를 부른다).
    비교일 = 계획을 만든 날(11/10·11/15 — 그날 만든 계획은 아직 없음), 예약을 취소한 날(11/28 — 그날 아침엔
    잡혀 있음), 대체·완료 진료 앞뒤"""
    mv = prep.master_values(prep_src)
    pats = prep_src.execute("SELECT * FROM source.emr_patients").fetchall()
    days = [date(2026, 11, d) for d in (10, 12, 15, 16, 19, 21, 23, 25, 28)] + [DAY]
    want_plans = {}
    for day in days:
        d = day.isoformat()
        claims = missed._claims(prep_src, d, mv)
        plans = missed._open_plans(prep_src, d)
        balances = missed._balances(prep_src, d)
        visits = missed._visits(prep_src, d)
        for p in pats:
            scaled = claims.get(p["id"], {}).get("scaled", False)
            want = prep._scaling_unused(prep_src, p, day, "", mv)
            assert missed._scaling_unused(p["birth_date"], scaled, day, mv) == want, (day, p["id"])
            want_plan = len(prep._open_plan(prep_src, p["id"], day, None))
            assert plans.get(p["id"], 0) == want_plan, (day, p["id"])
            if p["id"] == 1:
                want_plans[d] = want_plan
            want_bal = prep_src.execute(
                "SELECT COALESCE(SUM(amount_due - amount_paid), 0) FROM source.emr_payments"
                " WHERE patient_id = ? AND date(paid_at) < ?",
                (p["id"], d),
            ).fetchone()[0]
            assert balances.get(p["id"], 0) == want_bal
        # 가족 리콜(④) = 같은 가족 중 리콜 연체이고 그날 아침 예약이 없는 사람
        p1 = next(p for p in pats if p["id"] == 1)
        got = {
            f"C-{pid:05d}"
            for pid, v in visits.items()
            if pid != 1
            and v["last_hygiene"]
            and missed._recall_overdue(v["last_hygiene"], v["months"], day)
            and not v["booked"]
        }
        assert got == {m["chart_no"] for m in prep._family_recall(prep_src, p1, day)}, day
    # ④ 도우미와 같을 뿐 아니라 경계 사례가 실제로 걸린다(둘이 같이 틀리지 않게) — 김철수의 열린 항목 수
    assert want_plans == {
        "2026-11-10": 0,  # 그날 만든 계획 1은 아직
        "2026-11-12": 4,  # 계획 1: 11·12·13·14
        "2026-11-15": 4,  # 그날 만든 계획 2는 아직
        "2026-11-16": 6,  # + 계획 2: 21(전체 구강)·22
        "2026-11-19": 6,  # 11/18 '00' 검진은 다른 시술코드 — 21 그대로
        "2026-11-21": 5,  # 13은 11/20 계획 밖 진료로 대체
        "2026-11-23": 4,  # 14 끝남 — 같은 치아(46)의 12는 같은 계획 진료라 그대로
        "2026-11-25": 2,  # 22 끝남, 11은 다른 계획(2)의 36번 진료로 대체 → 12·21
        "2026-11-28": 2,
        "2026-12-01": 2,
    }
    family = {
        d: {m["chart_no"] for m in prep._family_recall(prep_src, p1, date.fromisoformat(d))}
        for d in ("2026-11-28", "2026-11-29")
    }
    assert family == {
        "2026-11-28": set(),
        "2026-11-29": {"C-00003"},
    }  # 11/28 취소 — 그날 아침엔 잡혀 있음


# ── 목록에 오르는 조건 ─────────────────────────────────────────


def test_future_appointment_excludes(src):
    src.patient(1)
    src.appt(1, "2026-06-01")
    src.appt(1, "2026-12-10", status="scheduled")
    src.patient(2)
    src.appt(2, "2026-06-01")
    src.appt(2, "2026-12-10", status="cancelled", cancelled="2026-11-20 10:00:00")
    got = facts(src.app())
    assert 1 not in got and 2 in got  # 기준일 전에 취소된 예약은 예약이 아니다


def test_today_appointment_counts_as_booked(src):
    src.patient(1)
    src.appt(1, "2026-06-01")
    src.appt(1, "2026-12-01", status="scheduled")
    assert 1 not in facts(src.app())


def test_no_completed_visit_excluded(src):
    src.patient(1)
    src.appt(1, "2026-06-01", status="noshow")
    assert 1 not in facts(src.app())


def test_no_chip_excluded(src):
    src.patient(1, birth="2015-01-01", months=12)  # 소아 — 스케일링 칩 없음
    src.appt(1, "2026-06-01", type_="hygiene")  # 리콜 기한 2027-06
    assert facts(src.app()) == {}


def test_facts_before_day_only(src):
    """그날 사건은 그날 아침에 안 보인다 — 그날 만든 당일 내원·수납·청구·문자"""
    src.patient(1)
    src.appt(1, "2026-06-01")
    walk_in = "2026-11-30 09:30:00"  # 그날 만든 당일 내원 — 아침엔 없던 예약
    src.claim(1, "2026-11-30", created=walk_in)
    src.payment(1, "2026-11-30", 50000, 0)
    for _ in range(missed.NO_REPLY_RUN):
        src.message(1, "2026-11-30 09:00:00", "recall")
    conn = src.app()
    before = facts(conn, date(2026, 11, 30))[1]
    assert before["last_visit"] == "2026-06-01"
    assert before["balance"] == 0
    assert "scaling_unused" in before["chips"]
    assert before["signals"]["no_reply_run"] == 0
    after = facts(conn)[1]
    assert after["last_visit"] == "2026-11-30"
    assert after["balance"] == 50000
    assert "scaling_unused" not in after["chips"]
    assert after["signals"]["no_reply_run"] == missed.NO_REPLY_RUN


# ── 칩 ──────────────────────────────────────────────────────────


def test_recall_overdue_chip(src):
    src.patient(1, months=6)
    src.appt(1, "2026-05-01", type_="hygiene")  # 기한 ≈ 2026-10-31
    src.patient(2, months=12)
    src.appt(2, "2026-05-01", type_="hygiene")  # 기한 2027-05
    src.appt(2, "2026-11-01")
    got = facts(src.app())
    assert "recall" in got[1]["chips"]
    assert got[1]["last_hygiene"] == "2026-05-01"
    assert "recall" not in got[2]["chips"]


def test_scaling_unused_chip(src):
    src.patient(1)  # 성인, 올해 스케일링 없음
    src.appt(1, "2026-06-01")
    src.patient(2)
    src.claim(2, "2026-03-01")  # 올해 받음
    src.patient(3)
    src.claim(3, "2025-03-01")  # 작년만
    src.patient(4, birth="2010-01-01")  # 미성년
    src.appt(4, "2026-06-01")
    got = facts(src.app())
    assert "scaling_unused" in got[1]["chips"]
    assert 2 not in got or "scaling_unused" not in got[2]["chips"]
    assert "scaling_unused" in got[3]["chips"]
    assert 4 not in got or "scaling_unused" not in got[4]["chips"]


def test_noshow_without_rebook_chip(src):
    src.patient(1)
    src.appt(1, "2026-06-01")
    src.appt(1, "2026-10-01", status="noshow")
    src.patient(2)
    src.appt(2, "2026-06-01", status="noshow")
    src.appt(2, "2026-07-01")  # 노쇼 뒤 다시 와서 끝남
    got = facts(src.app())
    assert {"noshow", "other_clinic"} <= set(got[1]["chips"])
    assert "noshow" not in got[2]["chips"]


def test_balance_chip(src):
    src.patient(1)
    src.appt(1, "2026-06-01")
    src.payment(1, "2026-06-01", 80000, 30000)
    src.patient(2)
    src.appt(2, "2026-06-01")
    src.payment(2, "2026-06-01", 30000, 50000)  # 더 낸 금액은 미수가 아니다
    got = facts(src.app())
    assert got[1]["balance"] == 50000 and "balance" in got[1]["chips"]
    assert got[2]["balance"] == 0 and "balance" not in got[2]["chips"]


def test_open_plan_chip(src):
    src.patient(1)
    done, left = src.plan(1, "2026-08-01 10:30:00", [("U0005", "16"), ("U0009", "46")])
    src.appt(1, "2026-08-15", plan_item=done)
    src.patient(2)
    (item,) = src.plan(2, "2026-08-01 10:30:00", [("U0005", "26")])
    a = src.appt(2, "2026-09-01")  # 계획 밖 진료가 같은 치아에 — 대체됨
    src.treatment(2, a, "2026-09-01", "U0005", "26")
    got = facts(src.app())
    assert "open_plan" in got[1]["chips"]
    assert got[1]["signals"]["open_plan_items"] == 1
    assert "open_plan" not in got[2]["chips"]


def test_other_clinic_by_external_claim(src):
    src.patient(1)
    src.claim(1, "2026-03-01", status="denied", note=EXTERNAL, reviewed="2026-05-10")
    src.patient(2)
    src.claim(
        2, "2026-03-01", status="denied", note="상병과 시행 행위 불일치", reviewed="2026-05-10"
    )
    src.patient(3)
    src.claim(
        3, "2026-03-01", status="denied", note=EXTERNAL, reviewed="2026-12-05"
    )  # 그날 뒤 통보
    src.patient(4)
    src.claim(4, "2026-03-01", status="adjusted", note=EXTERNAL, reviewed=DAY.isoformat())
    got = facts(src.app())
    assert "other_clinic" in got[1]["chips"] and got[1]["signals"]["external_claim"]
    assert "other_clinic" not in got[2]["chips"]
    assert "other_clinic" not in got[3]["chips"]
    assert not got[4]["signals"]["external_claim"]  # 그날 통보는 그날 아침엔 없다


def test_other_clinic_by_no_reply_run(src):
    src.patient(1)
    src.appt(1, "2026-03-01")
    src.message(1, "2026-02-20 09:00:00", "reply")
    for m, t in enumerate(("appt_confirm", "reminder", "recall", "recall")[: missed.NO_REPLY_RUN]):
        src.message(1, f"2026-0{m + 3}-01 09:00:00", t)
    src.patient(2)
    src.appt(2, "2026-03-01")
    for m, t in enumerate(("appt_confirm", "reminder", "recall", "recall")[: missed.NO_REPLY_RUN]):
        src.message(2, f"2026-0{m + 3}-01 09:00:00", t)
    src.message(2, "2026-08-01 09:00:00", "reply")  # 마지막에 답함
    got = facts(src.app())
    assert got[1]["signals"]["no_reply_run"] == missed.NO_REPLY_RUN
    assert "other_clinic" in got[1]["chips"]
    assert got[2]["signals"]["no_reply_run"] == 0
    assert "other_clinic" not in got[2]["chips"]


# ── 점수 ────────────────────────────────────────────────────────


def _row(**kw) -> dict:
    base = {
        "chips": ["scaling_unused"],
        "balance": 0,
        "signals": {
            "months_since_visit": 2.0,
            "no_reply_run": 0,
            "external_claim": False,
            "open_plan_items": 0,
        },
    }
    signals = base["signals"] | kw.pop("signals", {})
    return base | kw | {"signals": signals}


def test_score_recent_visit_higher():
    w = missed.WEIGHTS
    recent = missed.score(_row(), None, NOW, w)
    old = missed.score(_row(signals={"months_since_visit": 30.0}), None, NOW, w)
    assert recent > old


def test_score_other_clinic_lower():
    w = missed.WEIGHTS
    chips = ["scaling_unused", "recall", "open_plan", "other_clinic"]
    many = missed.score(_row(chips=chips, signals={"external_claim": True}), None, NOW, w)
    few = missed.score(_row(chips=["scaling_unused", "open_plan"]), None, NOW, w)
    assert many < few  # 칩이 많아도 다른 치과 흔적이 있으면 내려간다


def test_balance_capped():
    w = missed.WEIGHTS
    rich = missed.score(_row(chips=["balance"], balance=10_000_000), None, NOW, w)
    plan = missed.score(_row(chips=["open_plan"]), None, NOW, w)
    assert rich < plan
    assert rich == missed.score(_row(chips=["balance"], balance=5_000_000), None, NOW, w)


def test_reasons_cover_weights():
    """값마다 이유 한 구절(설계 §5) — 화면 "순위는 어떻게 정해지나요?"가 같은 문구를 쓴다"""
    assert set(missed.REASONS) == set(missed.WEIGHTS)
    assert all(r.strip() for r in missed.REASONS.values())
    text = json.dumps(missed.rules(missed.WEIGHTS), ensure_ascii=False)
    assert all(r in text for r in missed.REASONS.values())


def test_weights_override_from_settings(src):
    conn = src.app()
    assert missed.weights(conn) == missed.WEIGHTS
    db.set_setting(conn, "missed_weights", {"open_plan": 50, "모르는 키": 1})
    got = missed.weights(conn)
    assert got["open_plan"] == 50
    assert got == missed.WEIGHTS | {"open_plan": 50}


# ── 쿼리 수 ─────────────────────────────────────────────────────


def _fill(src: Source, n: int) -> None:
    """환자 n명 — 모든 칩의 원천(예약·청구·수납·문자·치료계획)을 한 줄 이상씩"""
    for pid in range(1, n + 1):
        src.patient(pid)
        src.appt(pid, "2026-05-01", type_="hygiene")
        src.appt(pid, "2026-09-01", status="noshow")
        src.payment(pid, "2026-05-01", 30000, 10000)
        src.claim(pid, "2026-03-01", status="denied", note=EXTERNAL, reviewed="2026-05-10")
        src.message(pid, "2026-08-01 09:00:00", "recall")
        src.plan(pid, "2026-05-01 10:30:00", [("U0005", "16")])


def _count(conn) -> int:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    missed.facts(conn, DAY)
    conn.set_trace_callback(None)
    return len(seen)


def test_query_count_independent_of_patients(tmp_path):
    counts = []
    for n in (10, 200):
        (tmp_path / str(n)).mkdir()
        s = Source(tmp_path / str(n) / "source.sqlite")
        _fill(s, n)
        conn = s.app()
        assert len(missed.facts(conn, DAY)) == n
        counts.append(_count(conn))
        conn.close()
    assert counts[0] == counts[1]


# ── 연락 기록과 목록 (설계 §4) ─────────────────────────────────


@pytest.fixture
def three(src):
    """미예약 환자 셋 — 1은 최근 방문 + 진료계획, 2·3은 오래전 방문(스케일링 칩만)"""
    for pid in (1, 2, 3):
        src.patient(pid)
    src.appt(1, "2026-10-01")
    src.plan(1, "2026-10-01 10:30:00", [("U0009", "46")])
    src.appt(2, "2025-06-01")
    src.appt(3, "2025-01-01")
    conn = src.app()
    yield conn
    conn.close()


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _sheet(conn, now=NOW) -> dict[int, dict]:
    return {r["patient_id"]: r for r in missed.sheet(conn, DAY, now)["rows"]}


def _record(conn, pid, result, days_ago=0.0, channel="call"):
    missed.record_contact(conn, pid, result, channel, by="데스크", at=_at(days_ago), day=DAY)
    conn.commit()


def test_sheet_order_and_rank(three):
    s = missed.sheet(three, DAY, NOW)
    assert [r["patient_id"] for r in s["rows"]] == [1, 2, 3]
    assert [r["rank"] for r in s["rows"]] == [1, 2, 3]
    assert s["date"] == "2026-12-01"
    assert s["page"] == missed.PAGE  # 화면의 처음 몇 명·더보기 단위
    assert all(r["contact"] is None for r in s["rows"])
    assert s["rules"] == missed.rules(missed.WEIGHTS)


def test_booked_contact_removes(three):
    _record(three, 1, "booked", days_ago=400)  # 기간 없이
    assert 1 not in _sheet(three)


def test_declined_removes_for_180_days(three):
    _record(three, 1, "declined", days_ago=179)
    _record(three, 2, "declined", days_ago=181)
    got = _sheet(three)
    assert 1 not in got
    assert got[2]["contact"]["result"] == "declined"


def test_other_clinic_contact_removes(three):
    _record(three, 1, "other_clinic", days_ago=400)
    assert 1 not in _sheet(three)


def test_no_answer_lowers_for_7_days(three):
    base = _sheet(three)[1]["score"]
    _record(three, 1, "no_answer", days_ago=6)
    got = _sheet(three)[1]
    assert got["score"] == base + missed.WEIGHTS["no_answer"]
    assert got["contact"] == {
        "result": "no_answer",
        "channel": "call",
        "by": "데스크",
        "at": _at(6),
        "active": True,
    }
    later = NOW.replace(day=NOW.day + 2)  # 기록 8일 뒤
    assert _sheet(three, later)[1]["score"] == base


def test_later_lowers_for_14_days(three):
    base = _sheet(three)[1]["score"]
    _record(three, 1, "later", days_ago=13)
    assert _sheet(three)[1]["score"] == base + missed.WEIGHTS["later"]
    _record(three, 2, "later", days_ago=15)
    assert _sheet(three)[2]["contact"]["result"] == "later"


def test_repeated_no_answer_is_no_reply(three):
    """부재중이 여러 번 겹치면 무응답 신호(설계 §4)"""
    base = _sheet(three)[3]["score"]
    for days in (30, 20):
        _record(three, 3, "no_answer", days_ago=days)
    assert _sheet(three)[3]["score"] == base + missed.WEIGHTS["no_reply"]


def test_no_answer_counts_only_since_other_result(three):
    """부재중 누적은 가장 최근의 다른 결과 뒤 연달아 남은 것만 — 그 앞 부재중은 세지 않는다"""
    base = _sheet(three)[3]["score"]
    _record(three, 3, "no_answer", days_ago=30)
    _record(three, 3, "later", days_ago=25)
    _record(three, 3, "no_answer", days_ago=20)
    assert _sheet(three)[3]["score"] == base
    _record(three, 3, "no_answer", days_ago=10)
    assert _sheet(three)[3]["score"] == base + missed.WEIGHTS["no_reply"]


def test_contact_active_flag(three):
    """contact.active = 최근 연락의 효과(감점 기간)가 아직 있음 — 화면 "연락함 숨기기"가 이것만 숨긴다"""
    _record(three, 1, "no_answer", days_ago=6)
    _record(three, 2, "later", days_ago=13)
    _record(three, 3, "declined", days_ago=181)  # 빠지는 기간이 지나 다시 목록에
    got = _sheet(three)
    assert [got[p]["contact"]["active"] for p in (1, 2, 3)] == [True, True, False]
    two_days = _sheet(three, NOW + timedelta(days=2))  # 부재중 8일, 나중에 다시 15일
    assert [two_days[p]["contact"]["active"] for p in (1, 2)] == [False, False]


def test_latest_contact_wins(three):
    _record(three, 1, "booked", days_ago=10)
    _record(three, 1, "later", days_ago=1)  # 고쳐 남김
    assert _sheet(three)[1]["contact"]["result"] == "later"
    _record(three, 2, "later", days_ago=10)
    _record(three, 2, "booked", days_ago=1)
    assert 2 not in _sheet(three)


def test_record_contact_rejects_unknown(three):
    for args in ((1, "maybe", "call"), (1, "booked", "fax"), (99, "booked", "call")):
        with pytest.raises(ValueError):
            missed.record_contact(three, *args, by="데스크", at=_at(0), day=DAY)
    assert three.execute("SELECT COUNT(*) FROM ax_missed_contacts").fetchone()[0] == 0


def test_chip_counts_before_hiding(three):
    _record(three, 2, "no_answer")  # 남음 — 화면의 연락함 숨기기 대상
    _record(three, 3, "booked")  # 빠짐
    s = missed.sheet(three, DAY, NOW)
    assert s["chip_counts"]["scaling_unused"] == 2
    assert s["chip_counts"]["open_plan"] == 1
    assert set(s["chip_counts"]) == set(missed.CHIPS)
