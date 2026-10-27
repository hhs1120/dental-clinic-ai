"""평가 테스트용 작은 DB — 앱 스키마(shared/schema.sql) + 정답 스키마, 통화 6건·분류 두 버전·판정·투표·RAG 질문.

통화(생성 5 + 붙여넣기 1): 1·2 dev 판정 풀(블라인드 풀) / 3 test 판정 풀 / 4·5 비풀(silver) / 6 붙여넣기(골드 없음).
골드 목적: 1·3·4·5 book, 2 complaint. 모집단(생성 전사) = book 4 · complaint 1
분류 v1: 1 book ✓ · 2 book ✗ · 3 book ✓ · 4 book ✓ · 5 reschedule(폴백 — 집계 제외) · 6 book
분류 v2: 1·3·4 book ✓ · 2 complaint ✓ · 5 failed
판정: 1 book · 2 complaint. 투표: 통화 1(좌 v1·우 v2 → right), 통화 2(좌 v2·우 v1 → left) — 둘 다 v2 승
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = (ROOT / "shared" / "schema.sql").read_text(encoding="utf-8")
TRUTH_SCHEMA = (ROOT / "ground_truth" / "schema.sql").read_text(encoding="utf-8")

ITEM_DEFAULTS = {
    "counterpart": "patient",
    "urgency": "normal",
    "emergency": 0,
    "complaint": 0,
    "price_inquiry": 0,
    "wants_appt": 0,
    "appt_confirmed": 0,
    "has_todo": 0,
}


def labels(purpose, **over):
    return dict(ITEM_DEFAULTS, primary_purpose=purpose, **over)


CALLS = [  # (id, call_uid, source, in_eval_pool, in_blind_pool, gold purpose, split)
    (1, "c-0001", "generated", 1, 1, "book", "dev"),
    (2, "c-0002", "generated", 1, 1, "complaint", "dev"),
    (3, "c-0003", "generated", 1, 0, "book", "test"),
    (4, "c-0004", "generated", 0, 0, "book", "silver"),
    (5, "c-0005", "generated", 0, 0, "book", "silver"),
    (6, "c-0006", "pasted", 0, 0, None, None),
]
V1 = {1: "book", 2: "book", 3: "book", 4: "book", 5: "reschedule", 6: "book"}
V2 = {1: "book", 2: "complaint", 3: "book", 4: "book"}


def make_db(tmp_path) -> sqlite3.Connection:
    truth = sqlite3.connect(tmp_path / "truth.sqlite")
    truth.executescript(TRUTH_SCHEMA)
    for _cid, uid, _source, _pool, _blind, purpose, split in CALLS:
        if purpose:
            g = labels(purpose, complaint=int(purpose == "complaint"))
            truth.execute(
                "INSERT INTO truth_call_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', ?)",
                (
                    uid,
                    purpose,
                    json.dumps([purpose]),
                    g["counterpart"],
                    g["urgency"],
                    g["emergency"],
                    g["complaint"],
                    g["price_inquiry"],
                    g["wants_appt"],
                    g["appt_confirmed"],
                    g["has_todo"],
                    split,
                ),
            )
    truth.commit()
    truth.close()
    conn = sqlite3.connect(tmp_path / "app.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for cid, uid, source, pool, blind, _purpose, _split in CALLS:
        conn.execute(
            "INSERT INTO ax_calls (id, call_uid, source, direction, started_at, duration_sec, has_transcript, transcript,"
            " in_eval_pool, eval_sort_order, judge_status, in_blind_pool) VALUES (?, ?, ?, 'inbound', '2026-11-02 10:00:00',"
            " 120, 1, ?, ?, ?, ?, ?)",
            (
                cid,
                uid,
                source,
                f"환자: 통화 {cid}",
                pool,
                cid if pool else None,
                "done" if cid in (1, 2) else "none",
                blind,
            ),
        )
    cls_id = 0
    for version, preds in (("v1", V1), ("v2", V2)):
        for cid in range(1, 7):
            cls_id += 1
            if cid not in preds and (version, cid) != ("v2", 5):
                continue
            # 분류마다 LLM 호출 1행(id = 분류 id) — v1 통화 5만 폴백
            conn.execute(
                "INSERT INTO ax_llm_calls (id, task, model_alias, model_actual, prompt_version, fallback_used, ok, created_at)"
                " VALUES (?, 'classify', 'classify', 'claude-haiku-5-5', ?, ?, 1, '2026-12-01 09:00:00')",
                (cls_id, version, int((version, cid) == ("v1", 5))),
            )
            if cid in preds:
                conn.execute(
                    "INSERT INTO ax_call_classifications (id, call_id, model_alias, model_actual, prompt_version, status,"
                    " labels, summary, llm_call_id) VALUES (?, ?, 'classify', 'claude-haiku-5-5', ?, 'ok', ?, '요약', ?)",
                    (
                        cls_id,
                        cid,
                        version,
                        json.dumps(labels(preds[cid], complaint=int(preds[cid] == "complaint"))),
                        cls_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO ax_call_classifications (id, call_id, model_alias, model_actual, prompt_version, status,"
                    " llm_call_id) VALUES (?, 5, 'classify', 'claude-haiku-5-5', 'v2', 'failed', ?)",
                    (cls_id, cls_id),
                )
    for cid, purpose in ((1, "book"), (2, "complaint")):
        conn.execute(
            "INSERT INTO ax_judgments (call_id, labels, judged_by, judged_at) VALUES (?, ?, '데스크', '2026-12-01 10:00:00')",
            (cid, json.dumps(labels(purpose, complaint=int(purpose == "complaint")))),
        )
    # 분류 id: v1 = 1..6(통화 id), v2 = 7..12
    conn.executemany(
        "INSERT INTO ax_blind_votes (left_classification_id, right_classification_id, choice, voted_by, voted_at)"
        " VALUES (?, ?, ?, '데스크', '2026-12-01 11:00:00')",
        [(1, 7, "right"), (8, 2, "left")],
    )
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_path / "truth.sqlite"),))
    return conn
