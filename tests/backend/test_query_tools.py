"""조회 도구 (앱 설계 §3 — 시그니처 고정, 정의 원본). 값은 손으로 만든 작은 DB로 확인한다."""

import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend import db
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunStore
from backend.harness.tools import ToolRegistry
from backend.query_tools import query_tools
from shared.clinic import Clinic
from tests.backend.script import Script, answer, call

SOURCE_SCHEMA = Path(__file__).parents[2] / "shared" / "source_schema.sql"
CLINIC = Clinic(today=date(2026, 12, 1), range_start=date(2026, 1, 1), eval_pool_size=150)


@pytest.fixture
def clinic(tmp_path):
    source = tmp_path / "s.sqlite"
    s = sqlite3.connect(source)
    s.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
    s.execute("PRAGMA foreign_keys = OFF")
    s.executescript(
        """
        INSERT INTO emr_staff (id, name, role) VALUES (1, '원장', 'doctor');
        INSERT INTO emr_alert_types VALUES ('A', '항응고제', 'systemic'), ('B', '임신', 'systemic'),
            ('C', '소아', 'procedure');
        INSERT INTO emr_procedures (code, name, category, is_insured, fee_total, patient_share_rate,
            duration_min, is_tooth_level) VALUES
            ('U0001', '스케일링', 'preventive', 'yes', 50000, 0.3, 30, 0),
            ('U0005', '레진', 'restorative', 'no', 100000, 1.0, 30, 1);
        INSERT INTO emr_families VALUES (1, '김'), (2, '정');
        INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id,
            registered_at, recall_cycle_months) VALUES
            (1, 'C-00001', '김철수', '1980-01-01', 'M', '010-1', 1, '2020-01-01', 12),
            (2, 'C-00002', '김영희', '1982-01-01', 'F', '010-2', 1, '2020-01-01', 12),
            (3, 'C-00003', '김철수', '1990-01-01', 'M', '010-3', NULL, '2020-01-01', 12),
            (4, 'C-00004', '이신환', '2000-01-01', 'F', '010-4', NULL, '2026-11-20', 12),
            (5, 'C-00005', '정시작', '2001-01-01', 'F', '010-5', 2, '2026-01-01', 12),
            (6, 'C-00006', '정기한', '2002-01-01', 'M', '010-6', 2, '2020-01-01', 12),
            (7, 'C-00007', '최기록', '2003-01-01', 'M', '010-7', NULL, '2020-01-01', 12);
        INSERT INTO emr_patient_alerts (id, patient_id, code, source, recorded_at, ends_at) VALUES
            (1, 1, 'A', 'intake', '2026-01-01', NULL),
            (2, 1, 'B', 'intake', '2026-01-01', '2026-12-01'),
            (3, 1, 'C', 'intake', '2026-12-02', NULL),
            (4, 7, 'A', 'intake', '2026-12-01', NULL);
        INSERT INTO emr_appointments (id, patient_id, staff_id, scheduled_at, duration_min, type,
            planned_procedure_code, status, rescheduled_from_id, booked_via, created_at) VALUES
            (1, 1, 1, '2026-11-02 10:00:00', 30, 'hygiene', 'U0001', 'completed', NULL, 'phone', 'x'),
            (2, 1, 1, '2026-11-20 10:00:00', 30, 'treatment', 'U0005', 'noshow', NULL, 'phone', 'x'),
            (3, 2, 1, '2025-01-02 10:00:00', 30, 'hygiene', 'U0001', 'completed', NULL, 'phone', 'x'),
            (4, 1, 1, '2026-12-01 09:30:00', 30, 'treatment', 'U0005', 'scheduled', NULL, 'phone', 'x'),
            (5, 1, 1, '2026-12-03 09:30:00', 30, 'treatment', 'U0005', 'scheduled', NULL, 'phone', 'x'),
            (6, 3, 1, '2026-12-01 11:00:00', 30, 'hygiene', 'U0001', 'cancelled', NULL, 'phone', 'x'),
            (7, 3, 1, '2026-12-01 14:00:00', 30, 'hygiene', 'U0001', 'scheduled', 6, 'phone', 'x'),
            (8, 4, 1, '2026-12-01 15:00:00', 30, 'hygiene', 'U0001', 'cancelled', NULL, 'phone', 'x'),
            (9, 6, 1, '2025-12-01 10:00:00', 30, 'hygiene', 'U0001', 'completed', NULL, 'phone', 'x'),
            (10, 6, 1, '2026-06-01 10:00:00', 30, 'treatment', 'U0005', 'completed', NULL, 'phone', 'x'),
            (11, 6, 1, '2026-07-01 10:00:00', 30, 'hygiene', 'U0001', 'noshow', NULL, 'phone', 'x'),
            (12, 7, 1, '2026-11-30 10:00:00', 30, 'treatment', 'U0005', 'scheduled', NULL, 'phone', 'x'),
            (13, 7, 1, '2026-12-02 10:00:00', 30, 'treatment', 'U0005', 'cancelled', NULL, 'phone', 'x'),
            (14, 7, 1, '2026-12-04 10:00:00', 30, 'hygiene', 'U0001', 'scheduled', NULL, 'phone', 'x'),
            (15, 7, 1, '2026-12-01 16:00:00', 30, 'hygiene', 'U0001', 'scheduled', NULL, 'phone', 'x');
        INSERT INTO emr_payments (id, patient_id, kind, amount_due, amount_paid, paid_at, method) VALUES
            (1, 1, 'visit', 30000, 10000, '2026-11-02 10:30:00', 'card'),
            (2, 3, 'visit', 5000, 0, '2026-12-02 10:00:00', 'card'),
            (3, 7, 'visit', 7000, 0, '2026-12-01 09:00:00', 'card');
        INSERT INTO emr_treatment_plans VALUES (1, 1, 'x', 2);
        INSERT INTO emr_treatment_plan_items VALUES
            (1, 1, 1, 'U0005', '16', 100000, 'open'),
            (2, 1, 2, 'U0005', '17', 100000, 'done');
        INSERT INTO emr_claims (id, treatment_id, patient_id, chart_no, tooth_no, claim_month,
            service_date, procedure_code, claim_amount, status, paid_amount, adjust_code,
            adjust_note, reviewed_at) VALUES
            (1, 1, 1, 'C-00001', '00', '2026-11', '2026-11-02', 'U0001', 35000, 'adjusted', 20000,
             'B', '사유', '2026-11-25'),
            (2, 2, 2, 'C-00002', '00', '2025-01', '2025-01-02', 'U0001', 35000, 'paid', 35000,
             NULL, NULL, '2025-02-01'),
            (3, 3, 3, 'C-00003', '00', '2026-11', '2026-11-03', 'U0001', 35000, 'pending', NULL,
             NULL, NULL, NULL),
            (4, 4, 4, 'C-00004', '00', '2026-10', '2026-10-05', 'U0001', 35000, 'denied', 0,
             'C', '연 1회 초과', '2026-10-25');
        UPDATE emr_procedures SET aliases = '["치석제거"]' WHERE code = 'U0001';
        INSERT INTO emr_procedures (code, name, category, is_insured, fee_total, patient_share_rate,
            duration_min, is_tooth_level, aliases) VALUES
            ('U0010', '치주치료', 'perio', 'yes', 80000, 0.3, 30, 0, '["잇몸치료"]'),
            ('U0021', '임플란트1차', 'implant', 'no', 1000000, 1.0, 60, 1, '["임플란트"]'),
            ('U0022', '임플란트2차', 'implant', 'no', 300000, 1.0, 30, 1, '["임플란트"]'),
            ('U0031', '단순발치', 'surgery', 'yes', 30000, 0.3, 30, 1, '["단순 발치"]'),
            ('U0032', '사랑니 발치', 'surgery', 'yes', 60000, 0.3, 30, 1, '["사랑니"]');
        INSERT INTO emr_treatments (id, appointment_id, patient_id, procedure_code, tooth_no,
            performed_at, fee_total, insured_amount, patient_amount) VALUES
            (101, 1, 1, 'U0001', '00', '2026-11-02 10:00:00', 50000, 35000, 15000),
            (102, 1, 1, 'U0010', '00', '2026-11-05 10:00:00', 80000, 56000, 24000),
            (103, 9, 6, 'U0001', '00', '2026-06-01 10:00:00', 50000, 35000, 15000),
            (104, 10, 6, 'U0010', '00', '2026-06-20 10:00:00', 80000, 56000, 24000),
            (105, 12, 7, 'U0010', '00', '2026-12-05 10:00:00', 80000, 56000, 24000);
        INSERT INTO emr_chart_notes (id, treatment_id, dx_code, note) VALUES
            (1, 101, 'K03', '전악 치석제거'),
            (2, 102, 'K05', '스케일링 3일 뒤 재평가, 치주치료 연결'),
            (3, 103, 'K03', '전악 치석제거'),
            (4, 104, 'K05', '치주치료'),
            (5, 105, 'K05', '치주치료 예정');
        """
    )
    s.commit()
    s.close()
    conn = db.connect(tmp_path / "a.sqlite", source)
    db.init_schema(conn)
    yield conn
    conn.close()


