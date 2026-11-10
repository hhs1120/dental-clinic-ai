"""⑤ 청구 점검 1단계(backend/claims.py) — 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §1.
손으로 만든 source(⑨ 도우미 Source + 청구·차트 기록)로 재심 신청 기한·칩·규칙 제안·청구 전 상병 경고·판단 기록을 본다."""

from datetime import date, timedelta

import pytest

from backend import claims
from generator.doc_catalog import CLAIM_HEADINGS
from generator.master import ADJUST_REASONS, APPEAL_DAYS, DIAGNOSES, SCALING_CODE
from tests.backend.test_missed import Source

DAY = date(2026, 12, 1)
NOTE = {}  # 유형 → (사유 키, 문구) — 유형마다 첫 사유
for _k, _r in ADJUST_REASONS.items():
    NOTE.setdefault(_r["type"], (_k, _r["note"]))
AT = "2026-11-09 10:00:00"


def add_claim(
    src: Source,
    pid: int,
    *,
    day: str = "2026-08-01",
    code: str = SCALING_CODE,
    status: str = "adjusted",
    note: str | None = None,
    reviewed: str | None = None,
    amount: int = 10000,
    paid: int | None = None,
    dx: str = "D06",
    tooth: str = "00",
) -> int:
    """청구 한 줄(진료·차트 기록 포함) — 환자가 없으면 만든다. paid 기본: 불능 0, 조정 절반, 지급 전액"""
    if not src.conn.execute("SELECT 1 FROM emr_patients WHERE id = ?", (pid,)).fetchone():
        src.patient(pid)
    if not src.conn.execute("SELECT 1 FROM emr_diagnoses").fetchone():
        src.conn.executemany("INSERT INTO emr_diagnoses VALUES (?, ?)", sorted(DIAGNOSES.items()))
    a = src.appt(pid, day)
    t = src.treatment(pid, a, day, code, tooth)
    src.conn.execute(
        "INSERT INTO emr_chart_notes (treatment_id, dx_code, note) VALUES (?, ?, '')", (t, dx)
    )
    if paid is None:
        paid = {"denied": 0, "adjusted": amount // 2, "paid": amount}.get(status)
    return src.conn.execute(
        "INSERT INTO emr_claims (treatment_id, patient_id, chart_no, tooth_no, claim_month,"
        " service_date, procedure_code, claim_amount, status, paid_amount, adjust_code,"
        " adjust_note, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            t,
            pid,
            f"C-{pid:05d}",
            tooth,
            day[:7],
            day,
            code,
            amount,
            status,
            paid,
            "C" if note else None,
            note,
            reviewed,
        ),
    ).lastrowid


def reviewed_for(days_left: int) -> str:
    """남은 날이 days_left가 되는 통보일"""
    return (DAY + timedelta(days=days_left) - timedelta(days=APPEAL_DAYS)).isoformat()


@pytest.fixture
def src(tmp_path):
    return Source(tmp_path / "source.sqlite")


def rows(conn) -> dict[int, dict]:
    return {r["claim_id"]: r for r in claims.adjusted(conn, DAY)["rows"]}


# ── 재심 신청 기한 ───────────────────────────────────────────────


def test_deadline_and_days_left(src):
    cid = add_claim(src, 1, note=NOTE["documentation"][1], reviewed="2026-09-09")
    r = rows(src.app())[cid]
    assert r["deadline"] == "2026-12-08"
    assert r["days_left"] == 7
    assert r["expired"] is False


def test_deadline_boundary(src):
    note = NOTE["documentation"][1]
    last = add_claim(src, 1, note=note, reviewed=(DAY - timedelta(days=90)).isoformat())
    gone = add_claim(src, 2, note=note, reviewed=(DAY - timedelta(days=91)).isoformat())
    got = rows(src.app())
    assert got[last]["days_left"] == 0
    assert "soon" in got[last]["chips"]
    assert got[last]["expired"] is False
    assert got[gone]["days_left"] == -1
    assert got[gone]["chips"] == ["expired"]
    assert got[gone]["expired"] is True


def test_deadline_from_master_values(src):
    cid = add_claim(src, 1, note=NOTE["documentation"][1], reviewed="2026-11-20")
    src.conn.execute("UPDATE ax_master_values SET value = '30' WHERE name = 'appeal_days'")
    conn = src.app()
    out = claims.adjusted(conn, DAY)
    assert out["appeal_days"] == 30
    assert out["rows"][0]["claim_id"] == cid
    assert out["rows"][0]["deadline"] == "2026-12-20"


