"""분류 채점 (평가 설계 §2.2·§2.4·§2.6·§7) — fixture는 eval_db 머리말."""

import json

import pytest

from evaluation import EvalRefused, classification
from tests.evaluation.eval_db import labels, make_db


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
    assert g["purpose"]["by_category"] == {
        "book": {"k": 2, "n": 2},
        "complaint": {"k": 0, "n": 1},
    }  # 가중의 근거 n
    assert r["excluded"] == {"fallback": 1, "failed": 0, "fewshot": 0}
    assert r["model_actual"] == ["claude-haiku-5-5"]


def test_model_vs_gold_test_split_is_test_pool_only(conn):
    g = classification.evaluate(conn, "test")["versions"]["v1"]["model_vs_gold"]
    assert (g["purpose"]["k"], g["purpose"]["n"]) == (1, 1)


def test_no_human_metrics(conn):
    """사람 전수 판정이 없어 모델 vs 사람·사람 vs 골드를 내지 않는다(평가 설계 §2.2) — 운영 판정이 있어도"""
    r = classification.evaluate(conn, "dev")
    assert "human_vs_gold" not in r and "model_vs_human" not in r["versions"]["v1"]


def test_judge_vs_gold(conn):
    """판정기 vs 골드(평가 설계 §2.6) — 분할은 모델 vs 골드와 같다(dev = 비풀 + dev 풀). dev 1·2·4·5 중 2 틀림 → 3/4,
    가중: book 모집단 4 / 표본 3, complaint 1 / 1 → (4 + 0) / 5. test = 통화 3 → 0/1. 분류 채점에는 섞지 않는다"""
    (j,) = classification.evaluate_judge(conn, "dev").items()
    assert j[0] == "v1" and j[1]["model_actual"] == ["claude-opus-5-5"]
    g = j[1]["judge_vs_gold"]
    assert (g["purpose"]["k"], g["purpose"]["n"]) == (3, 4)
    assert g["purpose"]["weighted"] == pytest.approx(0.8)
    t = classification.evaluate_judge(conn, "test")["v1"]["judge_vs_gold"]
    assert (t["purpose"]["k"], t["purpose"]["n"]) == (0, 1)
    assert "judge" not in classification.evaluate(conn, "dev")


def test_judge_excludes_fallback(conn):
    conn.execute("UPDATE ax_llm_calls SET fallback_used = 1 WHERE id = 101")
    g = classification.evaluate_judge(conn, "dev")["v1"]["judge_vs_gold"]
    assert (g["purpose"]["k"], g["purpose"]["n"]) == (2, 3)


def test_judge_version_single_or_setting(conn):
    """판정기 버전이 하나면 그것, 여럿이면 ax_settings.prompt_versions의 judge, 그것도 없으면 None"""
    assert classification.judge_version(conn) == "v1"
    conn.execute(
        "INSERT INTO ax_model_judgments (call_uid, model_alias, model_actual, prompt_version, status, labels,"
        " llm_call_id) VALUES ('c-0001', 'judge', 'claude-opus-5-5', 'v2', 'failed', NULL, 101)"
    )
    assert classification.judge_version(conn) is None
    conn.execute(
        "INSERT INTO ax_settings (key, value) VALUES ('prompt_versions', '{\"judge\": \"v2\"}')"
    )
    assert classification.judge_version(conn) == "v2"


def test_crosscheck_agree_error_and_catch(conn):
    """교차검증(평가 설계 §2.5) — 통화 단위, 양쪽 ok·폴백 아닌 골드 통화만(dev 1·2·4). v1은 판정기와 모두 같고 2가 틀림 →
    일치 오류 1/3(가중: book 2·2 + complaint 1 중 1 → 1/5), 틀린 1건에 엇갈림 없음 → 포착 0/1"""
    x = classification.crosscheck(conn, "dev", "v1", "v1")
    assert x["n"] == 3 and (x["disagree"]["k"], x["disagree"]["n"]) == (0, 3)
    assert (x["agree_error"]["k"], x["agree_error"]["n"]) == (1, 3)
    assert x["agree_error"]["weighted"] == pytest.approx(0.2)
    assert (x["catch"]["k"], x["catch"]["n"]) == (0, 1) and x["lift"] is None


