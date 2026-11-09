"""Voyage 검색 호출 비용 기록 (설계 2026-11-09 챗 "Voyage 비용", 앱 설계 §2). 질의 임베딩·리랭커 호출을
ax_llm_calls에 1행씩 남겨 하루 상한(daily_usd_limit.runtime)이 센다. 호출 경로는 게이트웨이 밖 그대로이고,
기록은 하네스 도구 트랜잭션 밖에서 쓴다(읽기 도구 안의 쓰기는 하네스가 막는다)."""

import sqlite3
from datetime import datetime

from shared.llm import load_config

ALIAS = "voyage"
PER_TOKENS = 1_000_000  # config/llm.yaml 단가는 100만 토큰당


def record_voyage(
    conn: sqlite3.Connection, usage: list, *, is_eval: bool, ref: tuple[str, str] | None
) -> float:
    """usage [(task, model, tokens)] → 기록한 비용 합. 무료 토큰 구간도 정가로 적는다"""
    models = load_config()["models"]
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    total = 0.0
    for task, model, tokens in usage:
        cost = tokens * models[model]["input"] / PER_TOKENS
        total += cost
        conn.execute(
            "INSERT INTO ax_llm_calls (task, model_alias, model_actual, ok, is_eval, created_at,"
            " input_tokens, cost_usd, ref_type, ref_id) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (
                task,
                ALIAS,
                model,
                int(is_eval),
                now,
                tokens,
                cost,
                ref[0] if ref else None,
                str(ref[1]) if ref else None,
            ),
        )
    conn.commit()
    return total
