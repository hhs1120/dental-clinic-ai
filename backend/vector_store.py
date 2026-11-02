"""벡터 저장 (앱 설계 §3·§7). sqlite-vec으로 app.sqlite 안에서 코사인을 전수 계산한다 — 근사 인덱스 없음.
벡터 접근은 이 클래스 뒤에만 둔다. 모델마다 한 벌이다."""

import sqlite3

import numpy as np


class VectorStore:
    def __init__(self, conn: sqlite3.Connection, model: str):
        self.conn, self.model = conn, model

    def upsert(self, ids: list[str], vectors: list[np.ndarray]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO ax_chunk_vectors VALUES (?, ?, ?)",
            [
                (i, self.model, np.asarray(v, dtype="float32").tobytes())
                for i, v in zip(ids, vectors, strict=True)
            ],
        )

    def query(self, vector: np.ndarray, where: tuple | None, k: int) -> list[str]:
        """코사인이 가까운 순서로 k개. where는 ax_chunks c에 거는 (SQL, 인자) — 필터 뒤에 k개를 고른다"""
        sql, args = where or ("1 = 1", [])
        rows = self.conn.execute(
            "SELECT v.chunk_id FROM ax_chunk_vectors v JOIN ax_chunks c ON c.id = v.chunk_id"
            f" WHERE v.model = ? AND {sql}"
            " ORDER BY vec_distance_cosine(v.embedding, ?), v.chunk_id LIMIT ?",
            [self.model, *args, np.asarray(vector, dtype="float32").tobytes(), k],
        ).fetchall()
        return [r[0] for r in rows]
