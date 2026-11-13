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


def test_future_notice_hidden(src):
    """기준일을 앞당겨 보면 그 뒤에 온 심사 결과는 아직 없다 — 목록·판단 기록 모두. 기준일 당일 통보는 보인다"""
    note = NOTE["requirement"][1]
    today = add_claim(src, 1, note=note, reviewed=DAY.isoformat())
    later = add_claim(src, 2, note=note, reviewed=(DAY + timedelta(days=1)).isoformat())
    conn = src.app()
    assert set(rows(conn)) == {today}
    with pytest.raises(ValueError):
        claims.record_judgment(conn, later, "contestable", "데스크", AT, DAY)


def test_tag_bounds_in_sheet(src):
    """꼬리표 경계는 서버가 내려준다 — 화면이 숫자를 따로 적지 않게"""
    out = claims.adjusted(src.app(), DAY)
    assert (out["soon_days"], out["watch_days"]) == (claims.SOON_DAYS, claims.WATCH_DAYS)


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
    assert out["counts"] == {"lines": 2, "warning": 1, "ai": 0, "ai_missing": 0, "ai_checked": 0}
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


# ── ⑤-2 AI 점검 결과 (설계 §2 "앱 화면") ─────────────────────────────

EXTRACT = "U0012"  # 사랑니 발치 — 요건 사유 R05
CHART_Q = {"source": "chart", "quote": "골삭제 후 분할 발치함"}
STD_Q = {"source": "standard", "quote": "매복 정도와 골삭제 여부를 적는다"}


def save_check(conn, cid: int, requirement: str | None, evidence=(), omissions=()) -> None:
    """ax_claim_checks 한 줄 — requirement None이면 후검증 실패(result NULL)"""
    import json

    result = (
        None
        if requirement is None
        else json.dumps(
            {"requirement": requirement, "evidence": list(evidence), "omissions": list(omissions)}
        )
    )
    with conn:
        conn.execute(
            "INSERT INTO ax_claim_checks VALUES (?, '{}', 'h', ?, ?, 'v1', 'm', ?)",
            (cid, result, int(result is not None), "2026-11-11T09:30:00"),
        )


def test_pre_claim_ai_counts(src):
    miss = add_claim(src, 1, code=EXTRACT, status="pending", day="2026-11-12")
    met = add_claim(src, 2, code=EXTRACT, status="pending", day="2026-11-12")
    add_claim(src, 3, code=EXTRACT, status="pending", day="2026-11-12")
    conn = src.app()
    save_check(conn, miss, "missing", [STD_Q])
    save_check(conn, met, "met", [CHART_Q])
    out = claims.pre_claim(conn, DAY)
    assert (out["counts"]["ai"], out["counts"]["ai_missing"], out["counts"]["ai_checked"]) == (
        1,
        1,
        2,
    )
    assert out["ai_at"] == "2026-11-11T09:30:00"
    got = {r["claim_id"]: r["ai"] for r in out["rows"]}
    assert got[miss]["requirement"] == "missing" and got[miss]["evidence"] == [STD_Q]
    assert got[met]["requirement"] == "met"
    assert len([v for v in got.values() if v is None]) == 1


def save_omission(conn, cid: int, omissions=(), *, age: int = 40, tooth=None, at=AT) -> None:
    """ax_claim_omissions 한 줄(2b AI 원답) — omissions None이면 후검증 실패"""
    import json

    result = None if omissions is None else json.dumps({"omissions": list(omissions)})
    facts = json.dumps({"procedure": "x", "tooth": tooth, "age": age, "note": "n"})
    with conn:
        conn.execute(
            "INSERT INTO ax_claim_omissions VALUES (?, ?, 'h', ?, ?, 'v1', 'm', ?)",
            (cid, facts, result, int(result is not None), at),
        )


AMALGAM = {"procedure": "아말감/GI", "tooth": "12", "quote": "12번 아말감/GI 함께 시행"}


