"""실행 기록 저장소 (앱 설계 §3b 상태·저장소 주입). 하네스는 연결을 주입받는다 — 앱은 app.sqlite, 정답을 보는 실행은
그 실행 전용 파일을 넘겨 앱 DB에 흔적을 남기지 않는다. 쓰기는 커밋하지 않고 부르는 쪽 트랜잭션에 든다."""

import json
import sqlite3
from datetime import date, datetime, timedelta

JSON_FIELDS = ("args", "response", "result", "raw_result")


class RunLocked(RuntimeError):
    """같은 실행을 다른 곳이 돌리고 있거나 잠금을 뺏겼다"""


def count_block(conn: sqlite3.Connection, rule_id: str) -> None:
    conn.execute(
        "INSERT INTO ax_rule_counts VALUES (?, 1)"
        " ON CONFLICT (rule_id) DO UPDATE SET blocked = blocked + 1",
        (rule_id,),
    )


def _stamp(at: datetime) -> str:
    return at.isoformat(sep=" ", timespec="seconds")


def _dump(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


class RunStore:
    lock_ttl = 600  # 초 — 하트비트가 이만큼 끊기면 잠금 만료. 호출 최악 시간보다 길어야 한다(harness/llm.py)

    def __init__(self, conn: sqlite3.Connection, clock=datetime.now):
        self.conn, self.clock = conn, clock

    def create_run(self, agent, target, prompt_version, owner, parent_run_id=None) -> int:
        now = _stamp(self.clock())
        try:
            cur = self.conn.execute(
                "INSERT INTO ax_agent_runs (agent, target, status, prompt_version, parent_run_id,"
                " locked_by, heartbeat_at, started_at) VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                (agent, target, prompt_version, parent_run_id, owner, now, now),
            )
        except sqlite3.IntegrityError as e:
            if e.sqlite_errorname != "SQLITE_CONSTRAINT_UNIQUE":
                raise
            raise RunLocked(f"{agent}/{target} 실행 중") from e
        return cur.lastrowid

    def take_over(self, run_id: int, owner: str) -> None:
        """잠금이 비었거나 만료된 실행을 가져온다. 못 가져오면 RunLocked"""
        now = self.clock()
        limit = _stamp(now - timedelta(seconds=self.lock_ttl))
        cur = self.conn.execute(
            "UPDATE ax_agent_runs SET locked_by = ?, heartbeat_at = ? WHERE id = ?"
            " AND status = 'running' AND (locked_by IS NULL OR heartbeat_at < ?)",
            (owner, _stamp(now), run_id, limit),
        )
        if cur.rowcount != 1:
            raise RunLocked(f"실행 {run_id} 잠김")

    def resumable(self) -> list[int]:
        """잠금이 만료된 끝나지 않은 실행"""
        limit = _stamp(self.clock() - timedelta(seconds=self.lock_ttl))
        rows = self.conn.execute(
            "SELECT id FROM ax_agent_runs WHERE status = 'running' AND heartbeat_at < ? ORDER BY id",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def run(self, run_id: int) -> dict:
        return dict(
            self.conn.execute("SELECT * FROM ax_agent_runs WHERE id = ?", (run_id,)).fetchone()
        )

    def add_step(self, run_id: int, kind: str, **fields) -> None:
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM ax_agent_steps WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        for name in JSON_FIELDS:
            if name in fields:
                fields[name] = _dump(fields[name])
        names = ["run_id", "seq", "kind", "created_at", *fields]
        values = [run_id, seq, kind, _stamp(self.clock()), *fields.values()]
        marks = ", ".join("?" * len(values))
        self.conn.execute(
            f"INSERT INTO ax_agent_steps ({', '.join(names)}) VALUES ({marks})", values
        )

    def steps(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ax_agent_steps WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        out = []
        for row in rows:
            step = dict(row)
            for name in JSON_FIELDS:
                if step[name] is not None:
                    step[name] = json.loads(step[name])
            out.append(step)
        return out

    def add_usage(
        self, run_id: int, owner: str, model_calls=0, tool_calls=0, cost_usd=0.0
    ) -> None:
        """호출 수·비용을 더하고 하트비트를 갱신한다. 잠금을 뺏겼으면 RunLocked"""
        cur = self.conn.execute(
            "UPDATE ax_agent_runs SET model_calls = model_calls + ?, tool_calls = tool_calls + ?,"
            " cost_usd = cost_usd + ?, heartbeat_at = ? WHERE id = ? AND locked_by = ?",
            (model_calls, tool_calls, cost_usd, _stamp(self.clock()), run_id, owner),
        )
        if cur.rowcount != 1:
            raise RunLocked(f"실행 {run_id} 잠금을 잃음")

    def finish(self, run_id: int, status: str, owner: str, result=None, reason=None) -> None:
        cur = self.conn.execute(
            "UPDATE ax_agent_runs SET status = ?, result = ?, reason = ?, finished_at = ?,"
            " locked_by = NULL WHERE id = ? AND locked_by = ?",
            (status, result, reason, _stamp(self.clock()), run_id, owner),
        )
        if cur.rowcount != 1:
            raise RunLocked(f"실행 {run_id} 잠금을 잃음")

    def add_block(self, rule_id: str) -> None:
        count_block(self.conn, rule_id)

    def block_counts(self) -> dict:
        return dict(self.conn.execute("SELECT rule_id, blocked FROM ax_rule_counts").fetchall())


class SqliteLlmLog:
    """게이트웨이 호출 기록 — shared/llm.py JsonlLog와 같은 인터페이스(write·spent)"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def write(self, record: dict) -> None:
        self.conn.execute(
            "INSERT INTO ax_llm_calls (task, model_alias, model_actual, prompt_version,"
            " fallback_used, ok, created_at, input_tokens, output_tokens, cost_usd, latency_ms,"
            " error, ref_type, ref_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["task"],
                record["alias"],
                record["model"],
                record["prompt_version"],
                int(record["fallback_used"]),
                int(record["ok"]),
                record["at"].replace("T", " "),
                record["input_tokens"],
                record["output_tokens"],
                record["cost_usd"],
                record["latency_ms"],
                record["error"],
                record["ref_type"],
                None if record["ref_id"] is None else str(record["ref_id"]),
            ),
        )
        self.conn.commit()

    def spent(self, day: date, scope: str) -> float:
        """앱 DB에는 한 범위(runtime)만 쓴다"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM ax_llm_calls WHERE substr(created_at, 1, 10) = ?",
            (day.isoformat(),),
        ).fetchone()
        return row[0]