class Ctx:
    def __init__(self, conn):
        self.store = RunStore(conn)


def run(conn, tool_name, **args):
    tool = {t.name: t for t in query_tools(CLINIC)}[tool_name]
    parsed = tool.args.model_validate(args).model_dump(mode="json")
    return tool.fn(Ctx(conn), parsed)


def test_schedule_counts_all_rows_and_separates_rescheduled(clinic):
    r = run(clinic, "query_schedule", date_from="2026-12-01", date_to="2026-12-01")
    assert r["count"] == 5
    assert r["by_status"] == {"scheduled": 3, "cancelled": 1, "rescheduled": 1}
    assert r["by_type"] == {"treatment": 1, "hygiene": 4}


def test_schedule_summary_comes_first_so_truncation_keeps_it(clinic):
    r = run(clinic, "query_schedule", date_from="2026-12-01", date_to="2026-12-01")
    assert list(r)[:3] == ["count", "by_status", "by_type"]


def test_schedule_rows_carry_patient_and_procedure(clinic):
    r = run(
        clinic, "query_schedule", date_from="2026-12-03", date_to="2026-12-03", type="treatment"
    )
    [row] = r["appointments"]
    assert row["chart_no"] == "C-00001" and row["patient"] == "김철수"
    assert row["procedure"] == "레진" and row["scheduled_at"] == "2026-12-03 09:30:00"