def test_pre_claim_uses_2b_omissions(src):
    """2a 답의 누락은 쓰지 않고 2b 답만(설계 §2b) — 2b 결과가 없으면 누락 없음"""
    cid = add_claim(src, 1, code=EXTRACT, status="pending", day="2026-11-12")
    conn = src.app()
    save_check(conn, cid, "met", [CHART_Q], [{"procedure": "봉합", "quote": "분할 발치함"}])
    out = claims.pre_claim(conn, DAY)
    assert out["counts"]["ai"] == 0 and out["rows"][0]["ai"]["omissions"] == []
    save_omission(conn, cid, [AMALGAM])
    out = claims.pre_claim(conn, DAY)
    assert out["counts"]["ai"] == 1
    (o,) = out["rows"][0]["ai"]["omissions"]
    assert (o["procedure_code"], o["procedure"], o["tooth"]) == ("U0006", "아말감/GI", "12")


def test_pre_claim_2b_only_line(src):
    """요건 대상이 아닌 줄(스케일링)에 2b만 — requirement None, 점검한 줄로 센다"""
    cid = add_claim(src, 1, status="pending", day="2026-11-12")
    conn = src.app()
    save_omission(conn, cid, [AMALGAM], at="2026-11-11T10:00:00")
    out = claims.pre_claim(conn, DAY)
    ai = out["rows"][0]["ai"]
    assert ai["requirement"] is None and ai["evidence"] == [] and len(ai["omissions"]) == 1
    assert (out["counts"]["ai"], out["counts"]["ai_checked"]) == (1, 1)
    assert out["ai_at"] == "2026-11-11T10:00:00"


def test_pre_claim_failed_2b_not_shown(src):
    cid = add_claim(src, 1, status="pending", day="2026-11-12")
    conn = src.app()
    save_omission(conn, cid, None)
    assert claims.pre_claim(conn, DAY)["rows"][0]["ai"] is None


def test_pending_union(src):
    """2a·2b 둘 다 다시 부를 대기 줄은 갱신 버튼에서 한 줄, 2b만 부를 줄(스케일링)도 센다"""
    add_claim(src, 1, code=EXTRACT, status="pending", day="2026-11-12")
    add_claim(src, 2, status="pending", day="2026-11-12")
    src.conn.execute("UPDATE emr_chart_notes SET note = '38번 매복, 골삭제 후 분할 발치함'")
    conn = src.app()
    assert claims.pre_claim(conn, DAY)["ai_pending"] == {"pre": 2, "adjust": 0}


def test_pre_claim_without_checks(src):
    add_claim(src, 1, code=EXTRACT, status="pending", day="2026-11-12")
    out = claims.pre_claim(src.app(), DAY)
    assert (out["counts"]["ai"], out["counts"]["ai_checked"], out["ai_at"]) == (0, 0, None)
    assert all(r["ai"] is None for r in out["rows"])
    assert out["ai_pending"] == {"pre": 0, "adjust": 0}  # 차트 서술이 빈 줄은 부를 것이 아님


