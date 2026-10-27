"""평가 지표 순수 함수 (평가 설계 §2.2·§3.2·§3.3) — 손으로 계산한 값과 비교."""

import pytest

from evaluation import metrics


def test_fraction_shows_count_and_percent():
    assert metrics.fraction(14, 20) == "14/20 (70%)"
    assert metrics.fraction(0, 0) == "0/0 (—)"
    assert metrics.fraction(1, 8) == "1/8 (13%)"  # 반올림은 0.5 올림


def test_mcnemar_exact():
    """엇갈린 문항(b: A만 맞음, c: B만 맞음)으로 양측 이항 검정"""
    assert metrics.mcnemar_exact(0, 0) == 1.0
    assert metrics.mcnemar_exact(0, 5) == pytest.approx(2 / 32)  # 5건 모두 한쪽 → 2 × (1/2)^5
    assert metrics.mcnemar_exact(3, 3) == 1.0
    assert metrics.mcnemar_exact(1, 9) == pytest.approx(2 * (1 + 10) / 1024)


def test_inverse_weights_and_weighted_accuracy():
    """역확률 가중 w_c = N_c / n_c — 희귀 카테고리를 과표집한 표본을 모집단 비중으로 되돌린다"""
    w = metrics.inverse_weights({"book": 90, "complaint": 10}, {"book": 9, "complaint": 5})
    assert w == {"book": 10.0, "complaint": 2.0}
    rows = [("book", True)] * 9 + [("complaint", False)] * 5
    assert metrics.weighted_accuracy(rows, w) == pytest.approx(90 / 100)  # 비가중이면 9/14
    assert metrics.macro_accuracy(rows) == pytest.approx(0.5)  # 카테고리 동등
    assert metrics.weighted_accuracy([], w) is None


@pytest.mark.parametrize(
    "chunk, source",
    [
        ("doc:12:3", "doc:12"),
        ("call:481:0", "call:481"),
        ("thread:C-00001:2026-07", "thread:C-00001:2026-07"),
        ("chart:55", "chart:55"),
    ],
)
def test_source_id_strips_chunk_index(chunk, source):
    assert metrics.source_id(chunk) == source


def test_first_hit_rank_within_top5():
    answers = {"doc:7", "doc:15"}
    assert metrics.first_hit_rank(["doc:2:0", "doc:15:1", "doc:7:0"], answers) == 2
    assert metrics.first_hit_rank(["doc:1:0"] * 5 + ["doc:7:0"], answers) is None  # 6위는 밖
    assert metrics.first_hit_rank([], answers) is None


def test_number_match_normalizes_units_and_commas():
    assert metrics.number_match("미수는 16,300원 남았어요", 16300)
    assert metrics.number_match("오늘 26명 오세요", 26)
    assert not metrics.number_match("오늘 27명", 26)
    assert not metrics.number_match("126건", 26)  # 다른 수의 일부는 아님
    assert metrics.number_match("1만 6,300원 남았어요", 16300) and metrics.number_match(
        "3만 원", 30000
    )
    assert not metrics.number_match("12월 2일 기준 3건", 2)  # 날짜 숫자는 답이 아님
    assert not metrics.number_match("2026-12-02 기준 3건", 2)
    assert not metrics.number_match("2.5건", 2) and not metrics.number_match("2.5건", 5)
    assert metrics.number_match("100일 동안 19건", 19) and metrics.numbers_in(
        "100일 동안 19건"
    ) == [100, 19]  # 기간 100일은 날짜가 아님
    assert metrics.number_match("2만 5천원", 25000) and metrics.number_match("3천 원", 3000)
    assert metrics.numbers_in("9명만 와요") == [9]  # 단위가 숫자에 바로 붙을 때만
    assert metrics.numbers_in("5천만 원, 1천만 원, 5백만") == [
        50_000_000,
        10_000_000,
        5_000_000,
    ]  # 겹친 단위
    assert metrics.numbers_in("1.5만 원") == [15000] and metrics.numbers_in("1,000만 원") == [
        10_000_000
    ]
    assert metrics.numbers_in("1만 9천 2회") == [
        19000,
        2,
    ]  # 띄어 쓴 나머지는 '원' 앞일 때만 금액에 붙음
    assert metrics.numbers_in("1만 2만") == [10000, 20000]  # 단위가 작아지지 않으면 다른 수
    assert metrics.numbers_in("2만 5천 3백 명") == [25300] and metrics.numbers_in(
        "2만원 중 3천원"
    ) == [20000, 3000]


def test_args_match_date_range_contains():
    """날짜 범위는 정답 범위를 포함하면 일치, 나머지 키는 같은 값 (평가 설계 §3.2)"""
    want = {"date_from": "2026-12-01", "date_to": "2026-12-05", "has_unpaid_balance": True}
    assert metrics.args_match(
        want,
        {
            "date_from": "2026-11-30",
            "date_to": "2026-12-06",
            "has_unpaid_balance": True,
            "type": "hygiene",
        },
    )
    assert not metrics.args_match(
        want, {"date_from": "2026-12-02", "date_to": "2026-12-05", "has_unpaid_balance": True}
    )
    assert not metrics.args_match(want, {"date_from": "2026-12-01", "date_to": "2026-12-05"})
    assert not metrics.args_match(
        want, {"date_from": "2026-12-01", "date_to": "2026-12-05", "has_unpaid_balance": False}
    )


def _call(tool, **args):
    return {"name": tool, "args": args}


def test_tool_path():
    """목록 밖 도구는 실패, 기대 도구는 순서대로 나오면 됨(사이 호출 허용), 기대 인자는 그 도구 호출 중 하나와 일치, 6회 초과 실패"""
    tools, args = ["query_patient", "query_claims"], {"query_claims": {"chart_no": "C-01285"}}
    ok = [
        _call("query_patient", name="고경숙"),
        _call("query_patient", chart_no="C-01285"),
        _call("query_claims", chart_no="C-01285"),
    ]
    assert metrics.tool_path(ok, tools, args) is None
    assert (
        metrics.tool_path(
            [_call("query_claims", chart_no="C-01285"), _call("query_patient", name="x")],
            tools,
            args,
        )
        == "순서"
    )
    assert metrics.tool_path(ok + [_call("search_docs")], tools, args) == "목록 밖 도구"
    assert (
        metrics.tool_path(
            [_call("query_patient", name="x"), _call("query_claims", chart_no="C-9")], tools, args
        )
        == "인자"
    )
    assert (
        metrics.tool_path(
            [_call("query_patient", name="x")] * 6 + [_call("query_claims", chart_no="C-01285")],
            tools,
            args,
        )
        == "호출 6회 초과"
    )


def test_docs_name_the_same_limits():
    """문서의 호출 상한·순위 컷이 코드 상수와 같아야 한다 (CLAUDE.md — 문서에 숫자를 남기면 비교 테스트)"""
    from pathlib import Path

    docs = Path(__file__).resolve().parents[2] / "docs"
    eval_doc = (docs / "치과 AX - 평가 설계.md").read_text(encoding="utf-8")
    app_doc = (docs / "치과 AX - 앱 설계.md").read_text(encoding="utf-8")
    assert (
        f"도구 호출 상한 {metrics.MAX_TOOL_CALLS}회" in eval_doc
        and f"호출 상한 {metrics.MAX_TOOL_CALLS}회" in app_doc
    )
    assert f"MRR@{metrics.TOP_K}" in eval_doc and f"Hit@{metrics.TOP_K}" in eval_doc