def test_schedule_status_and_type_filters(clinic):
    r = run(
        clinic,
        "query_schedule",
        date_from="2026-11-01",
        date_to="2026-11-30",
        status="noshow",
        type="treatment",
    )
    assert r["count"] == 1


def test_unpaid_balance_is_before_that_day_not_today(clinic):
    """환자 3의 미수는 12/2 — 12/1 예약에는 아직 잔액이 없다"""
    r = run(
        clinic,
        "query_schedule",
        date_from="2026-12-01",
        date_to="2026-12-01",
        has_unpaid_balance=True,
    )
    assert {a["chart_no"] for a in r["appointments"]} == {"C-00001"}


def test_is_new_patient_uses_range_start(clinic):
    r = run(
        clinic, "query_schedule", date_from="2026-12-01", date_to="2026-12-01", is_new_patient=True
    )
    assert {a["chart_no"] for a in r["appointments"]} == {"C-00004"}


def test_false_filters_mean_the_opposite(clinic):
    r = run(
        clinic,
        "query_schedule",
        date_from="2026-12-01",
        date_to="2026-12-01",
        is_new_patient=False,
    )
    assert "C-00004" not in {a["chart_no"] for a in r["appointments"]}


def test_patient_needs_chart_or_name():
    tool = {t.name: t for t in query_tools(CLINIC)}["query_patient"]
    with pytest.raises(ValidationError):
        tool.args.model_validate({})