def test_adjusted_requirement_ai_suggestion(src):
    note = ADJUST_REASONS["R05"]["note"]
    met = add_claim(src, 1, code=EXTRACT, note=note, reviewed=reviewed_for(10))
    miss = add_claim(src, 2, code=EXTRACT, note=note, reviewed=reviewed_for(10))
    doc = add_claim(src, 3, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    conn = src.app()
    save_check(conn, met, "met", [CHART_Q])
    save_check(conn, miss, "missing", [STD_Q])
    got = rows(conn)
    assert (got[met]["suggestion"], got[met]["suggest_by"]) == ("contestable", "ai")
    assert got[met]["ai_evidence"] == [CHART_Q]
    assert (got[miss]["suggestion"], got[miss]["suggest_by"]) == ("justified", "ai")
    assert (got[doc]["suggestion"], got[doc]["suggest_by"]) == ("resubmit", "rule")
    assert got[doc]["ai_evidence"] is None


def test_adjusted_invalid_or_na_check_keeps_none(src):
    note = ADJUST_REASONS["R05"]["note"]
    bad = add_claim(src, 1, code=EXTRACT, note=note, reviewed=reviewed_for(10))
    na = add_claim(src, 2, code=EXTRACT, note=note, reviewed=reviewed_for(10))
    none = add_claim(src, 3, code=EXTRACT, note=note, reviewed=reviewed_for(10))
    conn = src.app()
    save_check(conn, bad, None)
    save_check(conn, na, "na")
    got = rows(conn)
    for cid in (bad, na, none):
        assert (got[cid]["suggestion"], got[cid]["suggest_by"]) == (None, None)
        assert got[cid]["ai_evidence"] is None


# ── ⑤-3 기준 조항 카드·서류 사유 차트 문장(설계 §3) ─────────────────────────


def add_standards(src) -> None:
    """청구 기준 문서 — 분야 문서마다 사유 절(generator/doc_catalog.py CLAIM_DOCS의 묶음)"""
    from generator.doc_catalog import CLAIM_DOCS

    for i, (field, reasons) in enumerate(CLAIM_DOCS.items(), 1):
        body = f"# {field} 분야 청구 기준\n\n머리말.\n\n" + "".join(
            f"## {CLAIM_HEADINGS[k]}\n\n- {CLAIM_HEADINGS[k]} 본문.\n\n" for k in reasons
        )
        src.conn.execute(
            "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
            " VALUES (?, 'claim_standard', ?, '1', '2024-01-01', 'x.md', ?)",
            (i, f"{field} 분야 청구 기준", body),
        )


def save_evidence(conn, cid: int, quotes) -> None:
    import json

    result = None if quotes is None else json.dumps({"quotes": list(quotes)})
    with conn:
        conn.execute(
            "INSERT INTO ax_claim_evidence VALUES (?, '{}', 'h', ?, ?, 'v1', 'm', ?)",
            (cid, result, int(result is not None), AT),
        )


def test_every_reason_card_and_rule(src):
    """상세 사유 11개 모두 카드 절 = CLAIM_HEADINGS, 헷갈리기 쉬운 쌍 — 내역설명 미기재는 보완 후 재청구·채울 항목,
    상병 착오는 삭감 수용(설계 §3 채점)"""
    add_standards(src)
    ids = {
        k: add_claim(src, i, note=r["note"], reviewed=reviewed_for(10))
        for i, (k, r) in enumerate(ADJUST_REASONS.items(), 1)
    }
    got = rows(src.app())
    for k, cid in ids.items():
        card = got[cid]["standard"]
        assert card["heading"] == CLAIM_HEADINGS[k] and card["body"].startswith("- ")
        assert card["doc_title"].endswith("분야 청구 기준")
    assert (got[ids["D01"]]["suggestion"], got[ids["D01"]]["fill_item"]) == (
        "resubmit",
        CLAIM_HEADINGS["D01"],
    )
    assert (got[ids["C01"]]["suggestion"], got[ids["C01"]]["fill_item"]) == ("justified", None)


def test_card_none_without_document(src):
    cid = add_claim(src, 1, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    assert rows(src.app())[cid]["standard"] is None


def test_adjusted_chart_quotes_documentation_only(src):
    doc = add_claim(src, 1, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    empty = add_claim(src, 2, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    none = add_claim(src, 3, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    req = add_claim(src, 4, note=NOTE["requirement"][1], reviewed=reviewed_for(10))
    conn = src.app()
    save_evidence(conn, doc, ["치주낭 5mm, 출혈 있음"])
    save_evidence(conn, empty, [])
    save_evidence(conn, req, ["요건 건에 잘못 저장된 문장"])
    got = rows(conn)
    assert got[doc]["chart_quotes"] == ["치주낭 5mm, 출혈 있음"]
    assert got[empty]["chart_quotes"] == []
    assert got[none]["chart_quotes"] is None and got[req]["chart_quotes"] is None


def test_pending_adjust_union(src):
    """갱신 버튼 삭감 대응 건수 = 2a 할 일 ∪ 서류 사유 차트 문장 할 일"""
    add_standards(src)
    add_claim(src, 1, code=EXTRACT, note=ADJUST_REASONS["R05"]["note"], reviewed=reviewed_for(10))
    add_claim(src, 2, note=NOTE["documentation"][1], reviewed=reviewed_for(10))
    src.conn.execute("UPDATE emr_chart_notes SET note = '38번 매복, 골삭제 후 분할 발치함'")
    assert claims._ai_pending(src.app(), DAY) == {"pre": 0, "adjust": 2}