def test_crosscheck_catch_and_lift(conn):
    """판정기가 통화 2를 complaint로 고치면 v1의 틀린 1건에 엇갈림이 붙는다 → 포착 1/1, 엇갈림 1/3, 향상 배수 1 ÷ 1/3 = 3.
    가중 엇갈림: complaint 1 / (book 2·2 + complaint 1) = 1/5"""
    conn.execute(
        "UPDATE ax_model_judgments SET labels = ? WHERE call_uid = 'c-0002'",
        (json.dumps(labels("complaint", complaint=1)),),
    )
    x = classification.crosscheck(conn, "dev", "v1", "v1")
    assert (x["catch"]["k"], x["catch"]["n"]) == (1, 1) and x["catch"]["weighted"] == 1
    assert x["disagree"]["weighted"] == pytest.approx(0.2)
    assert x["lift"] == pytest.approx(3)
    assert (x["agree_error"]["k"], x["agree_error"]["n"]) == (0, 2)


def test_crosscheck_urgency_urgent_or_not(conn):
    """급함은 urgent 여부만 비교(평가 설계 §2.5) — 판정기 low·골드 low가 v1 normal과 달라도 엇갈림·오류가 아니다"""
    conn.execute(
        "UPDATE ax_model_judgments SET labels = ? WHERE call_uid = 'c-0001'",
        (json.dumps(labels("book", urgency="low")),),
    )
    conn.execute("UPDATE truth.truth_call_labels SET urgency = 'low' WHERE call_uid = 'c-0004'")
    x = classification.crosscheck(conn, "dev", "v1", "v1")
    assert x["disagree"]["k"] == 0 and x["catch"]["n"] == 1


def test_crosscheck_excludes_fewshot_in_dev(conn):
    """분류 버전 A의 few-shot 예시 통화는 dev 집계에서 뺀다(평가 설계 §7) — 분류 채점과 같은 모수"""
    x = classification.crosscheck(conn, "dev", "v1", "v1", fewshot={"c-0002"})
    assert x["n"] == 2 and x["agree_error"]["k"] == 0
    assert classification.crosscheck(conn, "test", "v1", "v1", fewshot={"c-0003"})["n"] == 1


def test_crosscheck_core_disagreement(conn):
    """v2는 통화 2에서 판정기와 목적이 달라 핵심 엇갈림 — v2 틀린 통화 없음 → 포착 0/0"""
    x = classification.crosscheck(conn, "dev", "v2", "v1")
    assert (x["disagree"]["k"], x["core"]["k"]) == (1, 1)
    assert (x["agree_error"]["k"], x["agree_error"]["n"]) == (0, 2)
    assert x["catch"]["n"] == 0


def test_crosscheck_skipped_when_same_model(conn):
    """모델 A·B가 같은 구성은 교차검증 지표를 내지 않는다(평가 설계 §2.5)"""
    conn.execute("UPDATE ax_model_judgments SET model_actual = 'claude-haiku-5-5'")
    assert classification.crosscheck(conn, "dev", "v1", "v1") == {
        "a": "v1",
        "b": "v1",
        "skipped": "모델 A·B가 같음",
    }


def test_fewshot_calls_excluded_from_dev(conn):
    r = classification.evaluate(conn, "dev", fewshot={"v1": {"c-0001"}})["versions"]["v1"]
    assert (r["model_vs_gold"]["purpose"]["k"], r["model_vs_gold"]["purpose"]["n"]) == (1, 2)
    assert r["excluded"]["fewshot"] == 1