def test_same_name_returns_candidates_without_summary(clinic):
    r = run(clinic, "query_patient", name="김철수")
    assert [m["chart_no"] for m in r["matches"]] == ["C-00001", "C-00003"]
    assert r.get("summary") is None


def test_no_match(clinic):
    assert run(clinic, "query_patient", name="없는사람") == {"matches": []}


def test_summary_fields(clinic):
    s = run(clinic, "query_patient", chart_no="C-00001")["summary"]
    assert s["balance"] == 20000
    assert s["last_visit"] == "2026-11-02"  # 노쇼는 내원이 아니다
    assert s["next_appointment"]["scheduled_at"] == "2026-12-01 09:30:00"
    assert [a["label"] for a in s["alerts"]] == ["항응고제"]  # 끝난 날·미래 기록일 제외
    assert s["scaling_claimed_this_year"] is True
    assert [i["tooth_no"] for i in s["open_plan_items"]] == ["16"]
    assert s["family_recall"] == [
        {"chart_no": "C-00002", "name": "김영희", "last_hygiene": "2025-01-02", "overdue": True}
    ]


def test_scaling_claim_of_other_year_does_not_count(clinic):
    assert (
        run(clinic, "query_patient", chart_no="C-00002")["summary"]["scaling_claimed_this_year"]
        is False
    )


def test_pending_scaling_claim_counts_as_used(clinic):
    s = run(clinic, "query_patient", chart_no="C-00003")["summary"]
    assert s["scaling_claimed_this_year"] is True


def test_family_member_seen_recently_is_not_overdue(clinic):
    s = run(clinic, "query_patient", chart_no="C-00002")["summary"]
    assert s["family_recall"] == [
        {"chart_no": "C-00001", "name": "김철수", "last_hygiene": "2026-11-02", "overdue": False}
    ]


def test_balance_ignores_payments_from_today_on(clinic):
    assert run(clinic, "query_patient", chart_no="C-00003")["summary"]["balance"] == 0


def test_claims_summary_and_result_fields(clinic):
    r = run(clinic, "query_claims", date_from="2026-11-01", date_to="2026-11-30")
    assert r["count"] == 2 and r["total_amount"] == 70000
    assert r["by_status"] == {"adjusted": 1, "pending": 1}
    assert list(r)[:3] == ["count", "total_amount", "by_status"]
    adjusted = next(c for c in r["claims"] if c["status"] == "adjusted")
    assert adjusted["adjust_note"] == "사유" and adjusted["paid_amount"] == 20000
    pending = next(c for c in r["claims"] if c["status"] == "pending")
    assert "adjust_note" not in pending


def test_claims_filters(clinic):
    assert run(clinic, "query_claims", chart_no="C-00002")["count"] == 1
    assert run(clinic, "query_claims", procedure_code="U0005")["count"] == 0
    assert run(clinic, "query_claims", status="paid")["count"] == 1


def test_bad_enum_is_refused():
    tool = {t.name: t for t in query_tools(CLINIC)}["query_schedule"]
    with pytest.raises(ValidationError):
        tool.args.model_validate(
            {"date_from": "2026-12-01", "date_to": "2026-12-01", "status": "없음"}
        )


def test_bad_date_is_refused():
    tool = {t.name: t for t in query_tools(CLINIC)}["query_schedule"]
    with pytest.raises(ValidationError):
        tool.args.model_validate({"date_from": "내일", "date_to": "2026-12-01"})


def test_descriptions_show_today():
    assert all(
        "2026-12-01" in t.description
        for t in query_tools(CLINIC)
        if t.name in ("query_schedule", "query_claims")
    )


def test_refuse_ends_run_without_another_model_call(clinic):
    r = ToolRegistry()
    for t in query_tools(CLINIC):
        r.register(t)
    spec = AgentSpec("챗", "chat", "t", "v1", lambda v: "s", tuple(r.tools), 6, 1.0)
    gw = Script(call(("refuse", {"reason": "restricted"})), answer("부르면 안 됨"))
    out = Runner(spec, gw, RunStore(clinic), r, owner="t").start("q", "원장님 주소")
    assert out.status == "done" and out.reason == "refuse:restricted" and len(gw.calls) == 1


