"""verify AI 비서 시나리오 검사(ground_truth/assistant_scenarios.json — AI 비서 설계 §7·§7.1)가 위반을 잡는지.
정상 시나리오 묶음(유형별 수 = ASSISTANT_TYPE_COUNTS)을 만들고 한 곳씩 깨뜨린다. DB는 보지 않는다(conn None)."""

import json
from dataclasses import replace

from generator import verify
from tests.generator.verify_db import CTX


def _turn(user="물어봐", tools=(), args=None, end=None):
    return {"user": user, "gold": {"tools": list(tools), "args": args or {}, "end": end}}


def _draft(chart_no, date):
    return {
        "draft": {
            "chart_no": chart_no,
            "date": date,
            "start_range": ["14:00", "17:30"],
            "staff_role": "doctor",
        }
    }


def _scenario(n, split, type_):
    """split마다 환자·날짜가 다르게 — dev는 C-1xxxx·12/2, test는 C-2xxxx·12/8"""
    chart = f"C-{1 if split == 'dev' else 2}{n:04d}"
    day = "2026-12-02" if split == "dev" else "2026-12-08"
    first = _turn(tools=["query_patient"], args={"chart_no": chart})
    last = {
        "booking": _turn(
            tools=["free_slots", "draft_appointment"],
            args={"chart_no": chart, "date": day},
            end=_draft(chart, day),
        ),
        "refuse": _turn(end={"no_write": True}),
    }.get(type_, _turn(tools=["prep_rows"], args={"date": day}))
    return {"id": f"as-{n:03d}", "split": split, "type": type_, "turns": [first, last]}


def _valid():
    out = []
    for split in ("dev", "test"):
        for type_, k in verify.ASSISTANT_TYPE_COUNTS.items():
            for _ in range(k):
                out.append(_scenario(len(out) + 1, split, type_))
    return out


def _check(tmp_path, scenarios, check=verify.check_assistant_scenarios):
    path = tmp_path / "assistant_scenarios.json"
    path.write_text(json.dumps(scenarios, ensure_ascii=False), encoding="utf-8")
    return check(None, replace(CTX, assistant_scenarios=path))


def test_valid_passes(tmp_path):
    assert _check(tmp_path, _valid()) == []
    assert _check(tmp_path, _valid(), verify.check_assistant_split_isolation) == []


def test_no_file_skips(tmp_path):
    assert verify.check_assistant_scenarios(None, CTX) == []
    ctx = replace(CTX, assistant_scenarios=tmp_path / "없음.json")
    assert verify.check_assistant_split_isolation(None, ctx) == []


def test_type_counts(tmp_path):
    s = _valid()
    s[0]["type"] = "lookup"  # dev booking 하나가 lookup으로 — 두 유형 수가 어긋남
    bad = _check(tmp_path, s)
    assert {v["type"] for v in bad if "expected" in v} == {"booking", "lookup"}


def test_ids_unique_and_format(tmp_path):
    s = _valid()
    s[1]["id"] = s[0]["id"]
    s[2]["id"] = "as-1"
    bad = _check(tmp_path, s)
    assert any(v.get("problem") == "id 중복" for v in bad)
    assert any(v.get("problem") == "id 형식" for v in bad)


def test_split_and_turn_shape(tmp_path):
    s = _valid()
    s[0]["split"] = "train"
    s[1]["turns"] = s[1]["turns"][:1]  # 턴 2~4개
    s[2]["turns"][0]["user"] = " "
    s[3]["turns"][1]["gold"]["end"] = {"draft": {"chart_no": "C-10001"}}  # 칸 모자람
    bad = {v["id"]: v["problem"] for v in _check(tmp_path, s) if "id" in v}
    assert bad == {
        s[0]["id"]: "split",
        s[1]["id"]: "턴 수",
        s[2]["id"]: "빈 문장",
        s[3]["id"]: "end 모양",
    }


def test_booking_ends_with_draft_and_refuse_with_no_write(tmp_path):
    s = _valid()
    booking = next(x for x in s if x["type"] == "booking")
    refuse = next(x for x in s if x["type"] == "refuse")
    booking["turns"][-1]["gold"]["end"] = None
    refuse["turns"][-1]["gold"]["end"] = None
    bad = {v["id"]: v["problem"] for v in _check(tmp_path, s) if "id" in v}
    assert bad == {booking["id"]: "예약 초안 없음", refuse["id"]: "쓰기 거절 없음"}


def test_split_isolation(tmp_path):
    s = _valid()
    test = next(x for x in s if x["split"] == "test")
    test["turns"][0]["gold"]["args"]["chart_no"] = "C-10001"  # dev 환자
    test["turns"][1]["gold"]["args"] = {"date_from": "2026-11-30", "date_to": "2026-12-03"}
    bad = _check(tmp_path, s, verify.check_assistant_split_isolation)
    assert {(v["kind"], v["value"]) for v in bad} == {
        ("patient", "C-10001"),
        ("day", "2026-12-02"),  # 범위는 날마다 펼쳐 본다
    }