def test_version_compare_on_intersection(conn):
    """두 버전 모두 ok·폴백 아님인 통화 교집합으로만, 골드 기준(1·2·4) 2/3 → 3/3"""
    (gold,) = classification.evaluate(conn, "dev")["compare"]
    assert (gold["a"], gold["b"], gold["n"]) == ("v1", "v2", 3)
    p = gold["items"]["primary_purpose"]
    assert (p["a"], p["b"], p["p"]) == (2, 3, 1.0)


def test_versions_sort_naturally():
    assert sorted(["v10", "v2", "v1", "v3-final"], key=classification.version_key) == [
        "v1",
        "v2",
        "v3-final",
        "v10",
    ]


def test_two_models_in_one_version_refused(conn):
    """같은 버전에 모델이 둘이면 정확도가 섞인다 — 교차검증(평가 설계 §2.5) 착수 전엔 막는다"""
    conn.execute(
        "INSERT INTO ax_llm_calls (id, task, model_alias, model_actual, prompt_version, ok, created_at)"
        " VALUES (99, 'classify', 'classify_b', 'claude-sonnet-5-5', 'v1', 1, '2026-12-01 09:00:00')"
    )
    conn.execute(
        "INSERT INTO ax_call_classifications (call_uid, model_alias, model_actual, prompt_version, status, labels, llm_call_id)"
        " VALUES ('c-0001', 'classify_b', 'claude-sonnet-5-5', 'v1', 'ok', '{}', 99)"
    )
    with pytest.raises(EvalRefused, match="모델"):
        classification.evaluate(conn, "dev")


def test_blind_llm_and_human_pairwise(conn):
    """LLM 쌍 비교와 사람 투표를 따로 — 버전 쌍별 승수(좌우를 풀어 버전에 돌림) + 좌/우 선택 비율(평가 설계 §2.4)"""
    b = classification.evaluate(conn, "dev")["blind"]
    (llm,) = b["llm"]
    assert llm["judge"] == "blind_judge/v1"
    assert [s["wins"] for s in llm["sides"]] == [0, 1] and llm["tie"] == 1
    (human,) = b["human"]
    assert human["sides"] == [
        {"version": "v1", "model": "claude-haiku-5-5", "wins": 0},
        {"version": "v2", "model": "claude-haiku-5-5", "wins": 2},
    ]
    assert (human["left"], human["right"]) == (1, 1)
    assert classification.evaluate(conn, "test")["blind"] == {
        "llm": [],
        "human": [],
        "agreement": [],
    }


def test_blind_agreement_on_same_pair(conn):
    """같은 두 분류에 LLM과 사람이 모두 고른 건만, 쌍 비교 판정기 버전별. 통화 1 같음, 통화 2 다름(LLM tie)"""
    assert classification.evaluate(conn, "dev")["blind"]["agreement"] == [
        {"judge": "blind_judge/v1", "k": 1, "n": 2}
    ]


def test_blind_agreement_matches_winner_across_sides(conn):
    """좌우가 달라도 이긴 쪽 분류로 맞춘다 — 통화 2: LLM은 (v1 좌, v2 우)에서 right, 사람은 (v2 좌, v1 우)에서 left"""
    conn.execute(
        "UPDATE ax_model_blind_votes SET choice = 'right' WHERE left_classification_id = 2"
    )
    (ag,) = classification.evaluate(conn, "dev")["blind"]["agreement"]
    assert (ag["k"], ag["n"]) == (2, 2)


