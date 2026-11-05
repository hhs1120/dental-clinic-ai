"""분류기 vs 판정기 엇갈림 (앱 설계 §1, 분류기 설계 "엇갈림"). 두 버전 모두 ok인 통화마다 셋 중 하나면 1행:
(가) 9항목 중 하나라도 다름 (나) 라벨·요약·판정 호출 중 하나라도 폴백 — 라벨이 같아도, 폴백 건은 자동 종결하지 않는다
(다) 분류기 할 일 목록이 있는데 분류기 has_todo = 0 — 라벨과 할 일 목록을 다른 모델이 내므로
"""

import json

from shared.call_labels import CORE_ITEMS, ITEMS


def compute(conn, a_version: str, b_version: str) -> int:
    """그 버전 쌍의 행을 다시 쓰고 엇갈림 행 수를 돌려준다"""
    rows = conn.execute(
        "SELECT c.call_uid, c.labels AS a, c.todo, j.labels AS b,"
        " la.fallback_used OR COALESCE(ls.fallback_used, 0) OR lb.fallback_used AS fallback"
        " FROM ax_call_classifications c"
        " JOIN ax_model_judgments j ON j.call_uid = c.call_uid"
        "  AND j.model_alias = 'judge' AND j.prompt_version = ? AND j.status = 'ok'"
        " JOIN ax_llm_calls la ON la.id = c.llm_call_id"
        " LEFT JOIN ax_llm_calls ls ON ls.id = c.summary_llm_call_id"
        " JOIN ax_llm_calls lb ON lb.id = j.llm_call_id"
        " WHERE c.model_alias = 'classify' AND c.prompt_version = ? AND c.status = 'ok'",
        (b_version, a_version),
    ).fetchall()
    out = []
    for r in rows:
        a, b = json.loads(r["a"]), json.loads(r["b"])
        items = [i for i in ITEMS if a[i] != b[i]]
        todo_mismatch = bool(json.loads(r["todo"] or "[]")) and not a["has_todo"]
        if items or r["fallback"] or todo_mismatch:
            out.append(
                (
                    r["call_uid"],
                    a_version,
                    b_version,
                    int(any(i in CORE_ITEMS for i in items)),
                    int(bool(r["fallback"])),
                    int(todo_mismatch),
                    json.dumps(items),
                )
            )
    conn.execute(
        "DELETE FROM ax_call_disagreements WHERE a_version = ? AND b_version = ?",
        (a_version, b_version),
    )
    conn.executemany("INSERT INTO ax_call_disagreements VALUES (?, ?, ?, ?, ?, ?, ?)", out)
    conn.commit()
    return len(out)