# ── 칩 ────────────────────────────────────────────────────────


def test_chip_membership_and_sums(src):
    dx = add_claim(
        src, 1, note=NOTE["diagnosis"][1], reviewed=reviewed_for(10), amount=20000, paid=5000
    )
    doc = add_claim(
        src, 2, status="denied", note=NOTE["documentation"][1], reviewed=reviewed_for(10)
    )
    req = add_claim(
        src, 3, note=NOTE["requirement"][1], reviewed=reviewed_for(45), amount=30000, paid=9000
    )
    old = add_claim(src, 4, note=NOTE["documentation"][1], reviewed=reviewed_for(-5))
    out = claims.adjusted(src.app(), DAY)
    by = {r["claim_id"]: r for r in out["rows"]}
    members = {c: {r["claim_id"] for r in out["rows"] if c in r["chips"]} for c in claims.CHIPS}
    assert members["diagnosis"] == {dx}
    assert members["soon"] == {doc}
    assert members["all"] == {dx, doc, req}
    assert members["expired"] == {old}
    assert members["denied"] == {doc}
    assert members["adjusted"] == {dx, req}
    for chip in claims.CHIPS:
        assert out["chips"][chip] == {
            "count": len(members[chip]),
            "amount": sum(by[c]["unpaid"] for c in members[chip]),
        }
    assert out["chips"]["all"]["amount"] == 15000 + 10000 + 21000


def test_unpaid_full_denial(src):
    cid = add_claim(
        src, 1, status="denied", note=NOTE["documentation"][1], reviewed=reviewed_for(20)
    )
    r = rows(src.app())[cid]
    assert r["unpaid"] == r["claim_amount"] == 10000


def test_only_adjusted_and_denied(src):
    add_claim(src, 1, status="paid")
    add_claim(src, 2, status="pending")
    assert claims.adjusted(src.app(), DAY)["rows"] == []


# ── 규칙 제안 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("type_", "suggestion"),
    [
        ("diagnosis", "justified"),
        ("external", "justified"),
        ("documentation", "resubmit"),
        ("requirement", None),
    ],
)
def test_suggestion_by_type(src, type_, suggestion):
    key, note = NOTE[type_]
    cid = add_claim(src, 1, note=note, reviewed=reviewed_for(20))
    r = rows(src.app())[cid]
    assert r["type"] == type_
    assert r["suggestion"] == suggestion
    assert r["reason"] == claims.SUGGEST_REASON[type_]
    assert r["fill_item"] == (CLAIM_HEADINGS[key] if type_ == "documentation" else None)


def test_sort_by_deadline(src):
    note = NOTE["documentation"][1]
    late = add_claim(src, 1, note=note, reviewed=reviewed_for(50))
    early = add_claim(src, 2, note=note, reviewed=reviewed_for(5))
    old = add_claim(src, 3, note=note, reviewed=reviewed_for(-100))
    recent = add_claim(src, 4, note=note, reviewed=reviewed_for(-2))
    got = [r["claim_id"] for r in claims.adjusted(src.app(), DAY)["rows"]]
    assert got == [early, late, recent, old]


# ── 판단 기록 ──────────────────────────────────────────────────


def test_record_and_overwrite(src):
    cid = add_claim(src, 1, note=NOTE["requirement"][1], reviewed=reviewed_for(20))
    conn = src.app()
    claims.record_judgment(conn, cid, "contestable", "데스크", AT, DAY)
    assert rows(conn)[cid]["judgment"] == {"value": "contestable", "by": "데스크", "at": AT}
    claims.record_judgment(conn, cid, "justified", "상담", "2026-11-10 09:00:00", DAY)
    assert rows(conn)[cid]["judgment"]["value"] == "justified"
    assert conn.execute("SELECT COUNT(*) FROM ax_claim_judgments").fetchone()[0] == 1


def test_diagnosis_only_justified(src):
    cid = add_claim(src, 1, note=NOTE["diagnosis"][1], reviewed=reviewed_for(20))
    conn = src.app()
    for bad in ("contestable", "resubmit"):
        with pytest.raises(ValueError):
            claims.record_judgment(conn, cid, bad, "데스크", AT, DAY)
    claims.record_judgment(conn, cid, "justified", "데스크", AT, DAY)
    assert rows(conn)[cid]["judgment"]["value"] == "justified"