def test_blind_agreement_not_mixed_across_judge_versions(conn):
    """쌍 비교 판정기 v2가 통화 1에서 사람과 다르게 골라도 v1의 일치로 세지 않는다 — 버전마다 따로"""
    conn.executemany(
        "INSERT INTO ax_llm_calls (id, task, model_alias, model_actual, prompt_version, ok, created_at)"
        " VALUES (?, 'blind_judge', 'blind_judge', 'claude-opus-5-5', 'v2', 1, '2026-12-02 11:00:00')",
        [(301,), (302,)],
    )
    conn.execute(
        "INSERT INTO ax_model_blind_votes (left_classification_id, right_classification_id, model_alias,"
        " model_actual, prompt_version, first_choice, second_choice, choice, first_llm_call_id,"
        " second_llm_call_id, created_at)"
        " VALUES (1, 7, 'blind_judge', 'claude-opus-5-5', 'v2', 'left', 'left', 'left', 301, 302,"
        " '2026-12-02 11:00:00')"
    )
    assert classification.evaluate(conn, "dev")["blind"]["agreement"] == [
        {"judge": "blind_judge/v1", "k": 1, "n": 2},
        {"judge": "blind_judge/v2", "k": 0, "n": 1},
    ]


def test_blind_test_split_only_pairs_with_requested_version(conn):
    """test는 요청한 버전이 낀 쌍만 — 다른 버전 쌍의 test 블라인드가 이 묶음으로 보이지 않게(평가 설계 §2.4)"""
    conn.execute("UPDATE source.ax_calls SET in_blind_pool = 1 WHERE call_uid = 'c-0003'")
    conn.execute(
        "INSERT INTO ax_blind_votes (left_classification_id, right_classification_id, choice, voted_by,"
        " voted_at) VALUES (3, 9, 'right', '데스크', '2026-12-01 11:00:00')"
    )
    assert len(classification.evaluate(conn, "test", ["v2"])["blind"]["human"]) == 1
    assert classification.evaluate(conn, "test", ["v3"])["blind"]["human"] == []


def test_blind_llm_excludes_fallback(conn):
    """쌍 비교의 두 호출 중 하나라도 폴백이면 뺀다 — 통화 2의 둘째 호출(204)"""
    conn.execute("UPDATE ax_llm_calls SET fallback_used = 1 WHERE id = 204")
    (llm,) = classification.evaluate(conn, "dev")["blind"]["llm"]
    assert llm["tie"] == 0 and [s["wins"] for s in llm["sides"]] == [0, 1]


def test_blind_excludes_fallback(conn):
    """한쪽 분류라도 폴백이면 그 투표는 뺀다 (평가 설계 §1 원칙 5) — 통화 1의 v1 분류(id 1)를 폴백으로"""
    conn.execute("UPDATE ax_llm_calls SET fallback_used = 1 WHERE id = 1")
    (h,) = classification.evaluate(conn, "dev")["blind"]["human"]
    assert [s["wins"] for s in h["sides"]] == [0, 1] and (h["left"], h["right"]) == (1, 0)


def test_dump_lists_wrong_items_in_dev_pool(conn):
    """dev 판정 풀만, 골드 기준 — 비풀(재료·검증)은 사람이 건별로 보지 않는다(평가 설계 §2.7)"""
    dump = classification.evaluate(conn, "dev", dump=True)["dump"]
    assert {(d["call_uid"], d["version"], d["item"]) for d in dump} >= {
        ("c-0002", "v1", "primary_purpose")
    }
    assert all(d["call_uid"] in ("c-0001", "c-0002") for d in dump)
    assert all("human" not in d for d in dump)
    assert "dump" not in classification.evaluate(conn, "dev")


def test_calls_read_from_source_even_if_app_has_same_table(conn):
    """통화는 생성 DB(source)의 ax_calls에서 — app DB에 같은 이름 표(옛 산출물)가 있어도 그걸 읽지 않는다(SQLite는 이름이
    겹치면 main을 먼저 고른다)"""
    # 기준은 source에서 직접 — _calls를 먼저 부르면 준비된 문장이 캐시돼 표를 만든 뒤에도 source를 가리킨다
    want = dict(
        conn.execute("SELECT call_uid, transcript FROM source.ax_calls WHERE source = 'generated'")
    )
    conn.execute("CREATE TABLE main.ax_calls AS SELECT * FROM source.ax_calls")
    conn.execute("UPDATE main.ax_calls SET transcript = '옛 전사'")
    assert {u: c["transcript"] for u, c in classification._calls(conn).items()} == want