def test_refuse_with_other_call_in_same_turn_stops_at_refuse(clinic):
    r = ToolRegistry()
    for t in query_tools(CLINIC):
        r.register(t)
    spec = AgentSpec("챗", "chat", "t", "v1", lambda v: "s", tuple(r.tools), 6, 1.0)
    turn = call(
        ("a", "refuse", {"reason": "not_found"}), ("b", "query_patient", {"chart_no": "C-00001"})
    )
    out = Runner(spec, Script(turn), RunStore(clinic), r, owner="t").start("q", "질문")
    assert out.reason == "refuse:not_found"
    assert [s["tool"] for s in RunStore(clinic).steps(out.run_id) if s["kind"] == "tool"] == [
        "refuse"
    ]


def test_tools_do_not_write(clinic):
    before = clinic.total_changes
    run(clinic, "query_patient", chart_no="C-00001")
    run(clinic, "query_claims")
    run(clinic, "query_schedule", date_from="2026-12-01", date_to="2026-12-01")
    assert clinic.total_changes == before


def test_results_are_json(clinic):
    json.dumps(run(clinic, "query_patient", chart_no="C-00001"))


def test_death_after_refuse_finishes_on_resume(clinic, monkeypatch):
    r = ToolRegistry()
    for t in query_tools(CLINIC):
        r.register(t)
    spec = AgentSpec("챗", "chat", "t", "v1", lambda v: "s", tuple(r.tools), 6, 1.0)

    class Crash(BaseException):
        pass

    def die(*a, **k):
        raise Crash

    monkeypatch.setattr(RunStore, "finish", die)
    with pytest.raises(Crash):
        Runner(
            spec,
            Script(call(("refuse", {"reason": "restricted"}))),
            RunStore(clinic),
            r,
            owner="t",
        ).start("q", "질문")
    monkeypatch.undo()
    clinic.rollback()
    clinic.execute("UPDATE ax_agent_runs SET locked_by = NULL")
    clinic.commit()
    gw = Script(answer("부르면 안 됨"))
    run_id = clinic.execute("SELECT id FROM ax_agent_runs").fetchone()[0]
    out = Runner(spec, gw, RunStore(clinic), r, owner="t").resume(run_id)
    assert out.reason == "refuse:restricted" and gw.calls == []


def test_load_clinic_reads_config():
    from shared.clinic import load_clinic

    c = load_clinic()
    assert c.today == date(2026, 12, 1) and c.range_start == date(2024, 12, 1)


def test_same_day_payment_is_not_opening_balance(clinic):
    assert run(clinic, "query_patient", chart_no="C-00007")["summary"]["balance"] == 0
    r = run(
        clinic,
        "query_schedule",
        date_from="2026-12-01",
        date_to="2026-12-01",
        has_unpaid_balance=True,
    )
    assert "C-00007" not in {a["chart_no"] for a in r["appointments"]}


def test_no_unpaid_filter_keeps_zero_balance(clinic):
    r = run(
        clinic,
        "query_schedule",
        date_from="2026-12-01",
        date_to="2026-12-01",
        has_unpaid_balance=False,
    )
    charts = {a["chart_no"] for a in r["appointments"]}
    assert "C-00007" in charts and "C-00001" not in charts


def test_registered_on_range_start_is_new(clinic):
    tool = {t.name: t for t in query_tools(replace(CLINIC, range_start=date(2026, 11, 20)))}[
        "query_schedule"
    ]
    args = tool.args.model_validate(
        {"date_from": "2026-12-01", "date_to": "2026-12-01", "is_new_patient": True}
    ).model_dump(mode="json")
    assert {a["chart_no"] for a in tool.fn(Ctx(clinic), args)["appointments"]} == {"C-00004"}