def test_expired_rejects_judgment(src):
    cid = add_claim(src, 1, note=NOTE["requirement"][1], reviewed=reviewed_for(-1))
    conn = src.app()
    with pytest.raises(ValueError):
        claims.record_judgment(conn, cid, "contestable", "데스크", AT, DAY)
    conn.execute(
        "INSERT INTO ax_claim_judgments VALUES (?, 'contestable', '데스크', ?)", (cid, AT)
    )
    assert rows(conn)[cid]["judgment"] == {"value": "contestable", "by": "데스크", "at": AT}


def test_record_rejects_unknown(src):
    ok = add_claim(src, 1, note=NOTE["requirement"][1], reviewed=reviewed_for(20))
    paid = add_claim(src, 2, status="paid")
    pending = add_claim(src, 3, status="pending")
    conn = src.app()
    for cid, value in (
        (ok, "maybe"),
        (paid, "justified"),
        (pending, "justified"),
        (999, "justified"),
    ):
        with pytest.raises(ValueError):
            claims.record_judgment(conn, cid, value, "데스크", AT, DAY)
    assert conn.execute("SELECT COUNT(*) FROM ax_claim_judgments").fetchone()[0] == 0


# ── 청구 전 점검 ───────────────────────────────────────────────


def test_pre_claim_warning(src):
    ok = add_claim(src, 1, day="2026-11-10", status="pending", dx="D06")
    bad = add_claim(src, 2, day="2026-11-12", status="pending", dx="D07")
    out = claims.pre_claim(src.app(), DAY)
    assert out["counts"] == {"lines": 2, "warning": 1}
    assert [r["claim_id"] for r in out["rows"]] == [bad, ok]
    first = out["rows"][0]
    assert first["dx_code"] == "D07"
    assert first["dx_name"] == DIAGNOSES["D07"]
    assert first["warning"]["allowed"] == ["만성 치은염", "치아 침착물(치석)"]
    assert out["rows"][1]["warning"] is None


def test_pre_claim_past_history(src):
    dx_note = NOTE["diagnosis"][1]
    add_claim(src, 1, day="2026-11-12", status="pending", dx="D07")
    for pid, amount in ((2, 10000), (3, 12000)):
        add_claim(
            src, pid, status="denied", note=dx_note, reviewed="2026-05-01", dx="D07", amount=amount
        )
    add_claim(src, 4, status="denied", note=dx_note, reviewed="2026-05-01", dx="D02")  # 다른 조합
    add_claim(
        src, 5, status="denied", note=NOTE["documentation"][1], reviewed="2026-05-01", dx="D07"
    )  # 서류 사유
    add_claim(src, 6, status="denied", note=dx_note, reviewed=DAY.isoformat(), dx="D07")  # 기준일
    out = claims.pre_claim(src.app(), DAY)
    w = out["rows"][0]["warning"]
    assert (w["past_count"], w["past_amount"]) == (2, 22000)


def _fill(s: Source, n: int) -> None:
    for i in range(1, n + 1):
        add_claim(s, i, note=NOTE["documentation"][1], reviewed=reviewed_for(i % 60 - 5))
        add_claim(s, i, day="2026-11-10", status="pending", dx="D07" if i % 3 else "D06")
        add_claim(
            s,
            i,
            day="2026-03-01",
            status="denied",
            note=NOTE["diagnosis"][1],
            reviewed="2026-05-01",
            dx="D07",
        )


def _count(conn, fn) -> int:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    fn(conn, DAY)
    conn.set_trace_callback(None)
    return len(seen)


def test_query_count_independent_of_claims(tmp_path):
    counts = []
    for n in (10, 200):
        (tmp_path / str(n)).mkdir()
        s = Source(tmp_path / str(n) / "source.sqlite")
        _fill(s, n)
        conn = s.app()
        claims.record_judgment(
            conn,
            next(r["claim_id"] for r in claims.adjusted(conn, DAY)["rows"] if not r["expired"]),
            "resubmit",
            "데스크",
            AT,
            DAY,
        )
        assert len(claims.pre_claim(conn, DAY)["rows"]) == n
        counts.append((_count(conn, claims.adjusted), _count(conn, claims.pre_claim)))
        conn.close()
    assert counts[0] == counts[1]
