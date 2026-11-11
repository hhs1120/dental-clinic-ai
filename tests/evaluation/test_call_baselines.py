"""① 분류 강한 기준선(평가 설계 §2.2, §1 원칙 8) — evaluation/call_baselines.py. 학습 없는 할 일 규칙과 silver 재료로만
학습한 글자 n-gram 기준선을 분류기와 같은 통화(재료 밖 골드)에서 나란히 잰다."""

from evaluation import call_baselines as cb
from tests.evaluation.eval_db import labels


def test_rule_has_todo_reads_staff_lines_only():
    assert cb.rule_has_todo("직원: 확인해서 다시 연락드릴게요.\n환자: 네.") == 1
    assert cb.rule_has_todo("직원: 원장님께 전달해 드릴게요.") == 1
    assert cb.rule_has_todo("직원: 내원하시면 원장님이 보실 거예요.\n환자: 연락드릴게요.") == 0


def test_ngram_learns_and_predicts():
    model = cb.train(
        [("예약 잡고 싶어요 예약", "book")] * 3 + [("이가 너무 아파요 통증", "emergency")] * 3
    )
    assert cb.predict(model, "다음 주에 예약 잡을게요") == "book"
    assert cb.predict(model, "어제부터 너무 아파요") == "emergency"


def _call(split, purpose, todo, text):
    return {
        "uid": text,
        "split": split,
        "pool": split != "silver",
        "transcript": text,
        "gold": labels(purpose, has_todo=todo),
    }


def test_evaluate_trains_on_material_and_scores_outside():
    """재료로 학습한 통화는 채점에서 빠지고, 분류기 버전도 같은 통화에서 센다"""
    calls = {}
    for k in range(40):
        purpose, todo = ("book", 1) if k % 2 else ("cancel", 0)
        word = "직원: 다시 연락드릴게요. 예약" if todo else "직원: 취소해 드렸어요. 취소"
        calls[f"c-{k:04d}"] = _call("silver", purpose, todo, f"{word} {k}")
    calls["c-dev1"] = _call("dev", "book", 1, "직원: 다시 연락드릴게요. 예약")
    calls["c-test"] = _call("test", "cancel", 0, "직원: 취소해 드렸어요. 취소")
    usable = {"v1": {u: c["gold"] for u, c in calls.items()}}
    r = cb.evaluate(calls, "dev", usable)
    material = r["trained_on"]
    assert 0 < material < 40
    assert r["n"] == 41 - material  # 재료 밖 silver + dev 풀, test 풀 제외
    assert r["rule"]["has_todo"] == {"k": r["n"], "n": r["n"]}
    assert r["ngram"]["primary_purpose"]["n"] == r["n"]
    assert r["versions"]["v1"]["primary_purpose"] == {"k": r["n"], "n": r["n"]}
    t = cb.evaluate(calls, "test", usable)
    assert t["n"] == 1 and t["trained_on"] == material


def test_urgency_compared_urgent_or_not():
    """기준선 급함도 urgent 여부만(분류기 채점과 같은 기준 — shared.call_labels.same_value)"""
    calls = {
        f"c-{k:04d}": _call("silver", "book", 0, f"직원: 예약 {k}")
        | {"gold": labels("book", urgency="low")}
        for k in range(20)
    }
    calls["c-dev1"] = _call("dev", "book", 0, "직원: 예약") | {
        "gold": labels("book", urgency="normal")
    }
    r = cb.evaluate(calls, "dev", {})
    assert r["ngram"]["urgency"]["k"] == r["ngram"]["urgency"]["n"]


def test_report_has_baseline_table(tmp_path):
    from evaluation import classification
    from evaluation import eval as ev
    from tests.evaluation.eval_db import make_db

    c = classification.evaluate(make_db(tmp_path), "dev")
    assert "baselines" in c
    text = "\n".join(ev._render_classification(c))
    assert "강한 기준선과 나란히" in text
