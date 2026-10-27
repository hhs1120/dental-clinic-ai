"""분류 채점 (평가 설계 §2.2·§2.4·§7) — fixture는 eval_db 머리말."""
import pytest

from evaluation import EvalRefused, classification
from tests.evaluation.eval_db import make_db


@pytest.fixture
def conn(tmp_path):
    return make_db(tmp_path)


def test_model_vs_gold_dev_weighted(conn):
    """dev 골드 = 비풀 + dev 풀(test 풀 제외), 폴백(통화 5)·붙여넣기(6) 제외. 역확률 가중: book 모집단 4 / 표본 2 = 2,
    complaint 1 / 1 = 1 → (2 + 2 + 0) / 5"""
    r = classification.evaluate(conn, "dev")["versions"]["v1"]
    g = r["model_vs_gold"]
    assert (g["purpose"]["k"], g["purpose"]["n"]) == (2, 3)
    assert g["purpose"]["weighted"] == pytest.approx(0.8)
    assert g["purpose"]["macro"] == pytest.approx(0.5)
    assert (g["items"]["complaint"]["k"], g["items"]["complaint"]["n"]) == (2, 3)
    assert g["purpose"]["by_category"] == {"book": {"k": 2, "n": 2}, "complaint": {"k": 0, "n": 1}}  # 가중의 근거 n
    assert r["excluded"] == {"fallback": 1, "failed": 0, "fewshot": 0}
    assert r["model_actual"] == ["claude-haiku-5-5"]


def test_model_vs_gold_test_split_is_test_pool_only(conn):
    g = classification.evaluate(conn, "test")["versions"]["v1"]["model_vs_gold"]
    assert (g["purpose"]["k"], g["purpose"]["n"]) == (1, 1)


def test_model_vs_human_and_human_vs_gold(conn):
    """판정 풀의 그 split 판정만 — 사람 vs 골드는 생성 라벨 타당성"""
    r = classification.evaluate(conn, "dev")
    h = r["versions"]["v1"]["model_vs_human"]
    assert (h["purpose"]["k"], h["purpose"]["n"]) == (1, 2)
    assert h["purpose"]["weighted"] == pytest.approx(4 / 5)  # book 4/1 = 4, complaint 1/1 = 1
    assert (r["human_vs_gold"]["purpose"]["k"], r["human_vs_gold"]["purpose"]["n"]) == (2, 2)
    assert classification.evaluate(conn, "test")["human_vs_gold"]["purpose"]["n"] == 0


def test_fewshot_calls_excluded_from_dev(conn):
    r = classification.evaluate(conn, "dev", fewshot={"v1": {"c-0001"}})["versions"]["v1"]
    assert (r["model_vs_gold"]["purpose"]["k"], r["model_vs_gold"]["purpose"]["n"]) == (1, 2)
    assert r["excluded"]["fewshot"] == 1


def test_version_compare_on_intersection(conn):
    """두 버전 모두 ok·폴백 아님인 통화 교집합으로만 — 골드 기준(1·2·4) 2/3 → 3/3, 사람 기준(판정된 1·2) 1/2 → 2/2"""
    gold, human = classification.evaluate(conn, "dev")["compare"]
    assert (gold["ref"], gold["a"], gold["b"], gold["n"]) == ("gold", "v1", "v2", 3)
    p = gold["items"]["primary_purpose"]
    assert (p["a"], p["b"], p["p"]) == (2, 3, 1.0)
    assert (human["ref"], human["n"], human["items"]["primary_purpose"]["a"], human["items"]["primary_purpose"]["b"]) == \
        ("human", 2, 1, 2)


def test_versions_sort_naturally():
    assert sorted(["v10", "v2", "v1", "v3-final"], key=classification.version_key) == ["v1", "v2", "v3-final", "v10"]


def test_two_models_in_one_version_refused(conn):
    """같은 버전에 모델이 둘이면 정확도가 섞인다 — 교차검증(평가 설계 §2.5) 착수 전엔 막는다"""
    conn.execute("INSERT INTO ax_llm_calls (id, task, model_alias, model_actual, prompt_version, ok, created_at)"
                 " VALUES (99, 'classify', 'classify_b', 'claude-sonnet-5-5', 'v1', 1, '2026-12-01 09:00:00')")
    conn.execute("INSERT INTO ax_call_classifications (call_id, model_alias, model_actual, prompt_version, status, labels, llm_call_id)"
                 " VALUES (1, 'classify_b', 'claude-sonnet-5-5', 'v1', 'ok', '{}', 99)")
    with pytest.raises(EvalRefused, match="모델"):
        classification.evaluate(conn, "dev")


def test_blind_pairwise_and_position(conn):
    """버전 쌍별 승수(좌우를 풀어 버전에 돌림) + 좌/우 선택 비율"""
    (b,) = classification.evaluate(conn, "dev")["blind"]
    assert b["sides"] == [{"version": "v1", "model": "claude-haiku-5-5", "wins": 0},
                          {"version": "v2", "model": "claude-haiku-5-5", "wins": 2}]
    assert b["tie"] == 0 and b["both_wrong"] == 0
    assert (b["left"], b["right"]) == (1, 1)
    assert classification.evaluate(conn, "test")["blind"] == []


def test_blind_excludes_fallback(conn):
    """한쪽 분류라도 폴백이면 그 투표는 뺀다 (평가 설계 §1 원칙 5) — 통화 1의 v1 분류(id 1)를 폴백으로"""
    conn.execute("UPDATE ax_llm_calls SET fallback_used = 1 WHERE id = 1")
    (b,) = classification.evaluate(conn, "dev")["blind"]
    assert [s["wins"] for s in b["sides"]] == [0, 1] and (b["left"], b["right"]) == (1, 0)


def test_dump_lists_wrong_items_in_dev_pool(conn):
    dump = classification.evaluate(conn, "dev", dump=True)["dump"]
    assert {(d["call_uid"], d["version"], d["item"]) for d in dump} >= {("c-0002", "v1", "primary_purpose")}
    assert all(d["call_uid"] in ("c-0001", "c-0002") for d in dump)
    assert "dump" not in classification.evaluate(conn, "dev")
