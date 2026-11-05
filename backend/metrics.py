"""정확도 표 집계 (기능 정의 §4 ② "정확도 표"). 앱 DB에 골드가 없어 모델 vs 판정기 일치율만 — 골드 지표는 eval.py.
모수는 dev 판정 풀(앱 설계 §6), 블라인드는 dev 블라인드 통화(평가 설계 §2.4)."""

import json
import sqlite3

from backend import judge_queue, pools
from shared.call_labels import ITEMS
from shared.clinic import Clinic


def agreement(
    conn: sqlite3.Connection, clinic: Clinic, versions: list[str], judge_version: str
) -> dict:
    """{분류 버전: {항목: {k, n}}} — 분류·판정기 모두 ok인 dev 판정 풀 통화, 급함은 3값 그대로(정확도 표)"""
    out = {}
    for v in versions:
        rows = conn.execute(
            "SELECT k.labels AS a, j.labels AS b FROM source.ax_calls x"
            " JOIN ax_call_classifications k ON k.call_uid = x.call_uid"
            "  AND k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok'"
            " JOIN ax_model_judgments j ON j.call_uid = x.call_uid"
            "  AND j.model_alias = 'judge' AND j.prompt_version = ? AND j.status = 'ok'"
            f" WHERE {pools.dev_eval_sql(clinic, 'x')}",
            (v, judge_version),
        ).fetchall()
        pairs = [(json.loads(r["a"]), json.loads(r["b"])) for r in rows]
        out[v] = {i: {"k": sum(a[i] == b[i] for a, b in pairs), "n": len(pairs)} for i in ITEMS}
    return out


def _votes(conn, clinic: Clinic, table: str) -> list[sqlite3.Row]:
    """dev 블라인드 통화의 투표 + 좌우 분류 버전"""
    return conn.execute(
        "SELECT v.left_classification_id AS l, v.right_classification_id AS r, v.choice,"
        " kl.prompt_version AS lv, kr.prompt_version AS rv FROM " + table + " v"
        " JOIN ax_call_classifications kl ON kl.id = v.left_classification_id"
        " JOIN ax_call_classifications kr ON kr.id = v.right_classification_id"
        " JOIN source.ax_calls x ON x.call_uid = kl.call_uid"
        f" WHERE {pools.dev_blind_sql(clinic, 'x')}"
    ).fetchall()


def _rates(rows, a: str, b: str) -> dict:
    out = {"b_better": 0, "a_better": 0, "tie": 0, "both_wrong": 0, "n": 0, "left": 0, "right": 0}
    for v in rows:
        if {v["lv"], v["rv"]} != {a, b}:
            continue
        out["n"] += 1
        if v["choice"] in ("left", "right"):
            out[v["choice"]] += 1
            picked = v["lv"] if v["choice"] == "left" else v["rv"]
            out["b_better" if picked == b else "a_better"] += 1
        else:
            out[v["choice"]] += 1
    return out


def blind_rates(conn: sqlite3.Connection, clinic: Clinic, a: str, b: str) -> dict:
    """LLM 쌍 비교·사람 투표의 승률(b가 나음/a가 나음/비슷/둘 다 틀림)·좌우 선택 수, 같은 쌍에서 둘의 일치"""
    llm, human = (
        _votes(conn, clinic, "ax_model_blind_votes"),
        _votes(conn, clinic, "ax_blind_votes"),
    )
    llm_by = {(v["l"], v["r"]): v["choice"] for v in llm}
    both = [v for v in human if (v["l"], v["r"]) in llm_by and {v["lv"], v["rv"]} == {a, b}]
    return {
        "llm": _rates(llm, a, b),
        "human": _rates(human, a, b),
        "agree": {"k": sum(llm_by[(v["l"], v["r"])] == v["choice"] for v in both), "n": len(both)},
    }


def human_share(conn: sqlite3.Connection, a: str, b: str) -> dict:
    """운영 구간에서 사람이 볼 몫 — 판정 큐 대상(판정 풀 밖·새 통화, 활성 분류 ok) 중 엇갈림 또는 표본"""
    rows = conn.execute(judge_queue.TARGETS_SQL, (a, a, b)).fetchall()
    k = sum(bool(r["disagree"]) or judge_queue.is_sample(r["call_uid"]) for r in rows)
    return {"k": k, "n": len(rows)}