def test_alert_recorded_today_is_visible(clinic):
    s = run(clinic, "query_patient", chart_no="C-00007")["summary"]
    assert [a["label"] for a in s["alerts"]] == ["항응고제"]


def test_recall_due_today_is_not_overdue_and_counts_only_completed_hygiene(clinic):
    """정기한: 마지막 완료 위생 2025-12-01 + 365일 = 기준일. 치료 완료·위생 노쇼는 세지 않는다"""
    s = run(clinic, "query_patient", chart_no="C-00005")["summary"]
    assert s["family_recall"] == [
        {"chart_no": "C-00006", "name": "정기한", "last_hygiene": "2025-12-01", "overdue": False}
    ]


def test_next_appointment_skips_past_and_cancelled(clinic):
    s = run(clinic, "query_patient", chart_no="C-00007")["summary"]
    assert s["next_appointment"]["scheduled_at"] == "2026-12-01 16:00:00"


def test_denied_claim_shows_result_and_counts_as_used(clinic):
    r = run(clinic, "query_claims", status="denied")
    assert r["claims"][0]["adjust_note"] == "연 1회 초과"
    s = run(clinic, "query_patient", chart_no="C-00004")["summary"]
    assert s["scaling_claimed_this_year"] is True


def test_blocked_refuse_does_not_end_run(clinic):
    r = ToolRegistry()
    for t in query_tools(CLINIC):
        r.register(t)
    spec = AgentSpec("챗", "chat", "t", "v1", lambda v: "s", tuple(r.tools), 6, 1.0)
    gw = Script(call(("refuse", {"reason": "없는값"})), answer("이어서"))
    out = Runner(spec, gw, RunStore(clinic), r, owner="t").start("q", "질문")
    assert out.result == "이어서" and len(gw.calls) == 2


# ── query_charts (설계 2026-11-09 챗 "query_charts") ─────────────────────────


def test_charts_gap_condition_finds_short_interval(clinic):
    out = run(
        clinic, "query_charts", procedure="잇몸치료", prev_procedure="스케일링", max_gap_days=4
    )
    assert [r["id"] for r in out["results"]] == ["chart:102"]  # 3일 뒤만, 19일 뒤는 빠짐
    r = out["results"][0]
    assert r["prev"] == {
        "procedure": "스케일링",
        "performed_at": "2026-11-02 10:00:00",
        "gap_days": 3,
    }
    assert r["chart_no"] == "C-00001" and r["note"].startswith("스케일링 3일 뒤")
    assert "patient" not in r and "name" not in r  # 차트 서술은 비식별


def test_charts_min_gap(clinic):
    out = run(
        clinic, "query_charts", procedure="치주치료", prev_procedure="치석제거", min_gap_days=10
    )
    assert [r["id"] for r in out["results"]] == ["chart:104"]


def test_charts_ambiguous_alias_returns_candidates(clinic):
    out = run(clinic, "query_charts", procedure="임플란트")
    assert out["results"] == [] and set(out["candidates"]) == {"임플란트1차", "임플란트2차"}


def test_charts_unknown_procedure_is_empty(clinic):
    assert run(clinic, "query_charts", procedure="교정")["count"] == 0


def test_charts_future_treatments_hidden(clinic):
    ids = [r["id"] for r in run(clinic, "query_charts", procedure="치주치료")["results"]]
    assert ids == ["chart:102", "chart:104"]  # 2026-12-05는 오늘(12-01) 뒤


def test_charts_category_word_matches_all_procedures_containing_it(clinic):
    """'발치'처럼 이름 일부면 그 말이 든 시술 전부(단순발치·사랑니 발치)를 앞 시술로 — 0건으로 헛돌지 않게"""
    out = run(clinic, "query_charts", procedure="발치")
    assert out["matched"] == ["단순발치", "사랑니 발치"]


def test_charts_unknown_name_is_reported(clinic):
    out = run(clinic, "query_charts", procedure="치주치료", prev_procedure="교정")
    assert out["count"] == 0 and out["unknown"] == ["교정"]
